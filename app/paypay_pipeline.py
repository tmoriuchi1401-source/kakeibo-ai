from __future__ import annotations

import csv
import io
import re
import unicodedata
from datetime import datetime
from pathlib import Path

from .sheets import SheetsDB
from .utils import canonical_hash, now_jst_string


_REQUIRED_COLUMNS = (
    "取引日",
    "出金金額(円)",
    "取引内容",
    "取引先",
    "取引方法",
    "取引番号",
)


def _normalize(value: str) -> str:
    return unicodedata.normalize("NFKC", str(value or "")).strip()


def _decode_csv(path: str | Path) -> str:
    data = Path(path).read_bytes()
    for encoding in ("utf-8-sig", "cp932"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ValueError("PayPay CSVの文字コードを判定できません")


def _parse_amount(value: str) -> int:
    normalized = _normalize(value)
    digits = re.sub(r"[^0-9]", "", normalized)
    if not digits:
        raise ValueError(f"PayPay CSVの支払い金額が不正です: {value!r}")
    return int(digits)


def _parse_date(value: str) -> str:
    normalized = _normalize(value)
    for pattern in (
        "%Y/%m/%d %H:%M:%S",
        "%Y/%m/%d %H:%M",
        "%Y/%m/%d",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d",
    ):
        try:
            return datetime.strptime(normalized, pattern).strftime("%Y-%m-%d")
        except ValueError:
            continue
    raise ValueError(f"PayPay CSVの取引日が不正です: {value!r}")


def _read_paypay_rows(path: str | Path) -> list[dict[str, str]]:
    rows = list(csv.reader(io.StringIO(_decode_csv(path)), skipinitialspace=False))
    header_index = None
    normalized_header: list[str] = []
    for index, row in enumerate(rows):
        candidate = [_normalize(value) for value in row]
        if "取引内容" in candidate and "取引番号" in candidate:
            header_index = index
            normalized_header = candidate
            break
    if header_index is None:
        raise ValueError("PayPay CSVのヘッダーを見つけられません")

    missing = [name for name in _REQUIRED_COLUMNS if name not in normalized_header]
    if missing:
        raise ValueError(f"PayPay CSVの必須列がありません: {', '.join(missing)}")

    result = []
    for row in rows[header_index + 1:]:
        if not row or not any(_normalize(value) for value in row):
            continue
        padded = row + [""] * (len(normalized_header) - len(row))
        result.append({name: _normalize(padded[index])
                       for index, name in enumerate(normalized_header)})
    return result


def parse_paypay_csv(path: str | Path) -> list[dict[str, str | int]]:
    payments = []
    for row_number, row in enumerate(_read_paypay_rows(path), start=2):
        if row["取引内容"] != "支払い":
            continue
        transaction_id = row["取引番号"]
        if not transaction_id:
            raise ValueError(f"PayPay CSVの取引番号が空です（データ行 {row_number}）")
        payments.append({
            "date": _parse_date(row["取引日"]),
            "merchant": row["取引先"],
            "amount": _parse_amount(row["出金金額(円)"]),
            "payment_type": row["取引方法"],
            "transaction_id": transaction_id,
            "import_id": f"paypay:{transaction_id}",
            "payment_category": row.get("支払い区分", ""),
            "user": row.get("利用者", ""),
        })
    return payments


def inspect_paypay_csv(path: str | Path) -> dict[str, object]:
    """Inspect every exported history row without applying import filtering."""
    rows = _read_paypay_rows(path)
    dates = [_parse_date(row["取引日"]) for row in rows]
    return {
        "row_count": len(rows),
        "observed_start": min(dates) if dates else None,
        "observed_end": max(dates) if dates else None,
        "transaction_kinds": sorted({row["取引内容"] for row in rows}),
    }


class PayPayPipeline:
    def __init__(self, db: SheetsDB | None = None):
        self.db = db

    def preview(self, path: str | Path, sample_limit: int = 5) -> dict:
        rows = _read_paypay_rows(path)
        payments = parse_paypay_csv(path)
        return {
            "summary": {
                "rows": len(rows),
                "payments": len(payments),
                "non_payments": len(rows) - len(payments),
                "payment_total": sum(int(item["amount"]) for item in payments),
            },
            "payment_samples": payments[:max(0, sample_limit)],
        }

    def import_csv(self, path: str | Path) -> dict:
        if self.db is None:
            raise ValueError("PayPay importにはSheetsDBが必要です")
        payments = parse_paypay_csv(path)
        existing = self.db.import_ids()
        rows = []
        stats = {
            "source_rows": len(payments),
            "new": 0,
            "unchanged": 0,
            "unclassified_paypay": 0,
        }
        for payment in payments:
            import_id = str(payment["import_id"])
            if import_id in existing:
                stats["unchanged"] += 1
                continue
            note_parts = []
            if payment["payment_category"]:
                note_parts.append(f"支払い区分={payment['payment_category']}")
            if payment["user"]:
                note_parts.append(f"利用者={payment['user']}")
            rows.append([
                import_id,
                now_jst_string(),
                "PayPay",
                payment["transaction_id"],
                payment["date"],
                payment["merchant"],
                payment["amount"],
                payment["payment_type"],
                "unclassified_paypay",
                "",
                canonical_hash(payment),
                "; ".join(note_parts),
            ])
            existing.add(import_id)
            stats["new"] += 1
            stats["unclassified_paypay"] += 1
        self.db.append("取込データ", rows)
        return stats
