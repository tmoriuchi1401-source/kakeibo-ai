from __future__ import annotations

import csv
import hashlib
from collections import Counter, defaultdict, deque
from datetime import datetime

from .sheets import SheetsDB
from .utils import canonical_hash, normalize_store, now_jst_string


def parse_aupay_csv(path: str) -> list[dict]:
    with open(path, encoding="cp932", newline="") as f:
        rows = list(csv.reader(f))
    header_i = next((i for i, row in enumerate(rows)
                     if "利用日時" in row and "利用店舗" in row
                     and "種別" in row and "利用額（円）" in row), None)
    if header_i is None:
        raise ValueError("au PAY CSVの利用明細ヘッダーを見つけられません")
    header = rows[header_i]
    pos = {name: header.index(name) for name in
           ["利用日時", "利用店舗", "種別", "利用額（円）", "備考"]}
    parsed = []
    for row in rows[header_i + 1:]:
        if not row or (row[0].startswith("■") if row[0] else False):
            break
        if len(row) <= max(pos.values()) or not row[pos["利用日時"]]:
            continue
        used_at = datetime.strptime(row[pos["利用日時"]].strip(), "%Y/%m/%d %H:%M")
        amount = int(row[pos["利用額（円）"]].replace(",", "").replace("円", "").strip())
        parsed.append({
            "date": used_at.strftime("%Y-%m-%d"),
            "used_at": used_at.strftime("%Y-%m-%d %H:%M"),
            "merchant": row[pos["利用店舗"]].strip(),
            "kind": row[pos["種別"]].strip(),
            "amount": amount,
            "note": row[pos["備考"]].strip(),
        })
    seen = Counter()
    for item in parsed:
        base = "|".join(str(item[key]) for key in
                        ["used_at", "merchant", "kind", "amount", "note"])
        seen[base] += 1
        item["occurrence"] = seen[base]
        item["import_id"] = "aupaycsv:" + hashlib.sha256(
            f"{base}|{seen[base]}".encode()).hexdigest()[:24]
    return parsed


def payment_signature(date: str, merchant: str, amount: int) -> tuple[str, str, int]:
    return date, normalize_store(merchant), amount


class AuPayCsvPipeline:
    def __init__(self, db: SheetsDB):
        self.db = db

    def _plan(self, path: str) -> tuple[list[list], list[tuple[int, list]], dict]:
        transactions = parse_aupay_csv(path)
        existing_rows = self.db.get("取込データ!A2:L")
        existing_ids = {str(row[0]) for row in existing_rows if row}
        existing_payments = defaultdict(deque)
        for row_num, raw in enumerate(existing_rows, start=2):
            row = list(raw) + [""] * max(0, 12 - len(raw))
            if (row[2] == "au PAY" and row[8] != "transfer_aupay_charge"
                    and not str(row[0]).startswith("aupaycsv:")):
                signature = payment_signature(
                    str(row[4]), str(row[5]), int(float(str(row[6]).replace(",", ""))))
                existing_payments[signature].append((row_num, row[:12]))
        rows = []
        updates = []
        stats = {"source_rows": len(transactions), "new": 0, "unchanged": 0,
                 "covered_by_existing": 0, "confirmed_existing": 0,
                 "payments": 0, "autocharges": 0}
        for item in transactions:
            is_payment = item["kind"] == "支払い"
            if is_payment:
                signature = payment_signature(item["date"], item["merchant"], item["amount"])
                if existing_payments[signature]:
                    row_num, existing = existing_payments[signature].popleft()
                    stats["covered_by_existing"] += 1
                    marker = f"CSV確認済={item['used_at'][:7].replace('-', '')}"
                    if marker not in str(existing[11]):
                        existing[11] = "; ".join(x for x in (str(existing[11]), marker) if x)
                        updates.append((row_num, existing))
                        stats["confirmed_existing"] += 1
                    continue
            if item["import_id"] in existing_ids:
                stats["unchanged"] += 1
                continue
            if is_payment:
                status = "unclassified_aupay"
                stats["payments"] += 1
            elif item["kind"] == "オートチャージ":
                status = "transfer_aupay_charge"
                stats["autocharges"] += 1
            else:
                status = "needs_review_aupay_csv"
            note = f"CSV種別={item['kind']}; 利用日時={item['used_at']}"
            if item["note"]:
                note += f"; 備考={item['note']}"
            rows.append([
                item["import_id"], now_jst_string(), "au PAY", item["import_id"],
                item["date"], item["merchant"], item["amount"], "au PAY", status,
                "", canonical_hash(item), note,
            ])
            stats["new"] += 1
        return rows, updates, stats

    def preview(self, path: str) -> dict:
        return self._plan(path)[2]

    def import_csv(self, path: str) -> dict:
        rows, updates, stats = self._plan(path)
        self.db.append("取込データ", rows)
        self.db.update_rows("取込データ", updates)
        return stats
