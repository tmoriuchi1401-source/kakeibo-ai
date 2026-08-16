from __future__ import annotations

from dataclasses import dataclass

from .reconciliation import ImportTransaction, parse_import_rows
from .sheets import HEADERS, SheetsDB


@dataclass(frozen=True)
class ReviewItem:
    transaction: ImportTransaction
    priority: str
    recommendation: str


def review_items(transactions: list[ImportTransaction]) -> list[ReviewItem]:
    items=[]
    for tx in transactions:
        status=tx.status
        if status == "要確認" or status.startswith("needs_review"):
            priority="高"
            if tx.source == "receipt":
                recommendation="レシート画像・合計・カテゴリを確認"
            else:
                recommendation="重複候補を確認し、統合先を選択"
        elif status == "unclassified_aupay":
            priority="中"
            recommendation="レシート有無とカテゴリを確認"
        elif status == "unclassified_card":
            priority="中"
            recommendation="レシート・Amazon・au PAYとの重複を確認"
        else:
            continue
        items.append(ReviewItem(tx,priority,recommendation))
    # High priority first, then newest date first, then stable import ID.
    def sort_key(item:ReviewItem):
        digits=item.transaction.date.replace("-","")
        date_number=int(digits) if digits.isdigit() else 0
        return (0 if item.priority=="高" else 1,-date_number,item.transaction.import_id)
    return sorted(items,key=sort_key)


class ReviewPipeline:
    def __init__(self,db:SheetsDB):
        self.db=db

    def preview(self)->dict:
        tx=parse_import_rows(self.db.get("取込データ!A2:L"))
        items=review_items(tx)
        return self._summary(items)

    def refresh(self)->dict:
        tx=parse_import_rows(self.db.get("取込データ!A2:L"))
        items=review_items(tx)
        self.db.ensure_sheet("要確認",HEADERS["要確認"])
        self.db.clear("要確認!A2:I")
        rows=[]
        for item in items:
            tx=item.transaction
            rows.append([tx.import_id,item.priority,tx.date,tx.source,tx.merchant,tx.amount,
                         tx.status,item.recommendation,tx.note])
        self.db.append("要確認",rows)
        result=self._summary(items)
        result["refreshed"]=True
        return result

    @staticmethod
    def _summary(items:list[ReviewItem])->dict:
        return {
            "review_rows":len(items),
            "high":sum(x.priority=="高" for x in items),
            "medium":sum(x.priority=="中" for x in items),
        }
