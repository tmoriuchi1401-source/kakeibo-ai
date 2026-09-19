from copy import deepcopy
from hashlib import sha256
import pytest

from app.daily_edit_cutover import MARKER, PROTECTION, cutover_requests, verify_cutover
from app.monthly_projection import ProjectionError

WRITER="existing@synthetic.iam.gserviceaccount.com"


def metadata():
    from app.compact_categories import TITLE,MARKER as HELPER_MARKER,VERSION as HELPER_VERSION
    from app.sheets_ui import EXPENSE_CATEGORY_HELPER_ID
    return {"developerMetadata":[{"metadataKey":MARKER,"metadataValue":sha256(b"daily").hexdigest()}],
        "sheets":[{"properties":{"title":"支出明細","sheetId":123},"protectedRanges":[{
            "description":PROTECTION,"range":{"sheetId":123,"startColumnIndex":0,"endColumnIndex":13},
            "warningOnly":False,"editors":{"users":[WRITER]}}]},
            {"properties":{"title":TITLE,"sheetId":EXPENSE_CATEGORY_HELPER_ID,"gridProperties":{"rowCount":3,"columnCount":4}},
             "developerMetadata":[{"metadataKey":HELPER_MARKER,"metadataValue":HELPER_VERSION}]}]}


@pytest.mark.parametrize("change",["unbound","wrong_daily","warning","end_row","hole","other_editor","domain"])
def test_form_cannot_be_enabled_before_the_canonical_edit_cutover(change):
    meta=metadata();p=meta["sheets"][0]["protectedRanges"][0]
    if change=="unbound":meta["developerMetadata"]=[]
    elif change=="wrong_daily":meta["developerMetadata"][0]["metadataValue"]="other"
    elif change=="warning":p["warningOnly"]=True
    elif change=="end_row":p["range"]["endRowIndex"]=5000
    elif change=="hole":p["unprotectedRanges"]=[{"sheetId":123,"startRowIndex":10}]
    elif change=="other_editor":p["editors"]["users"].append("other@example.test")
    elif change=="domain":p["editors"]["domainUsersCanEdit"]=True
    with pytest.raises(ProjectionError):verify_cutover(meta,"daily",WRITER)


def test_cutover_targets_existing_ledger_and_owned_old_edit_view_only():
    from app.sheets_ui import CATEGORY_UI_ID, CATEGORY_UI_MARKER, VERSION
    meta=metadata();meta["developerMetadata"]=[];meta["sheets"][0]["protectedRanges"]=[]
    meta["sheets"].append({"properties":{"title":"カテゴリ対応","sheetId":CATEGORY_UI_ID},
        "developerMetadata":[{"metadataKey":CATEGORY_UI_MARKER,"metadataValue":VERSION}]})
    requests=cutover_requests(meta,"daily",WRITER)
    assert requests[0]["addProtectedRange"]["protectedRange"]["editors"]=={"users":[WRITER]}
    assert "endRowIndex" not in requests[0]["addProtectedRange"]["protectedRange"]["range"]
    assert all(q["updateCells"]["range"]["sheetId"]==CATEGORY_UI_ID for q in requests if "updateCells" in q)
    assert requests[-1]["createDeveloperMetadata"]["developerMetadata"]["metadataKey"]==MARKER
    verify_cutover(metadata(),"daily",WRITER)
    normalized=metadata()
    normalized["sheets"][0]["protectedRanges"][0]["range"].pop("startColumnIndex")
    verify_cutover(normalized,"daily",WRITER)
    assert cutover_requests(metadata(),"daily",WRITER)==[]
    meta["sheets"][-1]["developerMetadata"]=[]
    with pytest.raises(ProjectionError,match="not_owned"):cutover_requests(meta,"daily",WRITER)
