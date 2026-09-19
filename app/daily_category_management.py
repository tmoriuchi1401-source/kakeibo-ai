"""Category identity form in the existing daily Settings tab."""
from hashlib import sha256
from uuid import uuid4

from .amazon_money import digest
from .category_management import ACTIONS, ERRORS, CategoryManagement, CategoryMaster
from .daily_view import SHEETS, STATE_LABELS, cells, dropdown, grid
from .monthly_projection import ProjectionError

MARKER="kakeibo_daily_category_management_v1"


def install_requests(source_id):
    rows=[["カテゴリの管理","固定IDで管理"],["操作","改名（IDを保持）"],
        ["元カテゴリ",""],["新しい分類名",""],["送信",False],["反映状況","未送信"],["最終更新",""],
        ["入力方法","大カテゴリ｜小カテゴリ。分割は1行に1分類。統合は元の固定IDをカンマで区切ります。"],
        ["過去の分類","過去明細は変更しません。分割・統合は新IDを追加し、元分類と既存ルールを残します。"],
        ["改名","固定IDと旧名を保持します。既存ルールは旧名のまま引き継ぎます。"]]
    req=[cells("設定",89,rows,width=3),cells("設定",90,[["REQ-"+uuid4().hex]],left=5,width=1),
        dropdown("設定",90,1,list(ACTIONS)),
        {"setDataValidation":{"range":grid("設定",93,93,1,2),"rule":{"condition":{"type":"BOOLEAN"},"strict":True,"showCustomUi":True}}},
        cells("設定",101,[["カテゴリ一覧・ページ",1]],width=3),
        {"repeatCell":{"range":grid("設定",89,98),"cell":{"userEnteredFormat":{"wrapStrategy":"WRAP","verticalAlignment":"MIDDLE"}},"fields":"userEnteredFormat(wrapStrategy,verticalAlignment)"}},
        {"repeatCell":{"range":grid("設定",90,93,1,3),"cell":{"userEnteredFormat":{"backgroundColor":{"red":1,"green":0.98,"blue":0.85}}},"fields":"userEnteredFormat.backgroundColor"}},
        {"updateDimensionProperties":{"range":{"sheetId":SHEETS["設定"][0],"dimension":"ROWS","startIndex":88,"endIndex":98},"properties":{"pixelSize":85},"fields":"pixelSize"}}]
    req.extend({"mergeCells":{"range":grid("設定",r,r,1,3),"mergeType":"MERGE_ALL"}} for r in range(89,99))
    req.extend({"mergeCells":{"range":grid("設定",r,r,1,3),"mergeType":"MERGE_ALL"}} for r in range(103,154))
    req.extend([
        {"repeatCell":{"range":grid("設定",103,153),"cell":{"userEnteredFormat":{"wrapStrategy":"WRAP","verticalAlignment":"MIDDLE"}},"fields":"userEnteredFormat(wrapStrategy,verticalAlignment)"}},
        {"updateDimensionProperties":{"range":{"sheetId":SHEETS["設定"][0],"dimension":"ROWS","startIndex":102,"endIndex":153},"properties":{"pixelSize":72},"fields":"pixelSize"}}])
    req.append({"createDeveloperMetadata":{"developerMetadata":{"metadataKey":MARKER,"metadataValue":sha256(source_id.encode()).hexdigest(),"visibility":"DOCUMENT","location":{"spreadsheet":True}}}})
    return req


class CategoryForm:
    def __init__(self,daily,source=None):
        self.daily=daily
        if source is not None:
            from .compact_category_sync import sync_choices
            self.inbox=CategoryManagement(daily.store,CategoryMaster(source),lambda catalog:sync_choices(source,catalog))

    def installed(self):
        meta=self.daily.verify()
        markers=[m for m in (meta or {}).get("developerMetadata",[]) if m.get("metadataKey")==MARKER]
        if not markers:return False
        if len(markers)!=1 or markers[0].get("metadataValue")!=sha256(self.daily.source_id.encode()).hexdigest():
            raise ProjectionError("category_form_binding_invalid")
        return True

    def form(self):
        blocks=self.daily.read_ranges(["'設定'!B90:B93","'設定'!F90:F90"])
        values=[r[0] if r else "" for r in blocks[0]];values += [""]*(4-len(values))
        return values,blocks[1][0][0] if blocks[1] and blocks[1][0] else ""

    def write(self,requests):
        self.daily.db.svc.spreadsheets().batchUpdate(spreadsheetId=self.daily.db.sid,body={"requests":requests}).execute(num_retries=0)

    def submit(self):
        values,token=self.form();old=self.inbox.read()["requests"].get(token)
        if values[3] is not True and not old:return {"categories_submitted":0}
        fingerprint=digest(values[:3]);new=old is None
        old=old or self.inbox.prepare(token,values[:3],form_digest=fingerprint)
        changed=old["form_digest"]!=fingerprint
        if old["state"] in {"queued","pending"}:
            self.write([cells("設定",94,[[STATE_LABELS[old["state"]]],[old["updated_at"]]],left=1,width=1)])
        item=self.inbox.apply(token)
        if self.form()!=(values,token):return {"categories_submitted":int(new),"category_input_changed":1}
        label=STATE_LABELS[item["state"]]+("・変更した入力は未送信です" if changed else "")
        if item["error"]:label+="・"+ERRORS[item["error"]]
        token="REQ-"+uuid4().hex
        self.write([cells("設定",94,[[label],[item["updated_at"]]],left=1,width=1),
            cells("設定",93,[[False]],left=1,width=1),cells("設定",90,[[token]],left=5,width=1)])
        if self.daily.read_ranges(["'設定'!B94:B95","'設定'!B93:B93","'設定'!F90:F90"])!=[[[label],[item["updated_at"]]],[[False]],[[token]]]:
            raise ProjectionError("category_ack_unknown")
        return {"categories_submitted":int(new)}


def run_categories(daily,source,*,apply):
    form=CategoryForm(daily,source)
    if not form.installed():return {}
    before=form.inbox.read()["requests"]
    pending=[k for k,v in before.items() if v["state"] in {"queued","pending"}]
    if not apply:return {"categories_pending":len(pending),"category_form_ready":int(form.form()[0][3] is True)}
    for key in pending[:20]:form.inbox.apply(key)
    result=form.submit() if len(pending)<=20 else {"categories_submitted":0}
    after=form.inbox.read()["requests"]
    result.update({"categories_"+state:sum(v["state"]==state and before.get(k,{}).get("state")!=state for k,v in after.items()) for state in ("applied","failed")})
    result["categories_pending"]=sum(v["state"] in {"queued","pending"} for v in after.values())
    return result


def upgrade_requests(daily):
    if CategoryForm(daily).installed():return []
    existing=daily.read_ranges(["'設定'!A89:C153","'設定'!F90:F90"],formulas=True)
    if any(v!="" for block in existing for row in block for v in row):raise ProjectionError("category_upgrade_range_occupied")
    return install_requests(daily.source_id)


def render_requests(catalog,controls):
    active=[c for c in catalog.categories if c.active]
    pages=max(1,(len(active)+49)//50);page=min(pages,max(1,int(controls.get("category_page",1))))
    shown=active[(page-1)*50:page*50]
    rows=[[c.label,c.category_id,""] for c in shown]
    req=[cells("設定",102,[["全カテゴリ",len(active),f"{page}/{pages}ページ"]],width=3),
        cells("設定",103,[["カテゴリ","固定ID",""]],width=3),
        cells("設定",104,rows+[[""]*3 for _ in range(50-len(rows))],width=3),
        dropdown("設定",101,1,range(1,pages+1))]
    if active:req.append(dropdown("設定",91,1,[c.label+"【"+c.category_id+"】" for c in active],strict=False))
    return req
