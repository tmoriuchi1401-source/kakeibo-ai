"""Offline, read-only preview for one ordinary Japanese receipt.

The module deliberately stops at a Sheets-compatible import-row plan.  It does
not call external AI and contains no writer.  OCR text is kept only in local
variables while candidate evidence is bound to the source digest.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass
from datetime import date
from typing import Literal

from .medical_ocr_observation_shadow import OcrObservation, ReceiptImage
from .medical_receipt_privacy import _StructuredOcrToken, build_receipt_privacy_preview
from .receipt_text_extraction import ExtractionMethod, _extract_receipt_text
from .reconciliation import (
    ImportTransaction,
    merchants_match,
    parse_import_rows,
    reconcile_transactions,
)


PreviewStatus = Literal[
    "ready",
    "needs_review",
    "privacy_blocked",
    "not_receipt",
    "extraction_failed",
    "duplicate",
]
Confidence = Literal["high", "medium", "low", "none"]
ReceiptOcrMethod = ExtractionMethod | Literal["rapidocr"]

_TOTAL_LABELS: tuple[tuple[str, int], ...] = (
    ("お支払金額", 60),
    ("お買上金額", 60),
    ("総合計", 55),
    ("合計金額", 55),
    ("現計", 50),
    ("合計", 45),
)
_NEGATIVE_TOTAL_CONTEXT = (
    "小計", "税抜", "消費税", "外税", "内税", "預り", "お預り", "釣銭", "お釣り",
    "ポイント", "クーポン", "値引", "割引", "残高", "税額",
)
_MERCHANT_NOISE = (
    "領収書", "レシート", "receipt", "お買上", "毎度", "ありがとう", "合計", "小計",
    "現計", "支払", "預り", "釣銭", "ポイント", "クーポン", "日時", "日付", "担当",
    "レジ", "tel", "電話", "〒", "http", "www", "登録番号", "適格請求書",
)
_MERCHANT_HINTS = ("店", "ストア", "マーケット", "スーパー", "マート", "薬局", "株式会社", "有限会社")
_AMOUNT = re.compile(
    r"(?<![0-9A-Za-z])(?:[¥￥]\s*)?([0-9０-９]{1,3}(?:[,，][0-9０-９]{3})+|[0-9０-９]{1,9})(?:\s*円|\s*[-―ー])?(?![0-9A-Za-z])"
)
_DATE_PATTERNS = (
    re.compile(r"(?<!\d)(20\d{2})[年/\.\-]\s*(\d{1,2})[月/\.\-]\s*(\d{1,2})日?(?!\d)"),
    re.compile(r"(?<!\d)(\d{2})[/\.\-](\d{1,2})[/\.\-](\d{1,2})(?!\d)"),
    re.compile(r"(?<![A-Za-z0-9])R\s*(\d{1,2})[./\-年]\s*(\d{1,2})[./\-月]\s*(\d{1,2})日?", re.I),
)


@dataclass(frozen=True)
class FieldCandidate:
    value: str | int
    line_index: int
    score: int
    evidence_sha256: str


@dataclass(frozen=True)
class GeneralReceiptPreview:
    status: PreviewStatus
    confidence: Confidence
    source_sha256: str
    extraction_method: ReceiptOcrMethod
    classification: str
    purchase_date: str | None
    merchant: str | None
    total: int | None
    issues: tuple[str, ...]
    date_candidate_count: int
    merchant_candidate_count: int
    total_candidate_count: int
    duplicate_state: str
    duplicate_candidate_ids: tuple[str, ...]
    reconciliation_status: str
    reconciliation_candidate_ids: tuple[str, ...]
    transaction_row: tuple[object, ...] | None
    write_plan_rows: int
    external_ai_used: Literal[False] = False
    write_authorized: Literal[False] = False

    def as_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "confidence": self.confidence,
            "source_sha256": self.source_sha256,
            "extraction_method": self.extraction_method,
            "classification": self.classification,
            "date": self.purchase_date,
            "merchant": self.merchant,
            "total": self.total,
            "issues": list(self.issues),
            "candidate_counts": {
                "date": self.date_candidate_count,
                "merchant": self.merchant_candidate_count,
                "total": self.total_candidate_count,
            },
            "duplicate": {
                "state": self.duplicate_state,
                "candidate_ids": list(self.duplicate_candidate_ids),
            },
            "reconciliation": {
                "status": self.reconciliation_status,
                "candidate_ids": list(self.reconciliation_candidate_ids),
            },
            "transaction_row": list(self.transaction_row) if self.transaction_row else None,
            "write_plan_rows": self.write_plan_rows,
            "external_ai_used": self.external_ai_used,
            "write_authorized": self.write_authorized,
        }


def _compact(value: str) -> str:
    return "".join(unicodedata.normalize("NFKC", value).split())


def _evidence(source_sha256: str, kind: str, line_index: int, value: object) -> str:
    material = f"{source_sha256}\0{kind}\0{line_index}\0{value}".encode("utf-8")
    return hashlib.sha256(material).hexdigest()


def _amounts(line: str) -> tuple[int, ...]:
    values = []
    normalized = unicodedata.normalize("NFKC", line)
    for match in _AMOUNT.finditer(normalized):
        token = match.group(1).replace(",", "")
        try:
            value = int(token)
        except ValueError:
            continue
        if 0 < value <= 999_999_999:
            values.append(value)
    return tuple(values)


def extract_total_candidates(text: str, source_sha256: str) -> tuple[FieldCandidate, ...]:
    """Return label-bound totals; unsafe monetary contexts never become candidates."""
    lines = text.splitlines()
    found: list[FieldCandidate] = []
    for index, raw in enumerate(lines):
        compact = _compact(raw).casefold()
        label = next(((name, rank) for name, rank in _TOTAL_LABELS if name in compact), None)
        if label is None or any(term in compact for term in _NEGATIVE_TOTAL_CONTEXT):
            continue
        values = tuple(dict.fromkeys(_amounts(raw)))
        score = label[1]
        if "税込" in compact:
            score += 3
        if not values and index + 1 < len(lines):
            next_compact = _compact(lines[index + 1])
            if not any(term in next_compact for term in _NEGATIVE_TOTAL_CONTEXT):
                values = tuple(dict.fromkeys(_amounts(lines[index + 1])))
                score -= 8
        if len(values) != 1:
            continue
        value = values[0]
        found.append(FieldCandidate(value, index, score, _evidence(source_sha256, "total", index, value)))
    # Repeated OCR observations of the same label-bound amount are corroboration,
    # not ambiguity.  Keep the strongest binding deterministically.
    strongest: dict[int, FieldCandidate] = {}
    for candidate in found:
        current = strongest.get(int(candidate.value))
        if current is None or (candidate.score, -candidate.line_index) > (current.score, -current.line_index):
            strongest[int(candidate.value)] = candidate
    return tuple(sorted(strongest.values(), key=lambda item: (-item.score, item.line_index)))


def _valid_date(year: int, month: int, day: int) -> str | None:
    try:
        return date(year, month, day).isoformat()
    except ValueError:
        return None


def extract_date_candidates(text: str, source_sha256: str) -> tuple[FieldCandidate, ...]:
    found: dict[str, FieldCandidate] = {}
    for index, raw in enumerate(text.splitlines()):
        normalized = unicodedata.normalize("NFKC", raw)
        compact = _compact(normalized).casefold()
        if any(term in compact for term in ("期限", "有効", "賞味", "消費期限")):
            continue
        for pattern_index, pattern in enumerate(_DATE_PATTERNS):
            for match in pattern.finditer(normalized):
                y, m, d = (int(value) for value in match.groups())
                if pattern_index == 1:
                    y += 2000 if y <= 79 else 1900
                elif pattern_index == 2:
                    y += 2018
                value = _valid_date(y, m, d)
                if value is None:
                    continue
                score = 40 - min(index, 15)
                if any(term in compact for term in ("取引日時", "購入日", "発行日", "日時", "日付")):
                    score += 20
                candidate = FieldCandidate(value, index, score, _evidence(source_sha256, "date", index, value))
                current = found.get(value)
                if current is None or candidate.score > current.score:
                    found[value] = candidate
    return tuple(sorted(found.values(), key=lambda item: (-item.score, item.line_index)))


def extract_merchant_candidates(text: str, source_sha256: str) -> tuple[FieldCandidate, ...]:
    found: dict[str, FieldCandidate] = {}
    for index, raw in enumerate(text.splitlines()[:12]):
        value = unicodedata.normalize("NFKC", raw).strip(" \t|:：*#")
        compact = _compact(value).casefold()
        if not (2 <= len(compact) <= 80) or any(term in compact for term in _MERCHANT_NOISE):
            continue
        digits = sum(char.isdigit() for char in compact)
        if digits > max(3, len(compact) // 2) or _amounts(value):
            continue
        if re.fullmatch(r"[\W_]+", compact):
            continue
        # Receipt headers strongly favour reading order.  A generic branch line
        # such as "中央店" must not outrank the brand printed directly above it.
        score = 60 - index * 4
        if index <= 2:
            score += 8
        if any(hint in compact for hint in _MERCHANT_HINTS):
            score += 6
        if re.search(r"[A-Za-z]{3}", value) and value.upper() == value:
            score += 12
        if "住所" in compact or re.search(r"\d{2,}[-ー]\d", compact):
            score -= 20
        candidate = FieldCandidate(value, index, score, _evidence(source_sha256, "merchant", index, value))
        key = _compact(value).casefold()
        current = found.get(key)
        if current is None or candidate.score > current.score:
            found[key] = candidate
    return tuple(sorted(found.values(), key=lambda item: (-item.score, item.line_index)))


def _select(candidates: tuple[FieldCandidate, ...], *, margin: int = 8) -> tuple[object | None, bool]:
    if not candidates:
        return None, False
    if len(candidates) > 1 and candidates[0].score - candidates[1].score < margin:
        return None, True
    return candidates[0].value, False


def _selected_text_confidence(
    value: str | None,
    tokens: tuple[_StructuredOcrToken, ...],
) -> float | None:
    """Find confidence for the exact OCR line that supplied a selected string."""
    if not value or not tokens:
        return None
    target = _compact(value).casefold()
    grouped: dict[tuple[int, tuple[int, int, int, int]], list[_StructuredOcrToken]] = {}
    for token in tokens:
        grouped.setdefault((token.page, token.line_key), []).append(token)
    matches = []
    for line_tokens in grouped.values():
        ordered = sorted(line_tokens, key=lambda token: token.x)
        line = _compact("".join(token.text for token in ordered)).casefold()
        if line != target:
            continue
        weight = sum(max(1, len(_compact(token.text))) for token in ordered)
        if weight:
            matches.append(sum(
                token.confidence * max(1, len(_compact(token.text))) for token in ordered
            ) / weight)
    return max(matches) if matches else None


def _duplicate_state(
    import_id: str,
    source_sha256: str,
    purchase_date: str | None,
    merchant: str | None,
    total: int | None,
    existing: list[ImportTransaction] | None,
) -> tuple[str, tuple[str, ...]]:
    if existing is None:
        return "not_checked", ()
    exact = [tx.import_id for tx in existing if tx.import_id == import_id or (len(tx.row) > 10 and tx.row[10] == source_sha256)]
    if exact:
        return "exact", tuple(dict.fromkeys(exact))
    if purchase_date and merchant and total is not None:
        semantic = [
            tx.import_id for tx in existing
            if tx.source == "receipt" and tx.date == purchase_date and tx.amount == total
            and merchants_match(tx.merchant, merchant)
        ]
        if semantic:
            return "possible", tuple(dict.fromkeys(semantic))
    return "none", ()


def preview_general_receipt_text(
    text: str,
    *,
    source_id: str,
    source_sha256: str | None = None,
    extraction_method: ReceiptOcrMethod = "image_ocr",
    classification: str = "normal",
    existing_rows: list[list] | None = None,
    ocr_tokens: tuple[_StructuredOcrToken, ...] = (),
) -> GeneralReceiptPreview:
    """Pure parser entry point, also used by the small human-checked eval harness."""
    digest = source_sha256 or hashlib.sha256(text.encode("utf-8")).hexdigest()
    import_id = f"receipt:{source_id}"
    dates = extract_date_candidates(text, digest)
    merchants = extract_merchant_candidates(text, digest)
    totals = extract_total_candidates(text, digest)
    selected_date, date_ambiguous = _select(dates)
    selected_merchant, merchant_ambiguous = _select(merchants, margin=5)
    selected_total, total_ambiguous = _select(totals, margin=10_000)
    issues = []
    if not dates:
        issues.append("date_missing")
    elif date_ambiguous:
        issues.append("date_ambiguous")
    if not merchants:
        issues.append("merchant_missing")
    elif merchant_ambiguous:
        issues.append("merchant_ambiguous")
    else:
        merchant_confidence = _selected_text_confidence(
            selected_merchant if isinstance(selected_merchant, str) else None, ocr_tokens,
        )
        if merchant_confidence is not None and merchant_confidence < 75:
            issues.append("merchant_ocr_low_confidence")
    if not totals:
        issues.append("total_missing")
    elif total_ambiguous:
        issues.append("total_ambiguous")

    existing = parse_import_rows(existing_rows) if existing_rows is not None else None
    duplicate_state, duplicate_ids = _duplicate_state(
        import_id, digest, selected_date if isinstance(selected_date, str) else None,
        selected_merchant if isinstance(selected_merchant, str) else None,
        selected_total if isinstance(selected_total, int) else None, existing,
    )
    if duplicate_state == "possible":
        issues.append("possible_duplicate")

    status: PreviewStatus
    if not totals and not any(term in _compact(text).casefold() for term in ("レシート", "receipt", "お買上")):
        status = "not_receipt"
    elif duplicate_state == "exact":
        status = "duplicate"
    elif issues:
        status = "needs_review"
    else:
        status = "ready"

    transaction = None
    reconciliation_status = "not_checked" if existing is None else "no_match"
    reconciliation_ids: tuple[str, ...] = ()
    if selected_date is not None and selected_merchant is not None and selected_total is not None:
        row_status = "解析済" if status == "ready" else "要確認"
        note = "; ".join(issues)
        transaction = (
            import_id, "", "receipt", source_id, selected_date, selected_merchant,
            selected_total, "", row_status, "", digest, note,
        )
        if existing is not None:
            hypothetical = ImportTransaction(
                0, import_id, "receipt", str(selected_date), str(selected_merchant), int(selected_total),
                row_status, "", note, list(transaction), "",
            )
            decisions = reconcile_transactions([*existing, hypothetical])
            relevant = [decision for decision in decisions if decision.transaction.import_id == import_id]
            if not relevant:
                relevant = [
                    decision for decision in decisions
                    if import_id in decision.candidate_ids or decision.target_id == import_id
                ]
            if relevant:
                reconciliation_status = relevant[0].status
                reconciliation_ids = tuple(dict.fromkeys(
                    candidate for decision in relevant
                    for candidate in (decision.transaction.import_id, *decision.candidate_ids)
                    if candidate != import_id
                ))

    confidence: Confidence = (
        "high" if status == "ready" else "medium" if status == "duplicate" else
        "low" if transaction is not None else "none"
    )
    return GeneralReceiptPreview(
        status, confidence, digest, extraction_method, classification,
        selected_date if isinstance(selected_date, str) else None,
        selected_merchant if isinstance(selected_merchant, str) else None,
        selected_total if isinstance(selected_total, int) else None,
        tuple(issues), len(dates), len(merchants), len(totals), duplicate_state,
        duplicate_ids, reconciliation_status, reconciliation_ids, transaction,
        1 if status == "ready" else 0,
    )


def _terminal_preview(
    status: PreviewStatus,
    digest: str,
    method: ReceiptOcrMethod,
    classification: str,
    issue: str,
) -> GeneralReceiptPreview:
    return GeneralReceiptPreview(
        status, "none", digest, method, classification, None, None, None, (issue,),
        0, 0, 0, "not_checked", (), "not_checked", (), None, 0,
    )


def preview_general_receipt_bytes(
    content: bytes,
    mime_type: str,
    *,
    source_id: str,
    existing_rows: list[list] | None = None,
) -> GeneralReceiptPreview:
    """Materialize and OCR one image/PDF locally, then return a read-only preview."""
    digest = hashlib.sha256(content).hexdigest()
    extracted = _extract_receipt_text(content, mime_type)
    if extracted.status != "extracted" or not extracted.text_present or extracted.text is None:
        return _terminal_preview(
            "extraction_failed", digest, extracted.method, "sensitive_unknown",
            extracted.status,
        )
    privacy = build_receipt_privacy_preview(
        extracted.text,
        extracted.structured_tokens,
        observation_complete=extracted.observation_complete,
    )
    if privacy.classification != "normal":
        return _terminal_preview(
            "privacy_blocked", digest, extracted.method, privacy.classification,
            privacy.reason_code,
        )
    return preview_general_receipt_text(
        extracted.text,
        source_id=source_id,
        source_sha256=digest,
        extraction_method=extracted.method,
        classification=privacy.classification,
        existing_rows=existing_rows,
        ocr_tokens=extracted.structured_tokens,
    )


def preview_general_receipt_observation(
    observation: OcrObservation,
    *,
    source_id: str,
    expected_sha256: str,
    existing_rows: list[list] | None = None,
) -> GeneralReceiptPreview:
    """Consume the existing engine-neutral OCR envelope, not Medical policy."""
    if (
        type(observation) is not OcrObservation
        or not observation.complete
        or observation.unit_id != source_id
        or observation.page != 1
        or observation.image_sha256 != expected_sha256
    ):
        return _terminal_preview(
            "extraction_failed", expected_sha256, "rapidocr", "sensitive_unknown",
            "observation_incomplete",
        )
    ordered = sorted(
        observation.regions,
        key=lambda region: (
            region.bbox[1] if region.bbox is not None else float("inf"),
            region.bbox[0] if region.bbox is not None else float("inf"),
            region.ordinal,
        ),
    )
    text = "\n".join(region.text.strip() for region in ordered if region.text.strip())
    privacy = build_receipt_privacy_preview(text)
    if privacy.classification != "normal":
        return _terminal_preview(
            "privacy_blocked", expected_sha256, "rapidocr", privacy.classification,
            privacy.reason_code,
        )
    return preview_general_receipt_text(
        text, source_id=source_id, source_sha256=expected_sha256,
        extraction_method="rapidocr", classification="normal", existing_rows=existing_rows,
    )


class GeneralReceiptPreviewPipeline:
    """Optional read-only Sheets connection for duplicate/reconciliation preview."""

    def __init__(self, db=None, *, rapidocr_adapter=None):
        self.db = db
        self.rapidocr_adapter = rapidocr_adapter

    def preview_bytes(self, content: bytes, mime_type: str, *, source_id: str) -> GeneralReceiptPreview:
        rows = self.db.get("取込データ!A2:L") if self.db is not None else None
        normalized_mime = (mime_type or "").strip().lower()
        if self.rapidocr_adapter is not None and normalized_mime in {
            "image/png", "image/jpeg", "image/jpg",
        }:
            digest = hashlib.sha256(content).hexdigest()
            try:
                observation = self.rapidocr_adapter.observe(ReceiptImage(source_id, 1, content))
            except Exception:
                return _terminal_preview(
                    "extraction_failed", digest, "rapidocr", "sensitive_unknown",
                    "observation_incomplete",
                )
            return preview_general_receipt_observation(
                observation, source_id=source_id, expected_sha256=digest,
                existing_rows=rows,
            )
        return preview_general_receipt_bytes(
            content, mime_type, source_id=source_id, existing_rows=rows,
        )
