"""Bounded dropdown pages without changing selections or accounting data."""
from hashlib import sha256

from .daily_view import SHEETS, HOME_LOOKUP_ROW, cells, dropdown, grid
from .monthly_projection import ProjectionError

MARKER="kakeibo_daily_choice_pages_v1"
CATEGORY_PAGE_SIZE=200
EXPENSE_PAGE_SIZE=500
CONTROLS=(("履歴",9,"分類候補\nページ"),("推移",17,"分類候補\nページ"),
          ("確認",73,"分類候補\nページ"),("確認",75,"明細候補\nページ"))


def page_options(page,pages):
    """A small nearby page menu; any page number can also be entered."""
    page=min(pages,max(1,int(page)))
    return sorted({1,pages,*range(max(1,page-20),min(pages,page+20)+1)})


def page_dropdown(title,row,col,page,pages):
    return dropdown(title,row,col,page_options(page,pages),strict=False)


def category_id(catalog,label,*,active_only=False):
    matches={c.category_id for c in catalog.categories if (c.active or not active_only) and
        (c.label==label or any(f"{a or '未設定'}｜{b or '未設定'}"==label for a,b in c.aliases))}
    if len(matches)!=1:raise ProjectionError("daily_category_selection_changed")
    return next(iter(matches))


def installed(daily):
    markers=[m for m in (daily.verify() or {}).get("developerMetadata",[]) if m.get("metadataKey")==MARKER]
    if not markers:return False
    if len(markers)!=1 or markers[0].get("metadataValue")!=sha256(daily.source_id.encode()).hexdigest():
        raise ProjectionError("daily_choice_binding_invalid")
    return True


def install_requests(source_id):
    requests=[]
    for title,row,label in CONTROLS:
        requests.extend([cells(title,row,[[label,1,""]],width=3),
            page_dropdown(title,row,1,1,1),
            {"repeatCell":{"range":grid(title,row,row,1,2),"cell":{"userEnteredFormat":{"backgroundColor":{"red":1,"green":0.98,"blue":0.85}}},"fields":"userEnteredFormat.backgroundColor"}},
            {"repeatCell":{"range":grid(title,row,row),"cell":{"userEnteredFormat":{"wrapStrategy":"WRAP","verticalAlignment":"MIDDLE","textFormat":{"fontSize":10}}},"fields":"userEnteredFormat(wrapStrategy,verticalAlignment,textFormat)"}},
            {"updateDimensionProperties":{"range":{"sheetId":SHEETS[title][0],"dimension":"ROWS","startIndex":row-1,"endIndex":row},"properties":{"pixelSize":60},"fields":"pixelSize"}}])
    requests.extend([cells("確認",77,[["候補の切替","候補ページを選ぶと定期処理で更新されます。ページ番号の直接入力もできます。入力中の値は残ります。"]],width=4),
        {"mergeCells":{"range":grid("確認",77,77,1,4),"mergeType":"MERGE_ALL"}},
        {"repeatCell":{"range":grid("確認",77,77,0,4),"cell":{"userEnteredFormat":{"wrapStrategy":"WRAP","verticalAlignment":"MIDDLE","textFormat":{"fontSize":10}}},"fields":"userEnteredFormat(wrapStrategy,verticalAlignment,textFormat)"}},
        {"updateDimensionProperties":{"range":{"sheetId":SHEETS["確認"][0],"dimension":"ROWS","startIndex":76,"endIndex":77},"properties":{"pixelSize":85},"fields":"pixelSize"}},
        {"createDeveloperMetadata":{"developerMetadata":{"metadataKey":MARKER,"metadataValue":sha256(source_id.encode()).hexdigest(),"visibility":"DOCUMENT","location":{"spreadsheet":True}}}}])
    return requests


def upgrade_requests(daily):
    if installed(daily):return []
    blocks=daily.read_ranges(["'履歴'!A9:C9","'推移'!A17:C17","'確認'!A73:D77"],formulas=True)
    if any(v!="" for block in blocks for row in block for v in row):raise ProjectionError("daily_choice_upgrade_occupied")
    return install_requests(daily.source_id)


def slice_page(options,value,size):
    pages=max(1,(len(options)+size-1)//size);page=min(pages,max(1,int(value)))
    return options[(page-1)*size:page*size],page,pages


def render_requests(catalog,expense_choices,controls):
    enabled=controls.get("choice_pages_enabled",False)
    all_labels=[c.label for c in catalog.categories];active=[c.label for c in catalog.categories if c.active]
    if not enabled and (len(all_labels)>CATEGORY_PAGE_SIZE or len(expense_choices)>EXPENSE_PAGE_SIZE):
        raise ProjectionError("daily_choice_paging_required")
    columns=[];requests=[]
    specs=[(all_labels,"history_choice_page","category",False),(all_labels,"trend_choice_page","trend_category",False),
           (active,"correction_category_page","correction_category",True),(expense_choices,"expense_choice_page","correction_choice",None)]
    for (options,key,selection,active_only),(title,row,label) in zip(specs,CONTROLS):
        shown,page,pages=slice_page(options,controls.get(key,1),EXPENSE_PAGE_SIZE if active_only is None else CATEGORY_PAGE_SIZE)
        shown=list(shown)
        if active_only is False:shown.insert(0,"すべて")
        selected=controls.get(selection,"")
        if selected and selected not in shown:
            try:
                if active_only is not None:category_id(catalog,selected,active_only=active_only)
                shown.append(selected)
            except ProjectionError:pass  # Keep unfinished form text; submission validates it.
        columns.append(shown)
        if enabled:
            requests.extend([cells(title,row,[[label]],width=1),
                cells(title,row,[[f"全{len(options)}件\n{page}/{pages}頁"]],left=2,width=1),
                page_dropdown(title,row,1,page,pages)])
    helper=[[column[i] if i<len(column) else "" for column in columns] for i in range(HOME_LOOKUP_ROW-2)]
    requests.extend([cells("_候補",1,[["履歴カテゴリ","推移カテゴリ","修正カテゴリ","修正対象ID"]],width=4),
                     cells("_候補",2,helper,width=4)])
    for (title,row,column,strict),values in zip([("履歴",4,"A",True),("推移",4,"B",True),("確認",64,"C",True),("確認",61,"D",False)],columns):
        requests.append(dropdown(title,row,1,source=f"='_候補'!${column}$2:${column}${len(values)+1}",strict=strict) if values else
                        {"setDataValidation":{"range":grid(title,row,row,1,2)}})
    return requests
