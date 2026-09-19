"""User-owned route/account completion; saved separately from projections.

Runs inside the existing daily writer lock. No import or accounting writes.
"""
from copy import deepcopy
from hashlib import sha256
import re
from uuid import uuid4

from .amazon_money import digest
from .monthly_projection import ProjectionError, month_key, shift_month
from .projection_store import replace_document, ProjectionJournal
from .utils import now_jst_string
from .daily_view import cells, dropdown, grid, SHEETS, STATE_LABELS

MARKER="kakeibo_daily_coverage_v1"
STATES={"未確認":"unknown","取込中":"partial","完了":"complete","対象外":"not_applicable"}
ERRORS={"coverage_month_invalid":"開始月・終了月はYYYY-MMで、当月以前の120か月以内を指定してください。",
        "coverage_route_invalid":"経路・口座をそれぞれ80文字以内で指定してください。",
        "coverage_status_invalid":"取込状況を選んでください。",
        "coverage_changed":"保存後に状況が変わりました。再確認して送信してください。"}


class Coverage:
    def __init__(self,store):self.store=store

    def read(self):
        value=self.store.read("coverage")
        if value is None:return {"routes":{},"months":{},"requests":{}}
        if not isinstance(value,dict) or set(value)!={"routes","months","requests"} or any(not isinstance(v,dict) for v in value.values()):
            raise ProjectionError("coverage_document_invalid")
        return value

    def prepare(self,token,values,*,current_month,form_digest):
        if not re.fullmatch(r"REQ-[a-f0-9]{32}",token):raise ProjectionError("coverage_token_invalid")
        before=self.store.read("coverage");doc=self.read()
        if token in doc["requests"]:return doc["requests"][token]
        if before is None:
            summary=self.store.read("summary") or {}
            if summary.get("required_routes") or summary.get("coverage"):
                raise ProjectionError("coverage_existing_input_migration_required")
        start,end,route,account,label=(str(v).strip() for v in values)
        error="";months=[]
        try:
            month_key(start);month_key(end)
            if start>end or end>current_month or shift_month(start,119)<end:raise ValueError()
            m=start
            while m<=end:months.append(m);m=shift_month(m,1)
        except (ValueError,ProjectionError):error="coverage_month_invalid"
        if not route or not account or max(len(route),len(account))>80 or any(ord(c)<32 for c in route+account):error="coverage_route_invalid"
        if label not in STATES:error="coverage_status_invalid"
        identity="RT-"+digest([route,account])[:32]
        now=now_jst_string()
        item={"state":"failed" if error else "queued","error":error,"route_id":identity,
              "route":{"route":route,"account":account},"months":months,"status":STATES.get(label),
              "snapshot":{m:doc["months"].get(m,{}).get(identity) for m in months},
              "form_digest":form_digest,"updated_at":now}
        doc["requests"][token]=item
        replace_document(self.store,"coverage",before,doc)
        return deepcopy(item)

    def sync(self):
        doc=self.read()
        if not doc["routes"]:return
        journal=ProjectionJournal(self.store).read()
        if journal["append"] or journal["ranges"] or journal["months"]:raise ProjectionError("coverage_projection_pending")
        before=self.store.read("summary")
        if before is None:raise ProjectionError("projection_bootstrap_required")
        after=deepcopy(before)
        after["required_routes"]=sorted(doc["routes"])
        after["coverage_routes"]=deepcopy(doc["routes"])
        after["coverage"]=deepcopy(doc["months"])
        zero_months=[]
        for month in before.get("coverage_zero_months",[]):
            states=doc["months"].get(month,{})
            total=after["months"].get(month,{})
            # Normal ledger projection replaces these values before sync. A
            # real purchase/refund must survive a coverage downgrade.
            empty=total.get("amount",0)==0 and total.get("purchase_count",0)==0 and total.get("refund_count",0)==0
            if empty and not all(states.get(k) in {"complete","not_applicable"} for k in doc["routes"]):
                after["months"].pop(month,None)
            elif empty:zero_months.append(month)
        indexed=None
        for month,states in doc["months"].items():
            complete=all(states.get(k) in {"complete","not_applicable"} for k in doc["routes"])
            if complete and month not in after["months"]:
                # Only a new explicit completion of an unmaterialized month
                # needs the index; ordinary display/replay never scans it.
                if indexed is None:
                    from .projection_refresh import load_index
                    index=self.store.read("index")
                    if index is None:raise ProjectionError("projection_bootstrap_required")
                    indexed=load_index(index)
                if indexed.by_month.get(month):raise ProjectionError("coverage_month_projection_missing")
                from .monthly_projection import rebuild_month
                from .projection_refresh import load_catalog, totals
                empty=rebuild_month(month,indexed,load_catalog(self.store.read("catalog")),lambda first,last:[])
                after["months"][month]=totals(empty)
                zero_months.append(month)
        after["coverage_zero_months"]=sorted(set(zero_months))
        replace_document(self.store,"summary",before,after)

    def apply(self,token):
        before=self.read();doc=deepcopy(before);item=doc["requests"][token]
        if item["state"] in {"applied","failed"}:return item
        if item["state"]=="queued":
            identity=item["route_id"]
            latest={m:doc["months"].get(m,{}).get(identity) for m in item["months"]}
            if latest!=item["snapshot"]:item.update(state="failed",error="coverage_changed")
            else:
                doc["routes"][identity]=item["route"]
                for month in item["months"]:doc["months"].setdefault(month,{})[identity]=item["status"]
                item["state"]="pending"
            item["updated_at"]=now_jst_string()
            replace_document(self.store,"coverage",before,doc)
        elif item["state"]!="pending":raise ProjectionError("coverage_request_invalid")
        if item["state"]=="pending":
            self.sync()
            before=self.read();doc=deepcopy(before);item=doc["requests"][token]
            item.update(state="applied",updated_at=now_jst_string())
            replace_document(self.store,"coverage",before,doc)
        return deepcopy(item)


def install_requests(source_id,*,current_month):
    rows=[["取込状況","月ごとの経路・口座"],["開始月",current_month],["終了月",current_month],
          ["経路",""],["口座",""],["状況","未確認"],["送信",False],["反映状況","未送信"],["最終更新",""],
          ["案内","完了は原本との照合後に選択。利用していない月は対象外。最大120か月をまとめて指定できます。"]]
    requests=[cells("設定",10,rows,width=3),cells("設定",11,[["REQ-"+uuid4().hex]],left=5,width=1),
        dropdown("設定",15,1,list(STATES)),
        {"setDataValidation":{"range":grid("設定",16,16,1,2),"rule":{"condition":{"type":"BOOLEAN"},"strict":True,"showCustomUi":True}}},
        cells("設定",22,[["状況を見る月",current_month],["ページ",1]],width=3),
        {"repeatCell":{"range":grid("設定",10,19),"cell":{"userEnteredFormat":{"wrapStrategy":"WRAP","verticalAlignment":"MIDDLE"}},"fields":"userEnteredFormat(wrapStrategy,verticalAlignment)"}},
        {"repeatCell":{"range":grid("設定",11,16,1,3),"cell":{"userEnteredFormat":{"backgroundColor":{"red":1,"green":0.98,"blue":0.85}}},"fields":"userEnteredFormat.backgroundColor"}},
        {"updateDimensionProperties":{"range":{"sheetId":SHEETS["設定"][0],"dimension":"ROWS","startIndex":9,"endIndex":19},"properties":{"pixelSize":65},"fields":"pixelSize"}}]
    requests.extend({"mergeCells":{"range":grid("設定",r,r,1,3),"mergeType":"MERGE_ALL"}} for r in range(10,20))
    requests.append({"createDeveloperMetadata":{"developerMetadata":{"metadataKey":MARKER,"metadataValue":sha256(source_id.encode()).hexdigest(),"visibility":"DOCUMENT","location":{"spreadsheet":True}}}})
    return requests


class CoverageForm:
    def __init__(self,daily):self.daily=daily;self.inbox=Coverage(daily.store)

    def installed(self):
        meta=self.daily.verify()
        markers=[m for m in (meta or {}).get("developerMetadata",[]) if m.get("metadataKey")==MARKER]
        if not markers:return False
        if len(markers)!=1 or markers[0].get("metadataValue")!=sha256(self.daily.source_id.encode()).hexdigest():raise ProjectionError("coverage_form_binding_invalid")
        return True

    def form(self):
        blocks=self.daily.read_ranges(["'設定'!B11:B16","'設定'!F11:F11"])
        values=[r[0] if r else "" for r in blocks[0]];values += [""]*(6-len(values))
        return values,blocks[1][0][0] if blocks[1] and blocks[1][0] else ""

    def submit(self,current_month):
        values,token=self.form();old=self.inbox.read()["requests"].get(token)
        if values[5] is not True and not old:return {"coverage_submitted":0}
        fingerprint=digest(values[:5]);new=old is None
        old=old or self.inbox.prepare(token,values[:5],current_month=current_month,form_digest=fingerprint)
        changed=old["form_digest"]!=fingerprint
        if old["state"] in {"queued","pending"}:self.write([cells("設定",17,[[STATE_LABELS[old["state"]]],[old["updated_at"]]],left=1,width=1)])
        item=self.inbox.apply(token)
        if self.form()!=(values,token):return {"coverage_submitted":int(new),"coverage_input_changed":1}
        label=STATE_LABELS[item["state"]]+("・変更した入力は未送信です" if changed else "")
        if item["error"]:label+="・"+ERRORS[item["error"]]
        token="REQ-"+uuid4().hex
        self.write([cells("設定",17,[[label],[item["updated_at"]]],left=1,width=1),cells("設定",16,[[False]],left=1,width=1),cells("設定",11,[[token]],left=5,width=1)])
        if self.daily.read_ranges(["'設定'!B17:B18","'設定'!B16:B16","'設定'!F11:F11"])!=[[[label],[item["updated_at"]]],[[False]],[[token]]]:raise ProjectionError("coverage_ack_unknown")
        return {"coverage_submitted":int(new)}

    def write(self,requests):
        self.daily.db.svc.spreadsheets().batchUpdate(spreadsheetId=self.daily.db.sid,body={"requests":requests}).execute(num_retries=0)


def run_coverage(daily,*,apply,current_month):
    form=CoverageForm(daily)
    if not form.installed():return {}
    before=form.inbox.read()["requests"]
    pending=[k for k,v in before.items() if v["state"] in {"queued","pending"}]
    if not apply:return {"coverage_pending":len(pending),"coverage_form_ready":int(form.form()[0][5] is True)}
    for key in pending[:20]:form.inbox.apply(key)
    result=form.submit(current_month) if len(pending)<=20 else {"coverage_submitted":0}
    form.inbox.sync()
    after=form.inbox.read()["requests"]
    result.update({"coverage_"+state:sum(v["state"]==state and before.get(k,{}).get("state")!=state for k,v in after.items()) for state in ("applied","failed")})
    result["coverage_pending"]=sum(v["state"] in {"queued","pending"} for v in after.values())
    return result


def upgrade_requests(daily,*,current_month):
    if CoverageForm(daily).installed():return []
    existing=daily.read_ranges(["'設定'!A10:C19","'設定'!F11:F11","'設定'!A22:C24"],formulas=True)
    if any(v!="" for block in existing for row in block for v in row):raise ProjectionError("coverage_upgrade_range_occupied")
    return install_requests(daily.source_id,current_month=current_month)


def render_requests(summary,current_month,controls):
    month=str(controls.get("coverage_month",current_month));month_key(month)
    page=max(1,int(controls.get("coverage_page",1)))
    routes=summary.get("coverage_routes",{})
    pages=max(1,(len(routes)+49)//50);page=min(page,pages)
    states=summary.get("coverage",{}).get(month,{})
    labels={v:k for k,v in STATES.items()}
    rows=[[v["route"],v["account"],labels.get(states.get(k),"未確認")] for k,v in sorted(routes.items())][(page-1)*50:page*50]
    months=[shift_month(current_month,-i) for i in range(120)]
    requests=[cells("設定",24,[["全経路・口座",len(routes),f"{page}/{pages}ページ"]],width=3),cells("設定",25,[["経路","口座","状況"]],width=3),
        cells("設定",26,rows+[[""]*3 for _ in range(50-len(rows))],width=3),dropdown("設定",23,1,range(1,pages+1)),
        dropdown("設定",22,1,months,strict=False),dropdown("設定",11,1,months,strict=False),dropdown("設定",12,1,months,strict=False)]
    if rows:
        requests.extend([{"repeatCell":{"range":grid("設定",25,25+len(rows)),"cell":{"userEnteredFormat":{"wrapStrategy":"WRAP","verticalAlignment":"MIDDLE"}},"fields":"userEnteredFormat(wrapStrategy,verticalAlignment)"}},
            {"updateDimensionProperties":{"range":{"sheetId":SHEETS["設定"][0],"dimension":"ROWS","startIndex":24,"endIndex":25+len(rows)},"properties":{"pixelSize":60},"fields":"pixelSize"}}])
    for row,key in [(13,"route"),(14,"account")]:
        choices=sorted({v[key] for v in routes.values()})
        if choices:requests.append(dropdown("設定",row,1,choices,strict=False))
    return requests
