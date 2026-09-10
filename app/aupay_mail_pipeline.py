from __future__ import annotations

import base64
import hashlib
import html
import json
import re
import secrets
import time
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from email import policy
from email.parser import BytesParser

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from .sheets import SheetsDB
from .utils import canonical_hash, now_jst_string
from .aupay_card_contract import aupay_card_source_hash
from .transaction_plan import build_write_plan, reconcile_transactions
from .aupay_card_apply_plan import build_canonical_apply_plan
from .aupay_card_executor import (
    SheetsCanonicalIdentityReader,
    execute_canonical_apply_plan,
)

GMAIL_READONLY = "https://www.googleapis.com/auth/gmail.readonly"


def authorize_gmail(client_secret_file: str, token_output_file: str) -> None:
    """Run one-time desktop OAuth and save an authorized-user JSON locally."""
    from google_auth_oauthlib.flow import InstalledAppFlow

    flow = InstalledAppFlow.from_client_secrets_file(
        client_secret_file,
        scopes=[GMAIL_READONLY],
    )
    credentials = flow.run_local_server(
        host="localhost",
        port=0,
        authorization_prompt_message="次のURLをブラウザで開いてGmail読み取りを許可してください:\n{url}",
        success_message="認証が完了しました。このブラウザ画面を閉じてください。",
        open_browser=True,
    )
    with open(token_output_file, "w", encoding="utf-8") as handle:
        handle.write(credentials.to_json())


@dataclass(frozen=True)
class AuPayNotice:
    slip_number: str
    date: str
    merchant: str
    amount: int
    payment_method: str = "au PAY"

    @property
    def import_id(self) -> str:
        return f"aupay:{self.slip_number}"


@dataclass(frozen=True)
class ParsedCardLineItem:
    item_number: int
    date: str
    merchant: str
    amount_yen: int
    transaction_kind: str
    payment_method: str
    member: str
    source_record_id: str
    source_occurrence: int
    source_hash: str

    def to_reconciliation_input(self) -> dict:
        return {
            "date": self.date,
            "merchant": self.merchant,
            "amount": self.amount_yen,
            "transaction_kind": self.transaction_kind,
            "payment_type": self.payment_method,
            "member": self.member,
            "memo": f"メール明細No.{self.item_number:03d}",
            "occurrence": self.item_number,
            "import_id": self.source_record_id,
            "source_occurrence": self.source_occurrence,
            "source_memo": "",
            "source_hash": self.source_hash,
        }


@dataclass(frozen=True)
class CardLineItemReview:
    item_number: int
    reason: str
    field_reasons: tuple[str, ...]
    evidence_classifications: tuple[str, ...]


@dataclass(frozen=True)
class CardMailParseResult:
    accepted_items: tuple[ParsedCardLineItem, ...] = ()
    review_items: tuple[CardLineItemReview, ...] = ()
    mail_rejection_reason: str = ""
    mail_evidence_classifications: tuple[str, ...] = ()

    @property
    def status(self) -> str:
        if self.mail_rejection_reason or (self.review_items and not self.accepted_items):
            return "rejected"
        if self.review_items:
            return "partial"
        return "accepted"

    def reconciliation_inputs(self) -> list[dict]:
        return [item.to_reconciliation_input() for item in self.accepted_items]


def _text(value: str) -> str:
    value = html.unescape(value or "")
    value = re.sub(r"<br\s*/?>", "\n", value, flags=re.I)
    value = re.sub(r"<[^>]+>", " ", value)
    return unicodedata.normalize("NFKC", value).replace("\u00a0", " ")


def _field(body: str, labels: list[str], value_pattern: str) -> str:
    names = "|".join(re.escape(x) for x in labels)
    match = re.search(
        rf"(?:{names})\s*[：:]?\s*({value_pattern})",
        body,
        flags=re.I,
    )
    return match.group(1).strip() if match else ""


def parse_aupay_notice(body: str) -> AuPayNotice:
    """Parse an au PAY usage notice. Missing/ambiguous core fields fail closed."""
    body = _text(body)
    slip = _field(body, ["伝票番号", "取引番号"], r"[0-9A-Za-z-]{6,40}")
    amount_raw = _field(
        body,
        ["ご利用金額", "利用金額", "決済金額", "支払金額"],
        r"[¥￥]?\s*[0-9,]+\s*円?",
    )
    transaction_type = _field(body, ["種別"], r"[^\r\n]+")
    date_raw = _field(
        body,
        ["ご利用日時", "利用日時", "決済日時", "ご利用日", "利用日"],
        r"\d{4}[年/-]\d{1,2}[月/-]\d{1,2}日?(?:\s+\d{1,2}:\d{2}(?::\d{2})?)?",
    )
    merchant = _field(
        body,
        ["ご利用店舗", "利用店舗", "ご利用先", "利用先", "加盟店名"],
        r"[^\r\n]+",
    )
    merchant = re.split(r"\s{2,}|(?:ご利用|利用|決済)(?:金額|日時|日)\s*[：:]", merchant)[0].strip(" :-")

    missing = [name for name, value in (
        ("伝票番号", slip), ("利用日時", date_raw), ("利用店舗", merchant), ("利用金額", amount_raw)
    ) if not value]
    if missing:
        raise ValueError("au PAY通知の必須項目を抽出できません: " + ", ".join(missing))
    if transaction_type and transaction_type.strip() != "支払":
        raise ValueError(f"au PAY通知の種別が支払ではありません: {transaction_type.strip()}")

    normalized_date = re.sub(r"[年月]", "-", date_raw).replace("日", "").replace("/", "-")
    try:
        date = datetime.strptime(normalized_date.split()[0], "%Y-%m-%d").strftime("%Y-%m-%d")
    except ValueError as exc:
        raise ValueError(f"au PAY通知の利用日が不正です: {date_raw}") from exc
    amount = int(re.sub(r"\D", "", amount_raw))
    if amount <= 0:
        raise ValueError("au PAY通知の利用金額が0以下です")
    return AuPayNotice(slip, date, merchant, amount)


def parse_eml(path: str) -> AuPayNotice:
    with open(path, "rb") as handle:
        message = BytesParser(policy=policy.default).parse(handle)
    parts = []
    for part in message.walk():
        if part.get_content_type() in ("text/plain", "text/html"):
            try:
                parts.append(part.get_content())
            except (LookupError, UnicodeDecodeError):
                continue
    return parse_aupay_notice("\n".join(parts))


def parse_aupay_card_raw_partial(raw_mime: bytes) -> CardMailParseResult:
    """Parse independent line items without granting production authority."""
    if not isinstance(raw_mime, bytes) or not raw_mime:
        return CardMailParseResult(
            mail_rejection_reason="invalid_raw_mime",
            mail_evidence_classifications=("raw_mime_unavailable",),
        )
    message = BytesParser(policy=policy.default).parsebytes(raw_mime)
    subject = str(message.get("subject", ""))
    if "au PAY カード" not in unicodedata.normalize("NFKC", subject):
        return CardMailParseResult(
            mail_rejection_reason="non_card_notice",
            mail_evidence_classifications=("subject_not_card_detail",),
        )
    message_id = str(message.get("Message-ID", "")).strip()
    if not message_id:
        return CardMailParseResult(
            mail_rejection_reason="missing_message_id",
            mail_evidence_classifications=("stable_source_identity_unavailable",),
        )
    body = message.get_body(preferencelist=("plain",))
    if body is None:
        return CardMailParseResult(
            mail_rejection_reason="missing_plain_text",
            mail_evidence_classifications=("authoritative_plain_part_unavailable",),
        )
    text = unicodedata.normalize("NFKC", body.get_content())
    member_match = re.search(r"(本会員|家族会員)さま\s*ご利用分", text)
    member = member_match.group(1) if member_match else ""
    blocks = re.split(r"(?m)^\s*No\.(\d+)\s*-+\s*$", text)
    if len(blocks) == 1:
        return CardMailParseResult(
            mail_rejection_reason="missing_card_details",
            mail_evidence_classifications=("line_item_delimiter_unavailable",),
        )
    source_hash = hashlib.sha256(message_id.encode("utf-8")).hexdigest()[:24]
    accepted: list[ParsedCardLineItem] = []
    source_occurrences: dict[tuple, int] = {}
    review: list[CardLineItemReview] = []
    for i in range(1, len(blocks), 2):
        number, block = int(blocks[i]), blocks[i + 1]
        date_raw = _field(block, ["▼ご利用日"], r"\d{4}年\d{1,2}月\d{1,2}日")
        merchant = _field(block, ["▼ご利用先"], r"[^\r\n]+")
        purchase_raw = _field(block, ["▼ご利用金額"], r"[0-9,]+円")
        return_raw = _field(block, ["▼ご利用金額"], r"-[0-9,]+円\s*\(返品\)")
        negative_raw = _field(block, ["▼ご利用金額"], r"-[^\r\n]+")
        field_reasons: list[str] = []
        evidence: list[str] = []
        try:
            date = datetime.strptime(date_raw, "%Y年%m月%d日").strftime("%Y-%m-%d")
        except (TypeError, ValueError):
            date = ""
            field_reasons.append("transaction_date_missing_or_invalid")
            evidence.append("transaction_date_field_unusable")
        if not merchant.strip():
            field_reasons.append("merchant_missing")
            evidence.append("merchant_field_unusable")

        amount = 0
        transaction_kind = ""
        if purchase_raw:
            amount = int(re.sub(r"\D", "", purchase_raw))
            transaction_kind = "purchase"
        elif return_raw and "返品" in block:
            amount = -int(re.sub(r"\D", "", return_raw))
            transaction_kind = "return"
        elif negative_raw:
            field_reasons.append("amount_unknown_negative_format")
            evidence.extend(("negative_amount", "deterministic_return_evidence_unavailable"))
        else:
            field_reasons.append("amount_missing_or_invalid")
            evidence.append("amount_field_unusable")
        if amount == 0 and transaction_kind:
            field_reasons.append("amount_zero")
            evidence.append("zero_amount")

        if field_reasons:
            review.append(CardLineItemReview(
                item_number=number,
                reason="line_item_required_field_invalid",
                field_reasons=tuple(dict.fromkeys(field_reasons)),
                evidence_classifications=tuple(dict.fromkeys(evidence)),
            ))
            continue
        payment_method = "通常払い"
        occurrence_key = (date, merchant.strip(), amount, payment_method, member, "")
        source_occurrence = source_occurrences.get(occurrence_key, 0) + 1
        source_occurrences[occurrence_key] = source_occurrence
        source_record_id = f"aupaycard-mail:{source_hash}:{number:03d}"
        source_input = {
            "date": date,
            "merchant": merchant.strip(),
            "amount": amount,
            "payment_type": payment_method,
            "member": member,
            "source_memo": "",
            "source_occurrence": source_occurrence,
            "import_id": source_record_id,
        }
        accepted.append(ParsedCardLineItem(
            item_number=number,
            date=date,
            merchant=merchant.strip(),
            amount_yen=amount,
            transaction_kind=transaction_kind,
            payment_method=payment_method,
            member=member,
            source_record_id=source_record_id,
            source_occurrence=source_occurrence,
            source_hash=aupay_card_source_hash(source_input),
        ))
    return CardMailParseResult(tuple(accepted), tuple(review))


def parse_aupay_card_raw(raw_mime: bytes) -> list[dict]:
    """Compatibility wrapper retaining the original all-or-nothing contract."""
    result = parse_aupay_card_raw_partial(raw_mime)
    if result.mail_rejection_reason:
        messages = {
            "invalid_raw_mime": "カードメールのraw MIMEがありません",
            "non_card_notice": "au PAYカード利用詳細メールではありません",
            "missing_message_id": "メールにMessage-IDがないため安全に一意キーを作れません",
            "missing_plain_text": "メールにtext/plain本文がありません",
            "missing_card_details": "カードメールに利用明細がありません",
        }
        raise ValueError(messages[result.mail_rejection_reason])
    if result.review_items:
        number = result.review_items[0].item_number
        raise ValueError(f"カードメール明細No.{number:03d}の必須項目を抽出できません")
    if any(item.transaction_kind == "return" for item in result.accepted_items):
        raise ValueError("返品明細はpartial parse reconciliation経路でのみ扱えます")
    return result.reconciliation_inputs()


def parse_aupay_card_eml(path: str) -> list[dict]:
    """Parse the multi-transaction 'au PAY カード' usage detail email."""
    with open(path, "rb") as handle:
        return parse_aupay_card_raw(handle.read())


def _decode_gmail_body(payload: dict) -> str:
    texts = []
    if payload.get("mimeType") in ("text/plain", "text/html") and payload.get("body", {}).get("data"):
        raw = payload["body"]["data"]
        data = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
        content_type = next(
            (h.get("value", "") for h in payload.get("headers", [])
             if h.get("name", "").lower() == "content-type"),
            "",
        )
        charset_match = re.search(r"charset=[\"']?([^;\s\"']+)", content_type, flags=re.I)
        charset = charset_match.group(1) if charset_match else "utf-8"
        try:
            texts.append(data.decode(charset))
        except (LookupError, UnicodeDecodeError):
            texts.append(data.decode("utf-8", errors="replace"))
    for part in payload.get("parts", []):
        texts.append(_decode_gmail_body(part))
    return "\n".join(x for x in texts if x)


def _decode_gmail_raw(value: str) -> bytes:
    if not isinstance(value, str) or not value:
        raise ValueError("Gmail raw MIMEを取得できません")
    try:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (TypeError, ValueError) as exc:
        raise ValueError("Gmail raw MIMEが不正です") from exc


def _card_mail_reason(error: ValueError) -> str:
    message = str(error)
    if "Message-ID" in message:
        return "missing_message_id"
    if "text/plain" in message:
        return "missing_plain_text"
    if "利用明細がありません" in message:
        return "missing_card_details"
    if "必須項目" in message:
        return "missing_required_fields"
    if "カード利用詳細" in message or "利用明細" in message:
        return "non_card_notice"
    if "raw MIME" in message:
        return "invalid_raw_mime"
    return "parse_failed"


def _transient_gmail_error(error: HttpError) -> bool:
    status = int(getattr(error.resp, "status", 0) or 0)
    if status == 429 or 500 <= status <= 599:
        return True
    if status != 403:
        return False
    try:
        payload = json.loads(error.content.decode("utf-8"))
    except (AttributeError, UnicodeDecodeError, json.JSONDecodeError):
        return False
    reasons = {
        item.get("reason", "")
        for item in payload.get("error", {}).get("errors", [])
        if isinstance(item, dict)
    }
    return bool(reasons & {"rateLimitExceeded", "userRateLimitExceeded", "backendError"})


def _execute_gmail(request_factory, *, sleeper=time.sleep, max_attempts: int = 6,
                   base_delay: float = 0.5):
    """Execute a Gmail read with bounded retry for transient errors only."""
    retries = 0
    for attempt in range(max_attempts):
        try:
            return request_factory().execute(), retries
        except HttpError as exc:
            if not _transient_gmail_error(exc) or attempt + 1 >= max_attempts:
                raise
            sleeper(base_delay * (2 ** attempt))
            retries += 1
    raise RuntimeError("gmail_retry_exhausted")


def gmail_service(token_json: str):
    info = json.loads(token_json)
    creds = Credentials.from_authorized_user_info(info, scopes=[GMAIL_READONLY])
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


class AuPayMailPipeline:
    def __init__(self, db: SheetsDB):
        self.db = db

    def import_notice(self, notice: AuPayNotice, gmail_message_id: str = "") -> str:
        if notice.import_id in self.db.import_ids():
            return "unchanged"
        raw = {
            "slip_number": notice.slip_number,
            "date": notice.date,
            "merchant": notice.merchant,
            "amount": notice.amount,
            "payment_method": notice.payment_method,
        }
        note = "au PAY実支出。レシート等との照合待ち"
        if gmail_message_id:
            note += f"; Gmail message={gmail_message_id}"
        self.db.append("取込データ", [[
            notice.import_id, now_jst_string(), "au PAY", notice.slip_number,
            notice.date, notice.merchant, notice.amount, notice.payment_method,
            "unclassified_aupay", "", canonical_hash(raw), note,
        ]])
        return "new"

    def import_gmail(self, token_json: str, query: str, max_results: int = 100) -> dict:
        service = gmail_service(token_json)
        stats = {"found": 0, "new": 0, "unchanged": 0, "needs_review": 0}
        page_token = None
        while stats["found"] < max_results:
            response = service.users().messages().list(
                userId="me", q=query, maxResults=min(100, max_results - stats["found"]),
                pageToken=page_token,
            ).execute()
            messages = response.get("messages", [])
            if not messages:
                break
            for item in messages:
                stats["found"] += 1
                message = service.users().messages().get(userId="me", id=item["id"], format="full").execute()
                try:
                    notice = parse_aupay_notice(_decode_gmail_body(message.get("payload", {})))
                except ValueError:
                    stats["needs_review"] += 1
                    continue
                result = self.import_notice(notice, item["id"])
                stats[result] += 1
            page_token = response.get("nextPageToken")
            if not page_token:
                break
        return stats


class AuPayCardMailPipeline:
    """Read au PAY card detail messages and hand parsed rows to the card pipeline."""

    def __init__(self, db: SheetsDB | None = None):
        self.db = db

    @staticmethod
    def _empty_summary() -> dict:
        return {
            "found": 0,
            "fetched": 0,
            "duplicate_gmail_message": 0,
            "parsed_messages": 0,
            "parsed_transactions": 0,
            "accepted_messages": 0,
            "partial_messages": 0,
            "review_only_messages": 0,
            "accepted_line_items": 0,
            "review_line_items": 0,
            "purchase_line_items": 0,
            "return_line_items": 0,
            "parser_review_mail_count": 0,
            "needs_review": 0,
            "missing_message_id": 0,
            "missing_plain_text": 0,
            "missing_required_fields": 0,
            "missing_card_details": 0,
            "non_card_notice": 0,
            "invalid_raw_mime": 0,
            "parse_failed": 0,
            "gmail_list_failed": 0,
            "gmail_read_failed": 0,
            "gmail_retry_count": 0,
            "listing_complete": False,
            "collection_truncated": False,
            "collection_complete": False,
        }

    @classmethod
    def _collect(cls, service, query: str, max_results: int, *,
                 sleeper=None, request_interval: float = 0.25) -> tuple[list[dict], dict]:
        if max_results <= 0:
            raise ValueError("max_resultsは1以上にしてください")
        sleeper = sleeper or time.sleep
        summary = cls._empty_summary()
        transactions: list[dict] = []
        seen_message_ids: set[str] = set()
        page_token = None
        while summary["found"] < max_results:
            try:
                list_response, retries = _execute_gmail(lambda: service.users().messages().list(
                    userId="me", q=query,
                    maxResults=min(100, max_results - summary["found"]),
                    pageToken=page_token,
                ), sleeper=sleeper)
                summary["gmail_retry_count"] += retries
            except HttpError:
                summary["gmail_list_failed"] += 1
                break
            messages = list_response.get("messages", [])
            if not messages:
                summary["listing_complete"] = True
                break
            for item in messages:
                if summary["found"] >= max_results:
                    break
                summary["found"] += 1
                gmail_message_id = str(item.get("id") or "").strip()
                if not gmail_message_id:
                    summary["needs_review"] += 1
                    summary["parser_review_mail_count"] += 1
                    summary["invalid_raw_mime"] += 1
                    continue
                if gmail_message_id in seen_message_ids:
                    summary["duplicate_gmail_message"] += 1
                    continue
                seen_message_ids.add(gmail_message_id)
                try:
                    response, retries = _execute_gmail(lambda: service.users().messages().get(
                        userId="me", id=gmail_message_id, format="raw",
                    ), sleeper=sleeper)
                    summary["gmail_retry_count"] += retries
                    raw_mime = _decode_gmail_raw(response.get("raw", ""))
                    parsed_result = parse_aupay_card_raw_partial(raw_mime)
                except HttpError:
                    summary["needs_review"] += 1
                    summary["gmail_read_failed"] += 1
                    continue
                except ValueError as exc:
                    summary["needs_review"] += 1
                    summary["parser_review_mail_count"] += 1
                    summary[_card_mail_reason(exc)] += 1
                    continue
                summary["fetched"] += 1
                if parsed_result.mail_rejection_reason:
                    summary["needs_review"] += 1
                    summary["parser_review_mail_count"] += 1
                    summary[parsed_result.mail_rejection_reason] += 1
                    summary["review_only_messages"] += 1
                    if request_interval > 0:
                        sleeper(request_interval)
                    continue
                parsed = parsed_result.reconciliation_inputs()
                if parsed:
                    summary["parsed_messages"] += 1
                summary["parsed_transactions"] += len(parsed)
                summary["accepted_line_items"] += len(parsed)
                summary["review_line_items"] += len(parsed_result.review_items)
                summary["purchase_line_items"] += sum(
                    item.transaction_kind == "purchase"
                    for item in parsed_result.accepted_items
                )
                summary["return_line_items"] += sum(
                    item.transaction_kind == "return"
                    for item in parsed_result.accepted_items
                )
                if parsed_result.review_items:
                    summary["needs_review"] += 1
                    summary["parser_review_mail_count"] += 1
                    summary["missing_required_fields"] += 1
                    if parsed:
                        summary["partial_messages"] += 1
                    else:
                        summary["review_only_messages"] += 1
                else:
                    summary["accepted_messages"] += 1
                transactions.extend(parsed)
                if request_interval > 0:
                    sleeper(request_interval)
            page_token = list_response.get("nextPageToken")
            if not page_token:
                summary["listing_complete"] = True
                break
            if summary["found"] >= max_results:
                summary["collection_truncated"] = True
                break
        summary["collection_complete"] = bool(
            summary["listing_complete"]
            and not summary["collection_truncated"]
            and not summary["gmail_list_failed"]
            and not summary["gmail_read_failed"]
        )
        return transactions, summary

    def preview_gmail(self, token_json: str, query: str, max_results: int = 100) -> dict[str, int]:
        """Read and parse Gmail messages without accessing Sheets or writing data."""
        _, summary = self._collect(gmail_service(token_json), query, max_results)
        return summary

    def preview_write_plan(self, token_json: str, query: str, max_results: int = 100) -> dict:
        """Collect Gmail and read existing imports, returning a write-free plan summary."""
        if self.db is None:
            raise ValueError("write-plan previewにはread-only SheetsDBが必要です")
        transactions, collection = self._collect(gmail_service(token_json), query, max_results)
        plan = build_write_plan(transactions, self.db.get("取込データ!A2:L"))
        same_day_groups = plan["same_day_same_amount_groups"]
        summary = dict(collection)
        summary.update({
            "input_mail_count": collection["found"],
            "parsed_transaction_count": collection["parsed_transactions"],
            "unique_transaction_count": plan["unique_transactions"],
            "duplicate_count": plan["summary"]["duplicate"],
            "needs_review_count": collection["needs_review"] + plan["summary"]["needs_review"],
            "rejected_count": plan["summary"]["rejected"],
            "identity_collisions": plan["identity_collisions"],
            "same_day_same_amount_group_count": len(same_day_groups),
            "same_day_same_amount_samples": same_day_groups[:5],
            "write_plan_inserts": plan["unique_transactions"],
            "canonical_transaction_count": plan["reconciliation"]["summary"]["canonical_transactions"],
            "probable_resend_groups": plan["reconciliation"]["summary"]["probable_resend_groups"],
            "probable_resend_records": plan["reconciliation"]["summary"]["probable_resend_records"],
            "cross_source_strong_match": plan["reconciliation"]["summary"]["cross_source_strong_match"],
            "cross_source_ambiguous": plan["reconciliation"]["summary"]["cross_source_ambiguous"],
            "cross_source_no_match": plan["reconciliation"]["summary"]["cross_source_no_match"],
        })
        return summary

    def preview_apply_plan(self, token_json: str, query: str, max_results: int = 100) -> dict:
        """Build and pre-apply revalidate a write-free canonical plan."""
        if self.db is None:
            raise ValueError("apply-plan previewにはread-only SheetsDBが必要です")
        transactions, collection = self._collect(gmail_service(token_json), query, max_results)
        # Plan-time identity state is intentionally a separate read from the
        # executor's mandatory pre-apply read-back below.
        reconciliation = reconcile_transactions(
            transactions, self.db.get("取込データ!A2:L"),
        )
        plan = build_canonical_apply_plan(collection, reconciliation)
        result = dict(collection)
        result.update(plan.summary())
        if plan.executable:
            execution = execute_canonical_apply_plan(
                plan,
                SheetsCanonicalIdentityReader(self.db),
                apply=False,
                audit_key=secrets.token_bytes(32),
                audit_ref_scope="ephemeral_read_only_run",
            )
            result.update(execution.summary())
        else:
            result.update({
                "executor_result_schema_version": 1,
                "execution_status": "rejected_invalid_plan",
                "apply_requested": False,
                "writer_connected": False,
                "input_candidate_count": 0,
                "selected_candidate_count": 0,
                "revalidated_new_count": 0,
                "already_present_count": 0,
                "conflict_count": 0,
                "withheld_count": plan.canonical_transaction_count,
                "would_write_count": 0,
                "failed_review_count": plan.canonical_transaction_count,
                "executor_reason_codes": ["apply_plan_blocked"],
                "external_write_count": 0,
            })
        return result

    def import_gmail(self, token_json: str, query: str, max_results: int = 100) -> dict[str, int]:
        """Reject the legacy raw-to-Sheets production path before any API access."""
        raise RuntimeError("raw_card_gmail_import_disabled_use_canonical_apply_plan")
