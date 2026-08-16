from __future__ import annotations

import base64
import hashlib
import html
import json
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from email import policy
from email.parser import BytesParser

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

from .sheets import SheetsDB
from .utils import canonical_hash, now_jst_string

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


def parse_aupay_card_eml(path: str) -> list[dict]:
    """Parse the multi-transaction 'au PAY カード' usage detail email."""
    with open(path, "rb") as handle:
        message = BytesParser(policy=policy.default).parse(handle)
    subject = str(message.get("subject", ""))
    if "au PAY カード" not in unicodedata.normalize("NFKC", subject):
        raise ValueError("au PAYカード利用詳細メールではありません")
    message_id = str(message.get("Message-ID", "")).strip()
    if not message_id:
        raise ValueError("メールにMessage-IDがないため安全に一意キーを作れません")
    body = message.get_body(preferencelist=("plain",))
    if body is None:
        raise ValueError("メールにtext/plain本文がありません")
    text = unicodedata.normalize("NFKC", body.get_content())
    member_match = re.search(r"(本会員|家族会員)さま\s*ご利用分", text)
    member = member_match.group(1) if member_match else ""
    blocks = re.split(r"(?m)^\s*No\.(\d+)\s*-+\s*$", text)
    source_hash = hashlib.sha256(message_id.encode("utf-8")).hexdigest()[:24]
    rows = []
    for i in range(1, len(blocks), 2):
        number, block = blocks[i], blocks[i + 1]
        date_raw = _field(block, ["▼ご利用日"], r"\d{4}年\d{1,2}月\d{1,2}日")
        amount_raw = _field(block, ["▼ご利用金額"], r"[0-9,]+円")
        merchant = _field(block, ["▼ご利用先"], r"[^\r\n]+")
        if not (date_raw and amount_raw and merchant):
            raise ValueError(f"カードメール明細No.{number}の必須項目を抽出できません")
        date = datetime.strptime(date_raw, "%Y年%m月%d日").strftime("%Y-%m-%d")
        amount = int(re.sub(r"\D", "", amount_raw))
        if amount <= 0:
            raise ValueError(f"カードメール明細No.{number}の金額が0以下です")
        import_id = f"aupaycard-mail:{source_hash}:{int(number):03d}"
        rows.append({
            "date": date,
            "merchant": merchant.strip(),
            "amount": amount,
            "payment_type": "メール通知",
            "member": member,
            "memo": f"メール明細No.{int(number):03d}",
            "occurrence": int(number),
            "import_id": import_id,
        })
    if not rows:
        raise ValueError("カードメールに利用明細がありません")
    return rows


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
