"""Idempotent fixed-money-ID posting through the existing ledger writer.

Run under the existing production lock/checkpoint. A durable monetary intent
precedes either ledger append; ambiguous appends are resolved by fixed IDs.
"""
from copy import deepcopy
from dataclasses import asdict, replace

from .amazon_money import MoneyError, decide, expense_rows, validate_book, source_alias
from .projection_store import replace_document
from .utils import now_jst_string


class MoneyLedger:
    def __init__(self,db):self.db=db

    def find(self,title,width,identities):
        if not identities:return {}
        # Fresh dimensions: never a fixed 5,000-row cutoff or stale cached grid.
        meta=self.db._execute_sheet_read(lambda:self.db.svc.spreadsheets().get(
            spreadsheetId=self.db.sid,fields="sheets(properties)"))
        sheet=next((s["properties"] for s in meta["sheets"] if s["properties"]["title"]==title),None)
        if sheet is None:raise MoneyError("money_ledger_missing")
        positions={}
        for start in range(2,sheet["gridProperties"]["rowCount"]+1,2000):
            end=min(start+1999,sheet["gridProperties"]["rowCount"])
            for offset,row in enumerate(self.db.get_raw(f"'{title}'!A{start}:A{end}")):
                if not row or row[0] not in identities:continue
                if row[0] in positions:raise MoneyError("money_duplicate_ledger_identity")
                positions[row[0]]=start+offset
        result={}
        for identity,position in positions.items():
            rows=self.db.get_raw(f"'{title}'!A{position}:{chr(64+width)}{position}")
            if len(rows)!=1 or rows[0][0]!=identity:raise MoneyError("money_ledger_identity_changed")
            result[identity]=(list(rows[0])+[""]*width)[:width]
        return result

    def append(self,title,rows):
        if rows:self.db.append_raw(title,rows)


class MoneyWriter:
    def __init__(self,store,ledger,possible_duplicates=None):
        self.store,self.ledger=store,ledger
        self.possible_duplicates=possible_duplicates or (lambda record:False)

    def _decision(self,record,book):
        decision=decide(record,book)
        if decision.action=="linked":
            alias=book["aliases"][source_alias(record)]
            expected=alias.get("expense_amounts",{})
            found=self.ledger.find("支出明細",13,set(decision.expense_ids))
            if (set(expected)!=set(decision.expense_ids) or set(found)!=set(expected)
                    or any(found[key][4]!=amount or found[key][12] not in {"","active"} for key,amount in expected.items())):
                return replace(decision,action="review",reason="legacy_ledger_binding_changed",expense_ids=())
        if decision.action=="post":
            if record.kind=="refund":
                origin=(book["legacy"].get(decision.related_id[7:],{}) if decision.related_id.startswith("legacy:")
                        else book["records"].get(decision.related_id,{}))
                ids=set(origin.get("expense_ids",[]))
                found=self.ledger.find("支出明細",13,ids)
                alias=book["aliases"].get(origin.get("source","")+":"+origin.get("source_id",""),{})
                expected=alias.get("expense_amounts",origin.get("expense_amounts"))
                from .monthly_projection import _yen,ProjectionError
                try:total=sum(_yen(row[4]) for row in found.values())
                except ProjectionError:total=None
                if (not ids or set(found)!=ids or any(row[12] not in {"","active"} for row in found.values())
                        or (any(found[k][4]!=expected.get(k) for k in ids) if expected is not None else total!=origin.get("amount"))):
                    return replace(decision,action="review",reason="refund_origin_ledger_changed",expense_ids=())
                # A legacy purchase and its split-charge aliases share one
                # canonical balance, even when the refund selects different IDs.
                refunded=0
                for key,prior in book["records"].items():
                    if key==record.money_id or prior.get("kind")!="refund" or prior.get("state") not in {"pending","posted","linked"}:continue
                    related=prior.get("related_id","")
                    other=(book["legacy"].get(related[7:],{}) if related.startswith("legacy:") else book["records"].get(related,{}))
                    if ids & set(other.get("expense_ids",[])):refunded-=prior["amount"]
                if total is None or refunded-record.amount>total:
                    return replace(decision,action="review",reason="refund_exceeds_purchase",expense_ids=())
            if self.ledger.find("取込データ",12,{record.source_id}):
                return replace(decision,action="review",reason="source_identity_already_imported",expense_ids=())
            resolution=book["records"].get(record.money_id,{}).get("resolution",{})
            if self.possible_duplicates(record) and resolution.get("action")!="separate":
                return replace(decision,action="review",reason="existing_expense_identity_required",expense_ids=())
        return decision

    def _book(self):
        book=self.store.read("money")
        validate_book(book)
        return book

    def preview(self,records):
        book=self._book()
        return [self._decision(record,book) for record in records]

    def verify_posted(self,record):
        """Verify canonical money on a canary replay, not just a saved flag."""
        entry=self._book()["records"].get(record.money_id,{})
        if entry.get("state")!="posted" or entry.get("fingerprint")!=record.fingerprint:
            raise MoneyError("money_canary_posting_unconfirmed")
        imported=self.ledger.find("取込データ",12,{record.money_id}).get(record.money_id)
        expected={0:record.money_id,3:record.reference,4:record.day,6:record.amount,7:record.account,
                  8:"canonical_amazon_money",9:record.money_id,10:record.fingerprint}
        if imported is None or any(imported[k]!=v for k,v in expected.items()):raise MoneyError("money_canary_import_changed")
        ids=set(entry.get("expense_ids",[]));rows=self.ledger.find("支出明細",13,ids)
        from .monthly_projection import _yen
        if (not ids or set(rows)!=ids or sum(_yen(r[4]) for r in rows.values())!=record.amount
                or any(r[1]!=record.day or r[10]!=record.money_id or r[12] not in {"","active"} for r in rows.values())):
            raise MoneyError("money_canary_expense_changed")

    def apply(self,records,*,limit):
        if not 1<=limit<=100:raise MoneyError("money_batch_limit_invalid")
        if len(records)>limit:raise MoneyError("money_batch_limit_exceeded")
        stats={"money_posted":0,"money_linked":0,"money_review":0,"money_duplicate":0,
               "money_supplement":0,"money_transfer":0,"expense_rows_written":0,"import_rows_written":0}
        for record in records:
            before=self._book()
            decision=self._decision(record,before)
            if decision.action=="duplicate":stats["money_duplicate"]+=1;continue
            previous=before["records"].get(record.money_id)
            if (decision.action=="review" and previous and previous.get("state")=="review"
                    and previous.get("reason")==decision.reason and previous.get("confirmed")==record.confirmed):
                stats["money_review"]+=1;continue
            if decision.action=="resume":
                intent=before["records"][record.money_id]
            else:
                intent={k:v for k,v in asdict(record).items() if k!="items"}
                if previous and previous.get("resolution"):intent["resolution"]=deepcopy(previous["resolution"])
                if decision.action=="review":intent["items"]=[asdict(item) for item in record.items]
                intent.update(fingerprint=record.fingerprint,state="pending" if decision.action=="post" else decision.action,
                    reason=decision.reason,expense_ids=list(decision.expense_ids),related_id=decision.related_id,
                    updated_at=now_jst_string())
                if decision.action=="post":
                    intent["expense_rows"]=expense_rows(decision)
                    intent["import_row"]=[record.money_id,intent["updated_at"],
                        "au PAYカード" if record.source=="au_pay_card" else "Amazon金銭",record.reference,
                        record.day,"Amazon",record.amount,record.account,"canonical_amazon_money",
                        record.money_id,record.fingerprint,"確定金銭; 原本="+record.original_url]
                after=deepcopy(before);after["records"][record.money_id]=intent
                replace_document(self.store,"money",before,after)
                before=after
            if decision.action not in {"post","resume"}:
                stats["money_"+decision.action]+=1;continue
            expected={"取込データ":[intent["import_row"]],"支出明細":intent["expense_rows"]}
            for title,rows in expected.items():
                width=12 if title=="取込データ" else 13
                found=self.ledger.find(title,width,{row[0] for row in rows})
                if any(found[row[0]]!=row for row in rows if row[0] in found):
                    raise MoneyError("money_ledger_content_conflict")
                missing=[row for row in rows if row[0] not in found]
                if missing:
                    self.ledger.append(title,missing)
                    stats["import_rows_written" if title=="取込データ" else "expense_rows_written"]+=len(missing)
                actual=self.ledger.find(title,width,{row[0] for row in rows})
                if any(actual.get(row[0])!=row for row in rows):raise MoneyError("money_ledger_readback_failed")
            after=deepcopy(before)
            final=after["records"][record.money_id]
            final.update(state="posted",updated_at=now_jst_string())
            # Keep financial identity/link fields; remove duplicate ledger detail
            # once durable canonical readback has completed.
            final.pop("expense_rows",None);final.pop("import_row",None)
            replace_document(self.store,"money",before,after)
            stats["money_posted"]+=1
        return stats
