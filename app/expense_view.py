from __future__ import annotations

from dataclasses import dataclass

from .sheets import HEADERS, SheetsDB


@dataclass(frozen=True)
class ActiveExpense:
    expense_id:str
    date:str
    merchant:str
    item_name:str
    amount:int
    major_category:str
    minor_category:str
    payment_method:str
    source:str
    note:str


def active_expenses(rows:list[list])->list[ActiveExpense]:
    expenses=[]
    for raw in rows:
        row=list(raw)+[""]*max(0,13-len(raw))
        if not row[0] or (row[12] and row[12] != "active"):
            continue
        try: amount=int(float(str(row[4]).replace(",","")))
        except (TypeError,ValueError): amount=0
        expenses.append(ActiveExpense(
            expense_id=str(row[0]),date=str(row[1]),merchant=str(row[2]),item_name=str(row[3]),
            amount=amount,major_category=str(row[5]),minor_category=str(row[6]),
            payment_method=str(row[7]),source=str(row[8]),note=str(row[11]),
        ))
    def key(expense:ActiveExpense):
        digits=expense.date.replace("-","").replace("/","")
        date_number=int(digits) if digits.isdigit() else 0
        return (-date_number,expense.source,expense.expense_id)
    return sorted(expenses,key=key)


class ExpenseViewPipeline:
    def __init__(self,db:SheetsDB): self.db=db

    def preview(self)->dict:
        expenses=active_expenses(self.db.get("支出明細!A2:M"))
        return self._summary(expenses)

    def refresh(self)->dict:
        expenses=active_expenses(self.db.get("支出明細!A2:M"))
        self.db.ensure_sheet("支出一覧",HEADERS["支出一覧"])
        self.db.clear("支出一覧!A2:J")
        rows=[[x.date.replace("-","/"),x.merchant,x.item_name,x.amount,x.major_category,
               x.minor_category,x.payment_method,x.source,x.note,x.expense_id] for x in expenses]
        self.db.append("支出一覧",rows)
        self.db.format_date_column("支出一覧",0)
        result=self._summary(expenses); result["refreshed"]=True
        return result

    @staticmethod
    def _summary(expenses:list[ActiveExpense])->dict:
        return {"active_expenses":len(expenses),"total":sum(x.amount for x in expenses)}
