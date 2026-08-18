from __future__ import annotations

import hashlib
from dataclasses import dataclass

from .reconciliation import ImportTransaction, parse_import_rows
from .sheets import CATEGORY_SEPARATOR, HEADERS, SheetsDB


@dataclass(frozen=True)
class ReviewItem:
    transaction: ImportTransaction
    priority: str
    recommendation: str


def is_reviewable_status(status: str) -> bool:
    return (
        status == "要確認"
        or status.startswith("needs_review")
        or status.endswith("_needs_review")
        or status in {"unclassified_aupay", "unclassified_card", "amazon_unmatched"}
    )


def selected_category_pair(major: str, minor: str) -> tuple[str, str]:
    major = major.strip()
    minor = minor.strip()
    if CATEGORY_SEPARATOR in major:
        combined_major, combined_minor = major.split(CATEGORY_SEPARATOR, 1)
        return combined_major.strip(), combined_minor.strip()
    return major, minor


def review_items(transactions: list[ImportTransaction]) -> list[ReviewItem]:
    items=[]
    for tx in transactions:
        status=tx.status
        if status == "要確認" or status.startswith("needs_review") or status.endswith("_needs_review"):
            priority="高"
            if status == "amazon_needs_review":
                recommendation="Amazon注文との重複候補を確認"
            elif tx.source == "receipt":
                recommendation="レシート画像・合計・カテゴリを確認"
            else:
                recommendation="重複候補を確認し、統合先を選択"
        elif status == "unclassified_aupay":
            priority="中"
            recommendation="レシート有無とカテゴリを確認"
        elif status == "unclassified_card":
            priority="中"
            recommendation="レシート・Amazon・au PAYとの重複を確認"
        elif status == "amazon_unmatched":
            priority="中"
            recommendation="Amazon注文履歴に一致なし。注文履歴の不足または請求内訳を確認"
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
        categories=self.db.categories()
        self.db.ensure_sheet("要確認",HEADERS["要確認"])
        existing={r[0]:list(r[9:15])+[""]*max(0,6-len(r[9:15]))
                  for r in self.db.get("要確認!A2:O") if r}
        self.db.clear("要確認!A2:O")
        rows=[]
        for item in items:
            tx=item.transaction
            manual=existing.get(tx.import_id,[""]*6)[:6]
            display_date=tx.date.replace("-","/") if tx.date else ""
            rows.append([tx.import_id,item.priority,display_date,tx.source,tx.merchant,tx.amount,
                         tx.status,item.recommendation,tx.note]+manual)
        self.db.append("要確認",rows)
        self.db.configure_review_validation(len(rows),categories)
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


class ReviewApprovalPipeline:
    ACTIONS={"支出として計上","重複として除外","レシートと統合","保留"}

    def __init__(self,db:SheetsDB): self.db=db

    @staticmethod
    def _expense_id(import_id:str)->str:
        return "M-"+hashlib.sha256(import_id.encode("utf-8")).hexdigest()[:24]

    def apply(self)->dict:
        imports=parse_import_rows(self.db.get("取込データ!A2:L"))
        by_id={tx.import_id:tx for tx in imports}
        categories=set(self.db.categories())
        expense_idx=self.db.expense_index()
        review_rows=self.db.get("要確認!A2:O")
        import_updates=[]; expense_new=[]; expense_updates=[]; review_updates=[]
        stats={"requested":0,"applied":0,"held":0,"errors":0,
               "expenses_created":0,"expenses_excluded":0}
        for row_num,raw in enumerate(review_rows,start=2):
            row=list(raw)+[""]*max(0,15-len(raw)); row=row[:15]
            action=str(row[9]).strip()
            if not action: continue
            stats["requested"]+=1
            error=""
            tx=by_id.get(str(row[0]))
            if action not in self.ACTIONS: error="許可されていない判断です"
            elif action=="保留":
                row[14]="保留"; stats["held"]+=1; review_updates.append((row_num,row)); continue
            elif tx is None: error="元の取込データが見つかりません"
            elif not is_reviewable_status(tx.status):
                error=f"既に処理済みです: {tx.status}"
            if error:
                row[14]="エラー: "+error; stats["errors"]+=1; review_updates.append((row_num,row)); continue

            target=""; new_status=""
            if action=="支出として計上":
                pair=selected_category_pair(str(row[11]),str(row[12]))
                if pair not in categories:
                    row[14]="エラー: カテゴリマスタに存在する大・小カテゴリを選択してください"
                    stats["errors"]+=1; review_updates.append((row_num,row)); continue
                expense_id=self._expense_id(tx.import_id); target=expense_id; new_status="manual_expense"
                expense=[expense_id,tx.date,tx.merchant,"手動計上",tx.amount,pair[0],pair[1],
                         tx.row[7],tx.source,"",tx.import_id,str(row[13]).strip(),"active"]
                if expense_id in expense_idx: expense_updates.append((expense_idx[expense_id],expense))
                else: expense_new.append(expense)
                stats["expenses_created"]+=1
            elif action=="重複として除外":
                target=str(row[10]).strip(); new_status="manual_duplicate_excluded"
                for expense_row_num,expense_raw in self.db.expense_rows_for_import(tx.import_id):
                    expense=list(expense_raw)+[""]*max(0,13-len(expense_raw)); expense=expense[:13]
                    expense[12]="duplicate_excluded"
                    expense_updates.append((expense_row_num,expense)); stats["expenses_excluded"]+=1
            elif action=="レシートと統合":
                target=str(row[10]).strip(); receipt=by_id.get(target)
                if receipt is None or receipt.source!="receipt":
                    row[14]="エラー: 実在するレシートの取込IDを入力してください"
                    stats["errors"]+=1; review_updates.append((row_num,row)); continue
                new_status="matched_receipt"

            updated=list(tx.row); updated[8]=new_status; updated[9]=target
            manual_note=str(row[13]).strip()
            annotation=f"スマホ判断={action}"+(f"; {manual_note}" if manual_note else "")
            updated[11]="; ".join(x for x in (tx.note,annotation) if x)
            import_updates.append((tx.row_num,updated))
            row[14]="反映済み"; review_updates.append((row_num,row)); stats["applied"]+=1

        if expense_new or expense_updates: self.db.ensure_expense_status_column()
        self.db.append("支出明細",expense_new)
        self.db.update_rows("支出明細",expense_updates)
        self.db.update_rows("取込データ",import_updates)
        self.db.update_rows("要確認",review_updates)
        return stats
