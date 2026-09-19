"""The monetary decision form on the existing daily confirmation tab."""
from hashlib import sha256
import json
from uuid import uuid4

from .amazon_money import MoneyError
from .amazon_money_review import ACTIONS, ERRORS, MoneyReviews
from .daily_view import STATE_LABELS, cells, dropdown, grid, SHEETS

MARKER="kakeibo_daily_money_form_v1"


def install_requests(source_id):
    """For a fresh daily copy or an explicitly checked blank upgrade range."""
    rows=[["Amazonの金銭確認","原本を確認して対応を選びます"],["対象（金銭ID・通知ID）",""],["対応",""],
          ["返金元の金銭ID",""],["既存の支出ID",""],["送信",False],["反映状況","未送信"],["最終更新",""],
          ["案内","支出IDはカンマ区切り。混合払い・未確定は直接計上できません。MN通知は保留・確認済みだけで記帳しません。"]]
    requests=[cells("確認",80,rows,width=4),cells("確認",81,[["REQ-"+uuid4().hex]],left=9,width=1),
        dropdown("確認",82,1,list(ACTIONS)),
        {"setDataValidation":{"range":grid("確認",85,85,1,2),"rule":{
            "condition":{"type":"BOOLEAN"},"strict":True,"showCustomUi":True}}},
        {"repeatCell":{"range":grid("確認",80,88,0,4),"cell":{"userEnteredFormat":{
            "wrapStrategy":"WRAP","verticalAlignment":"MIDDLE","textFormat":{"fontSize":11}}},
            "fields":"userEnteredFormat(wrapStrategy,verticalAlignment,textFormat)"}},
        {"repeatCell":{"range":grid("確認",81,85,1,4),"cell":{"userEnteredFormat":{
            "backgroundColor":{"red":1,"green":0.98,"blue":0.85}}},"fields":"userEnteredFormat.backgroundColor"}},
        {"updateDimensionProperties":{"range":{"sheetId":SHEETS["確認"][0],"dimension":"ROWS","startIndex":79,"endIndex":88},
            "properties":{"pixelSize":65},"fields":"pixelSize"}}]
    requests.extend({"mergeCells":{"range":grid("確認",r,r,1,4),"mergeType":"MERGE_ALL"}} for r in range(80,89))
    requests.append({"createDeveloperMetadata":{"developerMetadata":{"metadataKey":MARKER,
        "metadataValue":sha256(source_id.encode()).hexdigest(),"visibility":"DOCUMENT","location":{"spreadsheet":True}}}})
    return requests


def upgrade_requests(daily):
    """Locked migration planning; refuse to overwrite any existing user text."""
    meta=daily.verify()
    if any(m.get("metadataKey")==MARKER for m in meta.get("developerMetadata",[])):
        DailyMoneyForm(daily,None).verify()
        return []
    existing=daily.read_ranges(["'確認'!A80:D88","'確認'!J81:J81"],formulas=True)
    if any(value!="" for block in existing for row in block for value in row):
        raise MoneyError("money_review_upgrade_range_occupied")
    return install_requests(daily.source_id)


class DailyMoneyForm:
    def __init__(self,daily,inbox):self.daily,self.inbox=daily,inbox

    def verify(self):
        meta=self.daily.verify()
        if not meta or not any(m.get("metadataKey")==MARKER and m.get("metadataValue")==sha256(self.daily.source_id.encode()).hexdigest()
                               for m in meta.get("developerMetadata",[])):
            raise MoneyError("money_review_form_migration_required")

    def form(self):
        blocks=self.daily.read_ranges(["'確認'!B81:B85","'確認'!J81:J81"])
        values=[r[0] if r else "" for r in blocks[0]]
        values += [""]*(5-len(values))
        token=blocks[1][0][0] if blocks[1] and blocks[1][0] else ""
        return values,token

    def submit(self):
        self.verify()
        values,token=self.form()
        old=self.inbox.read()["requests"].get(token)
        if values[4] is not True and not old:return {"money_reviews_submitted":0}
        form_digest=sha256(json.dumps(values[:4],ensure_ascii=False,sort_keys=True).encode()).hexdigest()
        new=old is None
        changed=bool(old and old.get("form_digest")!=form_digest)
        if new:
            money_id=str(values[0]).split("｜",1)[0].strip()
            try:
                old=self.inbox.prepare(token,money_id,ACTIONS.get(values[1],""),related_id=str(values[2]).strip(),
                    expense_ids=tuple(x.strip() for x in str(values[3]).replace("、",",").split(",") if x.strip()),form_digest=form_digest)
            except MoneyError as error:
                if str(error) not in ERRORS:raise
                old=self.inbox.reject(token,money_id,str(error),form_digest=form_digest)
        if old["state"] in {"queued","pending"}:
            self.daily.db.svc.spreadsheets().batchUpdate(spreadsheetId=self.daily.db.sid,body={"requests":[
                cells("確認",86,[[STATE_LABELS[old["state"]]],[old["updated_at"]]],left=1,width=1)]}).execute(num_retries=0)
        result=self.inbox.apply(token)
        if self.form()!=(values,token):return {"money_reviews_submitted":int(new),"money_review_input_changed":1}
        label=STATE_LABELS[result["state"]]
        if changed:label+="・変更した入力は未送信です"
        if result.get("error") in ERRORS:label+="・"+ERRORS[result["error"]]
        token="REQ-"+uuid4().hex
        requests=[cells("確認",86,[[label],[result["updated_at"]]],left=1,width=1),
                  cells("確認",85,[[False]],left=1,width=1),cells("確認",81,[[token]],left=9,width=1)]
        self.daily.db.svc.spreadsheets().batchUpdate(spreadsheetId=self.daily.db.sid,body={"requests":requests}).execute(num_retries=0)
        observed=self.daily.read_ranges(["'確認'!B86:B87","'確認'!B85:B85","'確認'!J81:J81"])
        if observed!=[[[label],[result["updated_at"]]],[[False]],[[token]]]:raise MoneyError("money_review_ack_unknown")
        return {"money_reviews_submitted":int(new)}


def run_money_reviews(daily,source,env,*,apply):
    from .amazon_money_runtime import money_enabled,money_writer
    if not money_enabled(env):return {}
    writer=money_writer(source,env)
    inbox=MoneyReviews(writer);form=DailyMoneyForm(daily,inbox)
    form.verify()
    before=inbox.read()["requests"]
    if not apply:return {"money_reviews_pending":sum(x["state"] in {"queued","pending"} for x in before.values()),
        "money_review_form_ready":int(form.form()[0][4] is True)}
    if hasattr(writer,"prepare"):writer.prepare()
    inbox.apply_pending()
    remaining=sum(x["state"] in {"queued","pending"} for x in inbox.read()["requests"].values())
    result=form.submit() if not remaining else {"money_reviews_submitted":0}
    after=inbox.read()["requests"]
    for state in ("applied","failed"):
        result["money_reviews_"+state]=sum(v["state"]==state and before.get(k,{}).get("state")!=state for k,v in after.items())
    result["money_reviews_pending"]=sum(v["state"] in {"queued","pending"} for v in after.values())
    return result
