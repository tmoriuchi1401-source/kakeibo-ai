from __future__ import annotations
from datetime import datetime, timezone
import re
import time
from googleapiclient.errors import HttpError
from .google_clients import sheets_service

CATEGORY_SEPARATOR = "｜"
CATEGORY_RULE_UI_SHEET = "カテゴリ操作"
LEGACY_CATEGORY_RULE_UI_SHEET = "カテゴリ自動分類"
CATEGORY_WORKFLOW_SHEET = CATEGORY_RULE_UI_SHEET
CATEGORY_REQUEST_SHEET = "_カテゴリ実行受付"
CATEGORY_WORKFLOW_MARKERS = {
    "rule": "■ 1. カテゴリを選ぶ・今後の自動分類",
    "backfill": "■ 2. 過去分の固定プレビュー",
    "confirm": "■ 3. 固定プレビューを確認して反映",
}
CATEGORY_RULE_UI_HELPER_SHEET = "_支出明細カテゴリ候補"
# The shared helper sheet has two disjoint horizontal spill surfaces.  Ledger
# G may only read B:ZY; the opt-in rule UI may only read ZZ:ALL.  Keeping this
# as an explicit contract prevents either row-relative formula from leaking
# choices into the other dropdown.
EXPENSE_CATEGORY_HELPER_LEDGER_MINOR_START_A1 = "B"
EXPENSE_CATEGORY_HELPER_LEDGER_MINOR_END_A1 = "ZY"
CATEGORY_RULE_UI_HELPER_COLUMN = 701  # ZZ, zero based
CATEGORY_RULE_UI_HELPER_A1 = "ZZ"
CATEGORY_RULE_UI_HELPER_END_A1 = "ALL"


def category_rule_ui_control_requests(*, sheet_id: int, helper_sheet_id: int,
                                      row_count: int, start_row: int = 2) -> list[dict]:
    """Return controls for the rows actually rendered by the rule UI.

    The minor choice is a native range dropdown whose source is a row-specific
    formula in the established hidden category helper.  It deliberately uses
    one validation request per rendered row: Sheets otherwise freezes a
    range-backed source when the rule is filled down.
    """
    if row_count <= 0:
        return []
    end_row = start_row + row_count - 1
    helper_rows = []
    for row_num in range(start_row, end_row + 1):
        helper_rows.append({"values": [{"userEnteredValue": {"formulaValue": (
            f"=IFERROR(LET(major,INDEX('{CATEGORY_RULE_UI_SHEET}'!C:C,ROW()),IF(major=\"\",\"\","
            "TRANSPOSE(UNIQUE(FILTER('カテゴリ'!$B$2:$B,'カテゴリ'!$A$2:$A=major))))),\"\")"
        )}}]})
    requests = [
        {"updateCells": {"range": {"sheetId": helper_sheet_id,
            "startRowIndex": start_row - 1, "endRowIndex": end_row,
            "startColumnIndex": CATEGORY_RULE_UI_HELPER_COLUMN,
            "endColumnIndex": CATEGORY_RULE_UI_HELPER_COLUMN + 1},
            "rows": helper_rows, "fields": "userEnteredValue"}},
        {"setDataValidation": {"range": {"sheetId": sheet_id,
            "startRowIndex": start_row - 1, "endRowIndex": end_row,
            "startColumnIndex": 2, "endColumnIndex": 3}, "rule": {
                "condition": {"type": "ONE_OF_RANGE", "values": [
                    {"userEnteredValue": "='カテゴリ'!$A$2:$A"}
                ]}, "strict": True, "showCustomUi": True,
                "inputMessage": "カテゴリマスタの大カテゴリを選択してください。"}}},
        {"setDataValidation": {"range": {"sheetId": sheet_id,
            "startRowIndex": start_row - 1, "endRowIndex": end_row,
            "startColumnIndex": 4, "endColumnIndex": 6}, "rule": {
                "condition": {"type": "BOOLEAN"}, "strict": True,
                "showCustomUi": True}}},
        {"repeatCell": {"range": {"sheetId": sheet_id,
            "startRowIndex": 1, "endRowIndex": end_row,
            "startColumnIndex": 0, "endColumnIndex": 6}, "cell": {
                "userEnteredFormat": {"backgroundColor": {"red": 1, "green": 1, "blue": 1},
                    "textFormat": {"foregroundColor": {"red": 0.16, "green": 0.20, "blue": 0.23},
                        "bold": False}, "wrapStrategy": "WRAP", "verticalAlignment": "MIDDLE"}},
            "fields": "userEnteredFormat(backgroundColor,textFormat,wrapStrategy,verticalAlignment)"}},
    ]
    for row_num in range(start_row, end_row + 1):
        requests.append({"setDataValidation": {"range": {"sheetId": sheet_id,
            "startRowIndex": row_num - 1, "endRowIndex": row_num,
            "startColumnIndex": 3, "endColumnIndex": 4}, "rule": {
                "condition": {"type": "ONE_OF_RANGE", "values": [{
                    "userEnteredValue": (
                        f"='{CATEGORY_RULE_UI_HELPER_SHEET}'!${CATEGORY_RULE_UI_HELPER_A1}${row_num}:${CATEGORY_RULE_UI_HELPER_END_A1}${row_num}"
                    )
                }]}, "strict": True, "showCustomUi": True,
                "inputMessage": "選んだ大カテゴリに属する小カテゴリを選択してください。"}}})
    return requests


def combined_category_options(categories:list[tuple[str,str]])->list[str]:
    return list(dict.fromkeys(
        f"{major}{CATEGORY_SEPARATOR}{minor}"
        for major,minor in categories if major and minor
    ))


HEADERS={
"支出明細":["支出ID","日付","店舗","商品名","金額","大カテゴリ","小カテゴリ","支払方法","データ元","レシートID","取込ID","備考","計上状態"],
"レシート":["レシートID","日付","店舗","合計金額","支払方法","画像URL","解析状態","解析日時","備考"],
"カテゴリ":["大カテゴリ","小カテゴリ"],
"店舗":["店舗ID","店舗名","標準店舗名","備考"],
"取込データ":["取込ID","取込日時","データ元","元データID","日付","店舗","金額","支払方法","処理状態","統合先支出ID","元データハッシュ","備考"],
"Amazon注文":["Amazonキー","Order ID","ASIN","注文日","商品名","数量","商品金額","支払方法","大カテゴリ","小カテゴリ","備考","データハッシュ","最終取込日時","発送日","発送数"],
"Amazon注文ヘッダ":["Order ID","Order Date","Order Amount","Payment Method","Item Count","Order Status","Charged Amount","Refund Status","Refund Amount","Shipment Amount","Gift Card Amount","Points Amount","Discount Amount","Source","Last Updated At"],
"Amazonイベント":["イベントID","Gmail Message ID","RFC Message-ID","Thread ID","Source Hash","Event Type","Order ID","Event Date","Charged Amount","Order Amount","Refund Amount","Shipment Amount","Gift Card Amount","Points Amount","Coupon Amount","Discount Amount","Payment Method","Item Count","Parse Status","Match Status","Apply Status","Parser Version","Imported At","Last Parsed At"],
"Amazon照合候補":["候補ID","カード取込ID","Order ID","候補順位","カード日","注文日","カード金額",
              "注文金額","差額","差額率","日付差","商品数","商品概要","大カテゴリ",
              "支払方法","データ種別","注文fingerprint","選択表示","生成日時","発送日","発送日差","発送数"],
"商品マスタ":["商品ID","商品名","大カテゴリ","小カテゴリ","備考","最終更新日時"],
"要確認":["確認ID","優先度","日付","データ元","店舗","金額","原本","状態","推奨対応","備考",
       "ユーザー判断","統合先取込ID","カテゴリ（大｜小）","小カテゴリ（従来）","ユーザー備考","反映結果",
       "Amazon候補","Amazon候補数","Amazon注文候補選択","Amazon候補ID","Amazon選択状態"],
"支出一覧":["日付","店舗","商品名","金額","大カテゴリ","小カテゴリ","支払方法","データ元","備考","支出ID"],
"カテゴリ自動分類ルール":["ルールID","条件種別","データ元","口座別名","請求名","店舗名","商品ID","商品名","金額","大カテゴリ","小カテゴリ","承認元支出ID","承認日時","revision","有効","適用件数","最終適用支出ID","最終適用日時"],
}

class SheetsReadPacer:
    """Optional shared per-account read spacing for lengthy verification jobs."""
    def __init__(self, *, clock=time.monotonic, sleep=time.sleep):
        self.clock, self.sleep, self.next_read = clock, sleep, 0.0

    def __call__(self):
        delay = self.next_read - self.clock()
        if delay > 0:
            self.sleep(delay)
        self.next_read = self.clock() + 1.1


class SheetsDB:
    def __init__(self, spreadsheet_id:str, service=None, *, read_sleeper=time.sleep, projection_journal=None,
                 read_pacer=None, read_retry_base=1):
        self.sid=spreadsheet_id; self.svc=service or sheets_service()
        self._read_sleeper=read_sleeper
        self._read_pacer=read_pacer
        self._read_retry_base=read_retry_base
        self._sheet_metadata_cache=None
        self._sheet_read_metrics={"logical":0,"attempts":0,"retries":0}
        self._projection_journal=projection_journal

    def projection_store(self):
        """Lazily use the configured private folder; reads create no state."""
        journal=getattr(self,"_projection_journal",None)
        if journal is None:
            from .projection_store import ProjectionJournal, store_from_environment
            store=store_from_environment(self.sid)
            if store is None:return None
            journal=ProjectionJournal(store)
            self._projection_journal=journal
        return journal.store

    def _invalidate_expense_projection(self, sheet, ranges=(), *, append=False):
        if sheet.strip("'") != "支出明細":return
        store=self.projection_store()
        if store is not None:
            # Must finish before the accounting request. Nothing after the
            # request clears this marker; derived refresh does the readback.
            self._projection_journal.mark(ranges,append=append)

    def _read_metrics(self):
        if not hasattr(self, "_sheet_read_metrics"):
            self._sheet_read_metrics={"logical":0,"attempts":0,"retries":0}
        return self._sheet_read_metrics

    def sheet_read_metrics(self):
        """Expose bounded read/retry counts for a single CLI process."""
        return dict(self._read_metrics())

    def _execute_sheet_read(self, request_factory):
        """Retry only Sheets read-quota responses; never wrap writes."""
        metrics=self._read_metrics(); metrics["logical"] += 1
        for attempt in range(4):
            try:
                pacer=getattr(self,"_read_pacer",None)
                if pacer is not None:pacer()
                metrics["attempts"] += 1
                return request_factory().execute()
            except HttpError as exc:
                status=int(getattr(getattr(exc, "resp", None), "status", 0) or 0)
                if status != 429 or attempt == 3:
                    raise
                metrics["retries"] += 1
                getattr(self, "_read_sleeper", time.sleep)(min(60, getattr(self,"_read_retry_base",1) * (2 ** attempt)))
        raise RuntimeError("sheets_read_retry_exhausted")

    def _sheet_metadata(self):
        cached=getattr(self, "_sheet_metadata_cache", None)
        if cached is None:
            cached=self._execute_sheet_read(
                lambda: self.svc.spreadsheets().get(spreadsheetId=self.sid)
            )
            self._sheet_metadata_cache=cached
        return cached

    def _invalidate_sheet_metadata(self):
        self._sheet_metadata_cache=None

    def _compact_category_helper(self):
        from .compact_categories import compact_helper
        return compact_helper(self._sheet_metadata())

    def sheet_titles(self):
        meta=self._sheet_metadata()
        return [s["properties"]["title"] for s in meta["sheets"]]
    def ensure_schema(self, categories:list[tuple[str,str]]|None=None):
        titles=set(self.sheet_titles()); req=[]
        for title in HEADERS:
            if title not in titles: req.append({"addSheet":{"properties":{"title":title}}})
        if req:
            self.svc.spreadsheets().batchUpdate(spreadsheetId=self.sid,body={"requests":req}).execute()
            self._invalidate_sheet_metadata()
        for title, hdr in HEADERS.items():
            if title == "要確認": self._ensure_review_original_column()
            existing=self.get(f"{title}!1:1")
            if not existing or existing[0][:len(hdr)] != hdr:
                self.svc.spreadsheets().values().update(spreadsheetId=self.sid,range=f"{title}!A1",valueInputOption="RAW",body={"values":[hdr]}).execute()
        if categories:
            rows=[[a,b] for a,b in categories]
            self.svc.spreadsheets().values().clear(spreadsheetId=self.sid,range="カテゴリ!A2:B",body={}).execute()
            self.svc.spreadsheets().values().update(spreadsheetId=self.sid,range="カテゴリ!A2",valueInputOption="RAW",body={"values":rows}).execute()
    def get(self, rng:str):
        return self._execute_sheet_read(
            lambda: self.svc.spreadsheets().values().get(spreadsheetId=self.sid,range=rng)
        ).get("values",[])
    def get_raw(self,rng:str):
        return self._execute_sheet_read(lambda:self.svc.spreadsheets().values().get(
            spreadsheetId=self.sid,range=rng,valueRenderOption="UNFORMATTED_VALUE",
            dateTimeRenderOption="SERIAL_NUMBER")).get("values",[])
    def append(self, sheet:str, rows:list[list]):
        if not rows:return
        self._invalidate_expense_projection(sheet,append=True)
        reply=self.svc.spreadsheets().values().append(
            spreadsheetId=self.sid,range=f"{sheet}!A:A",valueInputOption="USER_ENTERED",
            insertDataOption="INSERT_ROWS",body={"values":rows},
        ).execute()
        if sheet == "支出明細":
            self._restore_expense_category_validation_for_append(reply)

    def _restore_expense_category_validation_for_append(self, reply:dict):
        """Apply G's exact row rules to freshly appended ledger rows only.

        This is intentionally a no-op until the reviewed UI plan has installed
        its owned helper sheet.  Business imports must never create UI support
        sheets implicitly or overwrite categories.
        """
        updated = str(reply.get("updates", {}).get("updatedRange", ""))
        match = re.search(r"!A(\d+):[A-Z]+(\d+)$", updated)
        if not match:
            return
        start, end = (int(value) for value in match.groups())
        from .sheets_ui import CAP, EXPENSE_CATEGORY_HELPER_ID, EXPENSE_CATEGORY_HELPER_MARKER, VERSION
        if start > CAP+1:
            return
        end = min(end, CAP+1)
        meta = self._sheet_metadata()
        helper = next((sheet for sheet in meta.get("sheets", [])
                       if sheet["properties"].get("sheetId") == EXPENSE_CATEGORY_HELPER_ID), None)
        if not helper or not any(
            marker.get("metadataKey") == EXPENSE_CATEGORY_HELPER_MARKER
            and marker.get("metadataValue") == VERSION
            for marker in helper.get("developerMetadata", [])
        ):
            return
        from .sheets_ui_actions import expense_minor_category_validation_requests, expense_helper_growth_requests
        growth = expense_helper_growth_requests(helper, end)
        self.svc.spreadsheets().batchUpdate(spreadsheetId=self.sid, body={
            "requests": growth + expense_minor_category_validation_requests(start, end),
        }).execute()
        if growth:self._invalidate_sheet_metadata()
    def append_raw(self, sheet:str, rows:list[list]):
        """Append literal ledger values; do not interpret bank descriptions as formulas."""
        if not rows:return
        self._invalidate_expense_projection(sheet,append=True)
        reply=self.svc.spreadsheets().values().append(spreadsheetId=self.sid,range=f"{sheet}!A:A",valueInputOption="RAW",insertDataOption="INSERT_ROWS",body={"values":rows}).execute()
        if sheet == "支出明細":self._restore_expense_category_validation_for_append(reply)
    def clear(self,rng:str):
        if rng.split("!")[0].strip("'")=="支出明細" and self.projection_store() is not None:
            from .monthly_projection import ProjectionError
            raise ProjectionError("ledger_clear_forbidden_use_status")
        self.svc.spreadsheets().values().clear(
            spreadsheetId=self.sid,range=rng,body={}
        ).execute()
    def ensure_sheet(self,title:str,header:list[str]):
        titles=set(self.sheet_titles())
        if title not in titles:
            self.svc.spreadsheets().batchUpdate(
                spreadsheetId=self.sid,
                body={"requests":[{"addSheet":{"properties":{
                    "title":title,"gridProperties":{"frozenRowCount":1}
                }}}]},
            ).execute()
            self._invalidate_sheet_metadata()
        if title == "要確認": self._ensure_review_original_column()
        existing=self.get(f"{title}!1:1")
        if not existing or existing[0][:len(header)] != header:
            self.svc.spreadsheets().values().update(
                spreadsheetId=self.sid,range=f"{title}!A1",valueInputOption="RAW",
                body={"values":[header]},
            ).execute()
    def _ensure_review_original_column(self):
        """Insert G once, retaining all existing review inputs and filter ranges."""
        legacy = HEADERS["要確認"][:6] + HEADERS["要確認"][7:]
        existing = self.get("'要確認'!A1:U1")
        if not existing or not existing[0]: return
        header = existing[0]
        if header[:len(HEADERS["要確認"])] == HEADERS["要確認"]: return
        # The insert may have committed even if the following header update
        # did not; a rerun must finish that migration without inserting twice.
        if (header[:6] == legacy[:6] and not (header[6] if len(header)>6 else "")
                and header[7:7+len(legacy)-6] == legacy[6:]):
            return
        if header[:len(legacy)] != legacy:
            raise ValueError("review_header_changed_before_original_link_migration")
        sheet = next(s for s in self._sheet_metadata()["sheets"]
                     if s["properties"]["title"] == "要確認")
        self.svc.spreadsheets().batchUpdate(spreadsheetId=self.sid,body={"requests":[
            {"insertDimension":{"range":{"sheetId":sheet["properties"]["sheetId"],
               "dimension":"COLUMNS","startIndex":6,"endIndex":7},"inheritFromBefore":True}}
        ]}).execute()
        self._invalidate_sheet_metadata()
    def configure_review_validation(self, categories:list[tuple[str,str]],
                                    amazon_options_by_row:dict[int,list[str]]|None=None):
        from .amazon_money_runtime import money_enabled
        actions=["支出として計上","重複として除外","レシートと統合","保留"]
        if not money_enabled():actions.insert(3,"Amazon注文と照合")
        meta=self.svc.spreadsheets().get(spreadsheetId=self.sid).execute()
        sheet_id=next(s["properties"]["sheetId"] for s in meta["sheets"]
                      if s["properties"]["title"]=="要確認")
        def rule(start_col,end_col,condition):
            return {"setDataValidation":{"range":{"sheetId":sheet_id,"startRowIndex":1,
                    "startColumnIndex":start_col,"endColumnIndex":end_col},
                    "rule":{"condition":condition,"strict":True,"showCustomUi":True}}}
        requests=[
            {"repeatCell":{"range":{"sheetId":sheet_id,"startRowIndex":1,
             "startColumnIndex":2,"endColumnIndex":3},
             "cell":{"userEnteredFormat":{"numberFormat":{"type":"DATE","pattern":"yyyy/mm/dd"}}},
             "fields":"userEnteredFormat.numberFormat"}},
             rule(10,11,{"type":"ONE_OF_LIST","values":[{"userEnteredValue":x} for x in
                  actions]}),
             rule(12,13,{"type":"ONE_OF_LIST","values":[{"userEnteredValue":x}
                  for x in combined_category_options(categories)]}),
             {"setDataValidation":{"range":{"sheetId":sheet_id,"startRowIndex":1,
              "startColumnIndex":13,"endColumnIndex":14}}},
        ]
        from .compact_categories import compact_helper, category_condition
        compact = compact_helper(meta)
        if compact:
            # Retain only existing review rows; never populate blank grid tails.
            sheet = next(s for s in meta["sheets"] if s["properties"]["sheetId"] == sheet_id)
            extent = sheet["properties"]["gridProperties"]["rowCount"]
            last = 1
            for first in range(2, extent + 1, 2000):
                ids = self.get_raw(f"'要確認'!A{first}:A{min(extent, first + 1999)}")
                last = max(last, max((first+i for i, row in enumerate(ids) if row and row[0]), default=1))
            requests[2]["setDataValidation"]["rule"]["condition"] = category_condition(compact)
            for request in requests:
                body = next(iter(request.values()))
                body["range"]["endRowIndex"] = max(2, last)
        for row_num,options in (amazon_options_by_row or {}).items():
            if options:
                requests.append({"setDataValidation":{"range":{"sheetId":sheet_id,
                    "startRowIndex":row_num-1,"endRowIndex":row_num,
                     "startColumnIndex":18,"endColumnIndex":19},
                    "rule":{"condition":{"type":"ONE_OF_LIST","values":[
                        {"userEnteredValue":value} for value in options
                    ]},"strict":True,"showCustomUi":True}}})
        # Presentation is opt-in; preserve the existing validation/apply contract.
        from .sheets_ui import refresh_layout_requests
        requests.extend(refresh_layout_requests(meta,"要確認"))
        self.svc.spreadsheets().batchUpdate(
            spreadsheetId=self.sid,body={"requests":requests}
        ).execute()
    def format_date_column(self,sheet_title:str,column_index:int=0):
        meta=self.svc.spreadsheets().get(spreadsheetId=self.sid).execute()
        sheet_id=next(s["properties"]["sheetId"] for s in meta["sheets"]
                      if s["properties"]["title"]==sheet_title)
        requests=[{"repeatCell":{"range":{
                "sheetId":sheet_id,"startRowIndex":1,
                "startColumnIndex":column_index,"endColumnIndex":column_index+1},
                "cell":{"userEnteredFormat":{"numberFormat":{"type":"DATE","pattern":"yyyy/mm/dd"}}},
                "fields":"userEnteredFormat.numberFormat"}}]
        from .sheets_ui import refresh_layout_requests
        requests.extend(refresh_layout_requests(meta,sheet_title))
        self.svc.spreadsheets().batchUpdate(
            spreadsheetId=self.sid,body={"requests":requests}
        ).execute()
    def update_row(self,sheet:str,row_num:int,row:list):
        if row_num>=2:self._invalidate_expense_projection(sheet,[(row_num,row_num)])
        self.svc.spreadsheets().values().update(spreadsheetId=self.sid,range=f"{sheet}!A{row_num}",valueInputOption="USER_ENTERED",body={"values":[row]}).execute()
    def set_raw_range(self, rng:str, rows:list[list]):
        if rows and rng.split("!")[0].strip("'")=="支出明細":
            match=re.fullmatch(r"'?(支出明細)'?![A-Z]+([0-9]+)(?::[A-Z]+[0-9]*)?",rng)
            if not match:
                from .monthly_projection import ProjectionError
                raise ProjectionError("ledger_write_range_must_be_bounded")
            start=int(match[2]);end=start+len(rows)-1
            if end>=2:self._invalidate_expense_projection("支出明細",[(max(2,start),end)])
        self.svc.spreadsheets().values().update(spreadsheetId=self.sid,range=rng,
            valueInputOption="RAW",body={"values":rows}).execute(num_retries=0)
    def update_row_raw(self,sheet:str,row_num:int,row:list):
        self.set_raw_range(f"'{sheet}'!A{row_num}",[row])
    def update_rows(self,sheet:str,rows:list[tuple[int,list]]):
        if not rows:return
        self._invalidate_expense_projection(sheet,[(n,n) for n,_ in rows if n>=2])
        if sheet == "要確認":
            # Approval reads displayed values. Writing the full row would turn
            # G's HYPERLINK formula into its displayed label.
            data = [{"range":f"'要確認'!P{row_num}","values":[[row[15]]]}
                    for row_num,row in rows]
            data += [{"range":f"'要確認'!U{row_num}","values":[[row[20]]]}
                     for row_num,row in rows]
            self.svc.spreadsheets().values().batchUpdate(
                spreadsheetId=self.sid,
                body={"valueInputOption":"RAW","data":data},
            ).execute()
            return
        section={"カテゴリ自動分類":"rule", "カテゴリ過去反映":"backfill",
                 "カテゴリ過去反映確認":"confirm"}.get(sheet)
        if section and CATEGORY_WORKFLOW_SHEET in set(self.sheet_titles()):
            prior=getattr(self,"_category_workflow_last_read",None)
            blocks=self._category_workflow_blocks()
            self._check_category_workflow_input(prior)
            start=self._workflow_positions(blocks)[section]["start"]
            data=[{"range":f"{CATEGORY_WORKFLOW_SHEET}!A{start+row_num-2}",
                   "values":[self._workflow_physical_row(section, row, compact=bool(self._compact_category_helper()))]}
                  for row_num,row in rows]
            self.svc.spreadsheets().values().batchUpdate(
                spreadsheetId=self.sid,
                body={"valueInputOption":"RAW","data":data}
            ).execute()
            self._category_workflow_last_read=None
            return
        data=[{"range":f"{sheet}!A{row_num}","values":[row]} for row_num,row in rows]
        self.svc.spreadsheets().values().batchUpdate(
            spreadsheetId=self.sid,
            body={"valueInputOption":"USER_ENTERED","data":data}
        ).execute()
    def update_shipping_fields(self, rows:list[tuple[int,list]]):
        if not rows:return
        data=[{"range":f"Amazon注文!N{row_num}:O{row_num}","values":[row[13:15]]}
              for row_num,row in rows]
        self.svc.spreadsheets().values().batchUpdate(
            spreadsheetId=self.sid,
            body={"valueInputOption":"USER_ENTERED","data":data}
        ).execute()
    def update_amazon_event_match_statuses(self, rows:list[tuple[int,str]]):
        if not rows:return
        data=[{"range":f"Amazonイベント!T{row_num}","values":[[status]]}
              for row_num,status in rows]
        self.svc.spreadsheets().values().batchUpdate(
            spreadsheetId=self.sid,
            body={"valueInputOption":"RAW","data":data}
        ).execute()
    def categories(self)->list[tuple[str,str]]:
        return [(r[0],r[1]) for r in self.get("カテゴリ!A2:B") if len(r)>=2 and r[0] and r[1]]
    def product_master(self)->dict[str,tuple[str,str,str]]:
        out={}
        for r in self.get("商品マスタ!A2:F"):
            if len(r)>=4: out[r[0]]=(r[2],r[3],r[1] if len(r)>1 else "")
        return out
    def amazon_index(self)->dict[str,tuple[int,str]]:
        out={}
        for i,r in enumerate(self.get("Amazon注文!A2:M"),start=2):
            if r: out[r[0]]=(i, r[11] if len(r)>11 else "")
        return out
    def amazon_baseline_keys(self)->set[str]:
        return {r[0] for r in self.get("Amazon注文!A2:K")
                if len(r)>10 and r[10]=="baseline"}
    def amazon_event_identity_index(self)->dict[str,set[str]]:
        identities={"gmail_message_ids":set(),"rfc_message_ids":set(),"source_hashes":set()}
        for raw in self.get("Amazonイベント!B2:E"):
            row=list(raw)+[""]*max(0,4-len(raw))
            if row[0]: identities["gmail_message_ids"].add(str(row[0]))
            if row[1]: identities["rfc_message_ids"].add(str(row[1]))
            if row[3]: identities["source_hashes"].add(str(row[3]))
        return identities
    def amazon_event_rows(self)->list[tuple[int,list]]:
        """Read stored Amazon events with their Sheets row numbers."""
        return [(i, list(row)) for i, row in enumerate(
            self.get("Amazonイベント!A2:X"), start=2
        ) if row]
    def update_amazon_event_cells(self,row_num:int,cells:list[tuple[int,object]]):
        """Update only selected zero-based columns in one Amazon event row."""
        if not cells:return
        data=[{"range":f"Amazonイベント!{chr(65+column)}{row_num}","values":[[value]]}
              for column,value in cells]
        self.svc.spreadsheets().values().batchUpdate(
            spreadsheetId=self.sid,
            body={"valueInputOption":"USER_ENTERED","data":data}
        ).execute()
    def update_amazon_event_item_count(self,row_num:int,value:int):
        """Update only the Item Count cell of one Amazon event row."""
        self.svc.spreadsheets().values().update(
            spreadsheetId=self.sid,range=f"Amazonイベント!R{row_num}",
            valueInputOption="RAW",body={"values":[[value]]},
        ).execute()
    def amazon_order_header_ids(self)->set[str]:
        return {str(r[0]).strip() for r in self.get("Amazon注文ヘッダ!A2:A")
                if r and str(r[0]).strip()}
    def amazon_order_creation_event_rows(self)->list[list]:
        return self.get("Amazonイベント!F2:R")
    def amazon_order_header_rows(self)->list[tuple[int,list]]:
        return [(i,r) for i,r in enumerate(
            self.get("Amazon注文ヘッダ!A2:O"),start=2
        ) if r and str(r[0]).strip()]
    def update_amazon_order_headers(self,rows:list[tuple[int,list]]):
        self.update_rows("Amazon注文ヘッダ",rows)
    def cancel_amazon_order_header(self,row_num:int):
        """Set only one Amazon order header Order Status cell to cancelled."""
        self.svc.spreadsheets().values().update(
            spreadsheetId=self.sid,range=f"Amazon注文ヘッダ!F{row_num}",
            valueInputOption="RAW",body={"values":[["cancelled"]]},
        ).execute()
    def import_ids(self)->set[str]:
        return {r[0] for r in self.get("取込データ!A2:A") if r}
    def receipt_ids(self)->set[str]:
        return {r[0] for r in self.get("レシート!A2:A") if r}
    def import_index(self)->dict[str,tuple[int,str]]:
        out={}
        for i,r in enumerate(self.get("取込データ!A2:L"),start=2):
            if r: out[r[0]]=(i,r[10] if len(r)>10 else "")
        return out
    def expense_index(self)->dict[str,int]:
        return {r[0]:i for i,r in enumerate(self.get("支出明細!A2:A"),start=2) if r}
    def expense_records(self)->dict[str,tuple[int,list]]:
        """Read the whole canonical ledger record only for guarded updates."""
        return {str(row[0]): (number, list(row) + [""] * max(0, 13-len(row)))
                for number,row in enumerate(self.get("支出明細!A2:M"), start=2) if row and row[0]}
    def category_rules(self):
        """Rules are optional until an explicit, separately approved UI install."""
        if "カテゴリ自動分類ルール" not in set(self.sheet_titles()):
            return []
        return self.get("カテゴリ自動分類ルール!A2:R")
    def ensure_category_rule_sheet(self):
        """Explicit write path only; never called by routine imports or UI refresh."""
        self.ensure_sheet("カテゴリ自動分類ルール", HEADERS["カテゴリ自動分類ルール"])
    def _category_workflow_defaults(self):
        from .category_backfill_ui import BACKFILL_CONFIRM_HEADERS, BACKFILL_UI_HEADERS
        from .category_rule_ui import UI_HEADERS
        return {"rule": list(UI_HEADERS), "backfill": list(BACKFILL_UI_HEADERS),
                "confirm": list(BACKFILL_CONFIRM_HEADERS)}

    def _check_category_workflow_input(self, prior):
        if prior is not None and self._compact_category_helper() and prior != self._category_workflow_last_read:
            from .monthly_projection import ProjectionError
            raise ProjectionError("category_workflow_input_changed")

    @staticmethod
    def _workflow_logical_row(section, physical, *, compact=False):
        cells=list(physical)+[""]*max(0, 12-len(physical))
        if section == "backfill":
            return cells[:5]+cells[6:11]
        if section == "confirm":
            return cells[:4]+[cells[6]]
        from .category_rule_choices import logical_choice
        cells[4]=logical_choice(cells[4])
        if compact:
            from .compact_categories import logical_rule_row
            return logical_rule_row(cells)
        return cells[:12]

    @staticmethod
    def _workflow_physical_row(section, logical, *, compact=False, header=False):
        cells=list(logical)
        def checkbox(value):
            text=str(value).strip().upper()
            return True if text == "TRUE" else False if text == "FALSE" else value
        if section == "backfill":
            cells += [""]*max(0, 10-len(cells))
            cells[4]=checkbox(cells[4])
            return cells[:5]+[""]+cells[5:10]+[""]
        if section == "confirm":
            cells += [""]*max(0, 5-len(cells))
            cells[2]=checkbox(cells[2])
            return cells[:4]+["", "", cells[4]]+[""]*5
        cells += [""]*max(0, 12-len(cells))
        cells[4]=checkbox(cells[4]); cells[5]=checkbox(cells[5])
        if not header:
            from .category_rule_choices import physical_choice
            cells[4]=physical_choice(cells[4])
        if compact:
            from .compact_categories import physical_rule_row
            return physical_rule_row(cells, header=header)
        return cells[:12]

    def _category_workflow_blocks(self):
        """Read the three independently-approved actions from one visible tab."""
        defaults=self._category_workflow_defaults(); titles=set(self.sheet_titles())
        if CATEGORY_WORKFLOW_SHEET not in titles:
            legacy={}
            legacy["rule"]=(defaults["rule"], self.get(f"{LEGACY_CATEGORY_RULE_UI_SHEET}!A2:L")
                            if LEGACY_CATEGORY_RULE_UI_SHEET in titles else [])
            legacy["backfill"]=(defaults["backfill"], self.get("カテゴリ過去反映!A2:J")
                                if "カテゴリ過去反映" in titles else [])
            legacy["confirm"]=(defaults["confirm"], self.get("カテゴリ過去反映確認!A2:E")
                               if "カテゴリ過去反映確認" in titles else [])
            return legacy
        compact=bool(self._compact_category_helper())
        if compact:
            sheet=next(s for s in self._sheet_metadata()["sheets"]
                       if s["properties"]["title"] == CATEGORY_WORKFLOW_SHEET)
            extent=sheet["properties"]["gridProperties"]["rowCount"]
            values=[]
            for first in range(1,extent+1,1000):
                last=min(extent,first+999)
                page=self.get_raw(f"'{CATEGORY_WORKFLOW_SHEET}'!A{first}:L{last}")
                values.extend(page+[[] for _ in range(last-first+1-len(page))])
        else:
            values=self.get(f"{CATEGORY_WORKFLOW_SHEET}!A1:L1000")
        markers={key: next((index for index,row in enumerate(values)
                            if row and row[0] == marker), None)
                 for key,marker in CATEGORY_WORKFLOW_MARKERS.items()}
        blocks={}
        ordered=["rule", "backfill", "confirm"]
        for position,key in enumerate(ordered):
            marker=markers[key]
            if marker is None:
                blocks[key]=(defaults[key], [])
                continue
            following=[markers[after] for after in ordered[position+1:]
                       if markers[after] is not None]
            end=min(following) if following else len(values)
            header=list(values[marker+1]) if marker+1 < len(values) and values[marker+1] else defaults[key]
            rows=[]
            for row in values[marker+2:end]:
                if any(str(value).strip() for value in row):
                    rows.append(self._workflow_logical_row(key, row, compact=compact))
            if compact and key == "rule":
                header=defaults[key]
            blocks[key]=(header, rows)
        if compact:
            from .compact_categories import digest
            self._category_workflow_last_read=digest(values)
        return blocks

    def _ensure_category_workflow_sheet(self):
        titles=set(self.sheet_titles())
        if CATEGORY_WORKFLOW_SHEET in titles:
            return
        if LEGACY_CATEGORY_RULE_UI_SHEET in titles:
            legacy=next(sheet for sheet in self._sheet_metadata()["sheets"]
                        if sheet["properties"]["title"] == LEGACY_CATEGORY_RULE_UI_SHEET)
            self.svc.spreadsheets().batchUpdate(spreadsheetId=self.sid, body={"requests":[{
                "updateSheetProperties":{"properties":{"sheetId":legacy["properties"]["sheetId"],
                    "title":CATEGORY_WORKFLOW_SHEET},"fields":"title"}
            }]}).execute()
        else:
            self.svc.spreadsheets().batchUpdate(spreadsheetId=self.sid, body={"requests":[{
                "addSheet":{"properties":{"title":CATEGORY_WORKFLOW_SHEET}}
            }]}).execute()
        self._invalidate_sheet_metadata()

    def _workflow_positions(self, blocks):
        row_num=6 if CATEGORY_REQUEST_SHEET in self.sheet_titles() else 1
        positions={}
        for section in ("rule", "backfill", "confirm"):
            header,rows=blocks[section]
            positions[section]={"marker":row_num, "header":row_num+1,
                                "start":row_num+2, "count":len(rows)}
            row_num += len(rows)+3
        return positions

    def _workflow_backfill_requests(self, sheet_id, start_row, rows):
        """Controls for the historical-preview block, scoped to its rows only."""
        start_rule={"condition":{"type":"ONE_OF_RANGE","values":[
            {"userEnteredValue":f"='{CATEGORY_WORKFLOW_SHEET}'!$Z$2:$Z$1000"}
        ]},"strict":True,"showCustomUi":True}
        end_rule={"condition":{"type":"ONE_OF_RANGE","values":[
            {"userEnteredValue":f"='{CATEGORY_WORKFLOW_SHEET}'!$AA$2:$AA$1000"}
        ]},"strict":True,"showCustomUi":True}
        normalizer="ARRAYFORMULA(IF(ISNUMBER(%s),TEXT(%s,\"yyyy-mm\"),TO_TEXT(%s)))"
        home_raw="'ホーム'!$I$3:$I$5001"; start_raw="$C$2:$C$1000"; end_raw="$D$2:$D$1000"
        month_pattern="^[0-9]{4}-(0[1-9]|1[0-2])$"
        home_text=normalizer%(home_raw,home_raw,home_raw); start_text=normalizer%(start_raw,start_raw,start_raw); end_text=normalizer%(end_raw,end_raw,end_raw)
        start_formula=("=LET(home_text,"+home_text+",home,FILTER(home_text,REGEXMATCH(home_text,\""+month_pattern+"\")),current_text,"+start_text+",current,IFERROR(FILTER(current_text,REGEXMATCH(current_text,\""+month_pattern+"\")),\"\"),values,TOCOL({home;current},1),SORT(UNIQUE(values),1,TRUE))")
        end_formula=("=LET(home_text,"+home_text+",home,FILTER(home_text,REGEXMATCH(home_text,\""+month_pattern+"\")),current_text,"+end_text+",current,IFERROR(FILTER(current_text,REGEXMATCH(current_text,\""+month_pattern+"\")),\"\"),values,TOCOL({home;current},1),{\"過去すべて\";SORT(UNIQUE(values),1,TRUE)})")
        header_row=start_row-1
        requests=[
            {"updateCells":{"range":{"sheetId":sheet_id,"startRowIndex":header_row-1,"endRowIndex":header_row,
                "startColumnIndex":2,"endColumnIndex":4},"rows":[{"values":[
                    {"note":"開始月を選びます。終了月が「過去すべて」のとき開始月は使用しません。選択だけでは反映されません。"},
                    {"note":"終了月を選びます。「過去すべて」は取込済みの全期間を対象にします。作成済み固定プレビューは変更しません。"},
                ]}],"fields":"note"}},
            {"updateCells":{"range":{"sheetId":sheet_id,"startRowIndex":0,"endRowIndex":1,"startColumnIndex":25,"endColumnIndex":26},"rows":[{"values":[{"userEnteredValue":{"stringValue":"過去反映・開始月候補"}}]}],"fields":"userEnteredValue"}},
            {"updateCells":{"range":{"sheetId":sheet_id,"startRowIndex":1,"endRowIndex":2,"startColumnIndex":25,"endColumnIndex":26},"rows":[{"values":[{"userEnteredValue":{"formulaValue":start_formula}}]}],"fields":"userEnteredValue"}},
            {"updateCells":{"range":{"sheetId":sheet_id,"startRowIndex":0,"endRowIndex":1,"startColumnIndex":26,"endColumnIndex":27},"rows":[{"values":[{"userEnteredValue":{"stringValue":"過去反映・終了月候補"}}]}],"fields":"userEnteredValue"}},
            {"updateCells":{"range":{"sheetId":sheet_id,"startRowIndex":1,"endRowIndex":2,"startColumnIndex":26,"endColumnIndex":27},"rows":[{"values":[{"userEnteredValue":{"formulaValue":end_formula}}]}],"fields":"userEnteredValue"}},
            {"updateDimensionProperties":{"range":{"sheetId":sheet_id,"dimension":"COLUMNS","startIndex":25,"endIndex":27},"properties":{"hiddenByUser":True},"fields":"hiddenByUser"}},
        ]
        for row_num,row in enumerate(rows, start=start_row):
            requests.extend([
                {"setDataValidation":{"range":{"sheetId":sheet_id,"startRowIndex":row_num-1,"endRowIndex":row_num,"startColumnIndex":2,"endColumnIndex":3},"rule":start_rule}},
                {"setDataValidation":{"range":{"sheetId":sheet_id,"startRowIndex":row_num-1,"endRowIndex":row_num,"startColumnIndex":3,"endColumnIndex":4},"rule":end_rule}},
                {"setDataValidation":{"range":{"sheetId":sheet_id,"startRowIndex":row_num-1,"endRowIndex":row_num,"startColumnIndex":4,"endColumnIndex":5},"rule":{"condition":{"type":"BOOLEAN"},"strict":True,"showCustomUi":True}}},
                {"repeatCell":{"range":{"sheetId":sheet_id,"startRowIndex":row_num-1,"endRowIndex":row_num,"startColumnIndex":4,"endColumnIndex":5},"cell":{"userEnteredFormat":{"backgroundColor":{"red":1,"green":0.95,"blue":0.75},"horizontalAlignment":"CENTER","verticalAlignment":"MIDDLE"}},"fields":"userEnteredFormat(backgroundColor,horizontalAlignment,verticalAlignment)"}},
            ])
            if len(row)>3 and row[3] == "過去すべて":
                requests.append({"repeatCell":{"range":{"sheetId":sheet_id,"startRowIndex":row_num-1,"endRowIndex":row_num,"startColumnIndex":2,"endColumnIndex":3},"cell":{"userEnteredFormat":{"backgroundColor":{"red":0.93,"green":0.93,"blue":0.93},"textFormat":{"foregroundColor":{"red":0.45,"green":0.45,"blue":0.45},"italic":True}}},"fields":"userEnteredFormat(backgroundColor,textFormat)"}})
        if self._compact_category_helper():
            from .compact_category_sync import backfill_month_choices
            sheet=next(s for s in self._sheet_metadata()["sheets"] if s["properties"]["sheetId"]==sheet_id)
            requests=backfill_month_choices(requests,sheet_id=sheet_id,title=CATEGORY_WORKFLOW_SHEET,rows=rows,
                extent=sheet["properties"]["gridProperties"]["rowCount"],store=self.projection_store())
        return requests

    def _configure_category_workflow(self, blocks, positions):
        meta=self._sheet_metadata(); sheet=next(value for value in meta["sheets"]
            if value["properties"]["title"] == CATEGORY_WORKFLOW_SHEET)
        sheet_id=sheet["properties"]["sheetId"]
        helper=next((value for value in meta["sheets"] if value["properties"]["title"] == CATEGORY_RULE_UI_HELPER_SHEET), None)
        used=max(position["start"]+position["count"] for position in positions.values())
        requests=[]
        offset=positions["rule"]["marker"]-1
        column_count=sheet["properties"].get("gridProperties", {}).get("columnCount", 0)
        if column_count < 27:
            requests.append({"appendDimension":{"sheetId":sheet_id,"dimension":"COLUMNS","length":27-column_count}})
        requests += [
            {"repeatCell":{"range":{"sheetId":sheet_id,"startRowIndex":offset,"endRowIndex":used,"startColumnIndex":0,"endColumnIndex":12},"cell":{"userEnteredFormat":{"backgroundColor":{"red":1,"green":1,"blue":1},"textFormat":{"foregroundColor":{"red":0.16,"green":0.20,"blue":0.23},"bold":False},"wrapStrategy":"WRAP","verticalAlignment":"MIDDLE"}},"fields":"userEnteredFormat(backgroundColor,textFormat,wrapStrategy,verticalAlignment)"}},
            {"setDataValidation":{"range":{"sheetId":sheet_id,"startRowIndex":offset,"endRowIndex":sheet["properties"]["gridProperties"]["rowCount"] if self._compact_category_helper() else 1000,"startColumnIndex":0,"endColumnIndex":6}}},
            {"updateDimensionProperties":{"range":{"sheetId":sheet_id,"dimension":"COLUMNS","startIndex":0,"endIndex":6},"properties":{"hiddenByUser":False},"fields":"hiddenByUser"}},
            {"updateDimensionProperties":{"range":{"sheetId":sheet_id,"dimension":"COLUMNS","startIndex":6,"endIndex":25},"properties":{"hiddenByUser":True},"fields":"hiddenByUser"}},
            {"updateSheetProperties":{"properties":{"sheetId":sheet_id,"gridProperties":{"frozenRowCount":4 if offset else 2}},"fields":"gridProperties.frozenRowCount"}},
        ]
        for section in ("rule", "backfill", "confirm"):
            marker=positions[section]["marker"]-1; header=positions[section]["header"]-1
            requests += [
                {"repeatCell":{"range":{"sheetId":sheet_id,"startRowIndex":marker,"endRowIndex":marker+1,"startColumnIndex":0,"endColumnIndex":6},"cell":{"userEnteredFormat":{"backgroundColor":{"red":0.11,"green":0.24,"blue":0.38},"textFormat":{"foregroundColor":{"red":1,"green":1,"blue":1},"bold":True},"wrapStrategy":"WRAP"}},"fields":"userEnteredFormat(backgroundColor,textFormat,wrapStrategy)"}},
                {"repeatCell":{"range":{"sheetId":sheet_id,"startRowIndex":header,"endRowIndex":header+1,"startColumnIndex":0,"endColumnIndex":6},"cell":{"userEnteredFormat":{"backgroundColor":{"red":0.20,"green":0.36,"blue":0.50},"textFormat":{"foregroundColor":{"red":1,"green":1,"blue":1},"bold":True},"wrapStrategy":"WRAP"}},"fields":"userEnteredFormat(backgroundColor,textFormat,wrapStrategy)"}},
            ]
        rule_position=positions["rule"]
        if helper and rule_position["count"]:
            compact=self._compact_category_helper()
            if compact:
                from .compact_categories import controls
                requests.extend(controls(sheet_id,compact,rule_position["start"],rule_position["count"]))
            else:
                from .sheets_ui_actions import expense_helper_growth_requests
                growth=expense_helper_growth_requests(helper,rule_position['start']+rule_position['count']-1)
                requests.extend(growth)
                if growth:self._invalidate_sheet_metadata()
                requests.extend(category_rule_ui_control_requests(sheet_id=sheet_id, helper_sheet_id=helper["properties"]["sheetId"], row_count=rule_position["count"], start_row=rule_position["start"]))
        if rule_position["count"]:
            from .category_rule_choices import choice_validation
            requests.append(choice_validation(sheet_id,rule_position["start"],rule_position["count"]))
            requests.append({"updateDimensionProperties":{"range":{"sheetId":sheet_id,
                "dimension":"COLUMNS","startIndex":4,"endIndex":5},
                "properties":{"pixelSize":110},"fields":"pixelSize"}})
        # Explain the two approvals beside the operator's controls.
        requests.append({"updateCells":{"start":{"sheetId":sheet_id,
            "rowIndex":rule_position["header"]-1,"columnIndex":4},"rows":[{"values":[
                {"note":"登録する／登録しない／未選択から選択。登録しないはこの候補の追加登録を見送る選択で、既存ルールの停止ではありません。"},
                {"note":"チェックして上部の「入力内容を処理する」を実行すると、同じ条件の全月の「その他／未分類」に選択カテゴリを反映します。分類済みは変更しません。完了した条件は別月分も一覧から除きます。月を限定する場合は下の「2」「3」を使います。"}]}],"fields":"note"}})
        backfill_position=positions["backfill"]
        requests.extend(self._workflow_backfill_requests(sheet_id, backfill_position["start"], blocks["backfill"][1]))
        confirm_position=positions["confirm"]
        for row_num,row in enumerate(blocks["confirm"][1], start=confirm_position["start"]):
            if len(row)>4 and str(row[4]).strip():
                requests.extend([
                    {"setDataValidation":{"range":{"sheetId":sheet_id,"startRowIndex":row_num-1,"endRowIndex":row_num,"startColumnIndex":2,"endColumnIndex":3},"rule":{"condition":{"type":"BOOLEAN"},"strict":True,"showCustomUi":True}}},
                    {"repeatCell":{"range":{"sheetId":sheet_id,"startRowIndex":row_num-1,"endRowIndex":row_num,"startColumnIndex":2,"endColumnIndex":3},"cell":{"userEnteredFormat":{"backgroundColor":{"red":1,"green":0.95,"blue":0.75},"horizontalAlignment":"CENTER","verticalAlignment":"MIDDLE"}},"fields":"userEnteredFormat(backgroundColor,horizontalAlignment,verticalAlignment)"}},
                ])
        for legacy_title in ("カテゴリ過去反映", "カテゴリ過去反映確認"):
            legacy=next((value for value in meta["sheets"] if value["properties"]["title"] == legacy_title), None)
            if legacy:
                requests.append({"updateSheetProperties":{"properties":{"sheetId":legacy["properties"]["sheetId"],"hidden":True},"fields":"hidden"}})
        self.svc.spreadsheets().batchUpdate(spreadsheetId=self.sid,body={"requests":requests}).execute()

    def _replace_category_workflow_section(self, section, rows, header):
        prior=getattr(self,"_category_workflow_last_read",None)
        blocks=self._category_workflow_blocks()
        self._check_category_workflow_input(prior)
        blocks[section]=(list(header), [list(row) for row in rows])
        self._write_category_workflow_blocks(blocks)

    def _write_category_workflow_blocks(self, blocks):
        self._ensure_category_workflow_sheet(); positions=self._workflow_positions(blocks)
        offset=positions["rule"]["marker"]-1
        values=[]
        compact=bool(self._compact_category_helper())
        for key in ("rule", "backfill", "confirm"):
            block_header,block_rows=blocks[key]
            values.append([CATEGORY_WORKFLOW_MARKERS[key]])
            values.append(self._workflow_physical_row(key, block_header, compact=compact, header=True))
            values.extend(self._workflow_physical_row(key, row, compact=compact) for row in block_rows)
            values.append([""])
        if compact:
            # Atomic values replacement, including stale tail clearing. No
            # intermediate blank input surface and no 1,000-row truncation.
            from .compact_categories import _cells
            sheet=next(s for s in self._sheet_metadata()["sheets"] if s["properties"]["title"] == CATEGORY_WORKFLOW_SHEET)
            extent=sheet["properties"]["gridProperties"]["rowCount"]
            sheet_id=sheet["properties"]["sheetId"]
            requests=[]
            if len(values)+offset>extent:
                requests.append({"appendDimension":{"sheetId":sheet_id,"dimension":"ROWS","length":len(values)+offset-extent}})
            request=_cells(sheet_id,offset+1,0,values,12)
            request["updateCells"]["range"]["endRowIndex"]=max(extent,len(values)+offset)
            requests.append(request)
            self.svc.spreadsheets().batchUpdate(spreadsheetId=self.sid,body={"requests":requests}).execute()
            self._invalidate_sheet_metadata()
            self._category_workflow_last_read=None
        else:
            self.clear(f"{CATEGORY_WORKFLOW_SHEET}!A{offset+1}:L1000")
            self.svc.spreadsheets().values().update(spreadsheetId=self.sid,range=f"{CATEGORY_WORKFLOW_SHEET}!A{offset+1}",valueInputOption="RAW",body={"values":values}).execute()
        self._configure_category_workflow(blocks, positions)

    def ensure_category_rule_ui_sheet(self, header):
        """Create the opt-in mobile request surface only after UI approval."""
        self._ensure_category_workflow_sheet()

    def replace_category_rule_ui_rows(self, rows:list[list], header:list[str]):
        """Replace the UI-owned values without inserting rows below controls."""
        self._replace_category_workflow_section("rule", rows, header)
    def category_rule_ui_rows(self):
        return self._category_workflow_blocks()["rule"][1]

    def consume_category_rule_ui_past_choice(self, condition_key: str, result: dict) -> bool:
        """Consume only the processed past checkbox for its stable condition key."""
        blocks=self._category_workflow_blocks()
        start=self._workflow_positions(blocks)["rule"]["start"]
        for row_num, row in enumerate(blocks["rule"][1], start=start):
            cells=list(row) + [""] * max(0, 12-len(row))
            if cells[6] != condition_key:
                continue
            state=str(result.get("state", "processed"))
            suffix=(f"\n過去プレビュー: {state}"
                    + (f" / 要求={result['request_id']}" if result.get("request_id") else ""))
            self.svc.spreadsheets().values().batchUpdate(spreadsheetId=self.sid, body={
                "valueInputOption": "USER_ENTERED", "data": [
                    {"range": f"{CATEGORY_WORKFLOW_SHEET}!B{row_num}", "values": [[str(cells[1]) + suffix]]},
                    {"range": f"{CATEGORY_WORKFLOW_SHEET}!F{row_num}", "values": [[False]]},
                ],
            }).execute()
            self._category_workflow_last_read=None
            return True
        return False
    def ensure_category_backfill_sheets(self):
        """Create the two narrow audit tabs only on explicit backfill preview."""
        from .category_backfill import BACKFILL_REQUEST_HEADERS, BACKFILL_TARGET_HEADERS
        self.ensure_sheet("カテゴリ過去反映要求", BACKFILL_REQUEST_HEADERS)
        self.ensure_sheet("カテゴリ過去反映対象", BACKFILL_TARGET_HEADERS)
    def category_backfill_requests(self):
        if "カテゴリ過去反映要求" not in set(self.sheet_titles()): return []
        return self.get("カテゴリ過去反映要求!A2:K")
    def category_backfill_targets(self):
        if "カテゴリ過去反映対象" not in set(self.sheet_titles()): return []
        return self.get("カテゴリ過去反映対象!A2:O")
    def update_expense_categories(self, rows:list[tuple[int,str,str]]):
        """Backfill's sole ledger mutation: the existing F:G category cells."""
        if not rows: return
        self._invalidate_expense_projection("支出明細",[(n,n) for n,_,_ in rows])
        self.svc.spreadsheets().values().batchUpdate(
            spreadsheetId=self.sid, body={"valueInputOption":"RAW", "data":[
                {"range":f"支出明細!F{row_num}:G{row_num}", "values":[[major,minor]]}
                for row_num,major,minor in rows
            ]},
        ).execute()
    def update_expense_fields(self,row_num:int,cells:list[list]):
        """Atomic field-only correction; immutable IDs/source/status excluded."""
        if not cells:return
        if row_num<2 or any(c not in {1,2,3,4,5,6,7,11} for c,_ in cells):
            raise ValueError("expense_correction_field_invalid")
        self._invalidate_expense_projection("支出明細",[(row_num,row_num)])
        self.svc.spreadsheets().values().batchUpdate(spreadsheetId=self.sid,body={
            "valueInputOption":"RAW","data":[
                {"range":f"'支出明細'!{chr(65+c)}{row_num}","values":[[value]]} for c,value in cells
            ]}).execute(num_retries=0)
    def _configure_backfill_mobile_sheet(self, title, header, hidden_from, control_rows, ignored_start_rows=()):
        """Render backfill controls only on the rows that can be actioned.

        These sheets are regenerated views.  Clearing their values must not
        leave old checkbox validation (or header styling copied by inserted
        rows) on a later detail/blank row.
        """
        self.ensure_sheet(title, header)
        meta=self._sheet_metadata()
        sheet=next(value for value in meta["sheets"] if value["properties"]["title"] == title)
        sheet_id=sheet["properties"]["sheetId"]
        is_backfill=title == "カテゴリ過去反映"
        widths=(130,70,55,60,50) if is_backfill else (120,150,90,90)
        clear_start,clear_end=(2,5) if is_backfill else (2,3)
        requests=[]
        if is_backfill:
            # Z was the former helper's final column.  AA is the independent
            # end-month helper, so extend only legacy-sized grids once before
            # addressing AA; never grow the sheet on later refreshes.
            column_count=sheet["properties"].get("gridProperties", {}).get("columnCount", 0)
            if column_count < 27:
                requests.append({"appendDimension":{"sheetId":sheet_id,"dimension":"COLUMNS",
                    "length":27-column_count}})
        requests += [
            {"updateDimensionProperties":{"range":{"sheetId":sheet_id,"dimension":"COLUMNS","startIndex":index,"endIndex":index+1},"properties":{"pixelSize":width},"fields":"pixelSize"}}
            for index,width in enumerate(widths)
        ] + [
            {"repeatCell":{"range":{"sheetId":sheet_id,"startRowIndex":0,"endRowIndex":1},"cell":{"userEnteredFormat":{"backgroundColor":{"red":0.11,"green":0.24,"blue":0.38},"textFormat":{"foregroundColor":{"red":1,"green":1,"blue":1},"bold":True},"wrapStrategy":"WRAP"}},"fields":"userEnteredFormat(backgroundColor,textFormat,wrapStrategy)"}},
            # Reset the generated body so an INSERT_ROWS-era dark header or
            # checkbox cannot survive on a now-empty/detail row.
            {"repeatCell":{"range":{"sheetId":sheet_id,"startRowIndex":1,"endRowIndex":1000,"startColumnIndex":0,"endColumnIndex":len(header)},"cell":{"userEnteredFormat":{"backgroundColor":{"red":1,"green":1,"blue":1},"textFormat":{"foregroundColor":{"red":0.16,"green":0.20,"blue":0.23},"bold":False},"wrapStrategy":"WRAP","verticalAlignment":"MIDDLE"}},"fields":"userEnteredFormat(backgroundColor,textFormat,wrapStrategy,verticalAlignment)"}},
            {"setDataValidation":{"range":{"sheetId":sheet_id,"startRowIndex":1,"endRowIndex":1000,"startColumnIndex":clear_start,"endColumnIndex":clear_end}}},
            # The prior one-period layout hid D:G.  Explicitly re-expose the
            # new five input columns so E's checkbox cannot stay hidden after
            # a header-only migration.
            {"updateDimensionProperties":{"range":{"sheetId":sheet_id,"dimension":"COLUMNS","startIndex":0,"endIndex":hidden_from},"properties":{"hiddenByUser":False},"fields":"hiddenByUser"}},
            {"updateDimensionProperties":{"range":{"sheetId":sheet_id,"dimension":"COLUMNS","startIndex":hidden_from,"endIndex":len(header)},"properties":{"hiddenByUser":True},"fields":"hiddenByUser"}},
            {"updateSheetProperties":{"properties":{"sheetId":sheet_id,"gridProperties":{"frozenRowCount":1}},"fields":"gridProperties.frozenRowCount"}},
        ]
        if is_backfill:
            # Keep each independently selected valid month in the range source
            # so a home-month refresh never drops a current selection.  The
            # end control alone adds the explicit all-history choice.
            start_rule={"condition":{"type":"ONE_OF_RANGE","values":[
                {"userEnteredValue":"='カテゴリ過去反映'!$Z$2:$Z$1000"}
            ]},"strict":True,"showCustomUi":True}
            end_rule={"condition":{"type":"ONE_OF_RANGE","values":[
                {"userEnteredValue":"='カテゴリ過去反映'!$AA$2:$AA$1000"}
            ]},"strict":True,"showCustomUi":True}
            month_pattern="^[0-9]{4}-(0[1-9]|1[0-2])$"
            # FILTER must return normalized values, not the pre-normalized
            # date serial.  ``TO_TEXT`` alone in REGEXMATCH only tests the
            # display value; it does not change the values FILTER returns.
            normalizer = "ARRAYFORMULA(IF(ISNUMBER(%s),TEXT(%s,\"yyyy-mm\"),TO_TEXT(%s)))"
            home_raw = "'ホーム'!$I$3:$I$5001"
            start_raw = "$C$2:$C$1000"
            end_raw = "$D$2:$D$1000"
            home_text = normalizer % (home_raw, home_raw, home_raw)
            start_text = normalizer % (start_raw, start_raw, start_raw)
            end_text = normalizer % (end_raw, end_raw, end_raw)
            start_formula=(
                "=LET(home_text," + home_text + ",home,FILTER(home_text,REGEXMATCH(home_text,\""
                + month_pattern + "\")),current_text," + start_text
                + ",current,IFERROR(FILTER(current_text,REGEXMATCH(current_text,\"" + month_pattern
                + "\")),\"\"),values,TOCOL({home;current},1),SORT(UNIQUE(values),1,TRUE))"
            )
            end_formula=(
                "=LET(home_text," + home_text + ",home,FILTER(home_text,REGEXMATCH(home_text,\""
                + month_pattern + "\")),current_text," + end_text
                + ",current,IFERROR(FILTER(current_text,REGEXMATCH(current_text,\"" + month_pattern
                + "\")),\"\"),values,TOCOL({home;current},1),{\"過去すべて\";SORT(UNIQUE(values),1,TRUE)})"
            )
            requests.extend([
                {"updateCells":{"range":{"sheetId":sheet_id,"startRowIndex":0,"endRowIndex":1,
                    "startColumnIndex":2,"endColumnIndex":4},"rows":[{"values":[
                        {"note":"開始月を選びます。終了月が「過去すべて」のとき開始月は使用しません。選択だけでは反映されません。"},
                        {"note":"終了月を選びます。「過去すべて」は取込済みの全期間を対象にします。作成済み固定プレビューは変更しません。"},
                    ]}],"fields":"note"}},
                {"updateCells":{"range":{"sheetId":sheet_id,"startRowIndex":0,"endRowIndex":1,
                    "startColumnIndex":25,"endColumnIndex":26},"rows":[{"values":[{"userEnteredValue":
                    {"stringValue":"過去反映・開始月候補"}}]}],"fields":"userEnteredValue"}},
                {"updateCells":{"range":{"sheetId":sheet_id,"startRowIndex":1,"endRowIndex":2,
                    "startColumnIndex":25,"endColumnIndex":26},
                    "rows":[{"values":[{"userEnteredValue":{"formulaValue":start_formula}}]}],
                    "fields":"userEnteredValue"}},
                {"updateCells":{"range":{"sheetId":sheet_id,"startRowIndex":0,"endRowIndex":1,
                    "startColumnIndex":26,"endColumnIndex":27},"rows":[{"values":[{"userEnteredValue":
                    {"stringValue":"過去反映・終了月候補"}}]}],"fields":"userEnteredValue"}},
                {"updateCells":{"range":{"sheetId":sheet_id,"startRowIndex":1,"endRowIndex":2,
                    "startColumnIndex":26,"endColumnIndex":27},
                    "rows":[{"values":[{"userEnteredValue":{"formulaValue":end_formula}}]}],
                    "fields":"userEnteredValue"}},
                {"updateDimensionProperties":{"range":{"sheetId":sheet_id,"dimension":"COLUMNS",
                    "startIndex":25,"endIndex":27},"properties":{"hiddenByUser":True},"fields":"hiddenByUser"}},
            ])
        for row_num in control_rows:
            if is_backfill:
                requests.extend([
                    {"setDataValidation":{"range":{"sheetId":sheet_id,"startRowIndex":row_num-1,
                        "endRowIndex":row_num,"startColumnIndex":2,"endColumnIndex":3},"rule":start_rule}},
                    {"setDataValidation":{"range":{"sheetId":sheet_id,"startRowIndex":row_num-1,
                        "endRowIndex":row_num,"startColumnIndex":3,"endColumnIndex":4},"rule":end_rule}},
                ])
            checkbox_column=4 if is_backfill else 2
            requests.extend([
                {"setDataValidation":{"range":{"sheetId":sheet_id,"startRowIndex":row_num-1,"endRowIndex":row_num,"startColumnIndex":checkbox_column,"endColumnIndex":checkbox_column+1},"rule":{"condition":{"type":"BOOLEAN"},"strict":True,"showCustomUi":True}}},
                {"repeatCell":{"range":{"sheetId":sheet_id,"startRowIndex":row_num-1,"endRowIndex":row_num,"startColumnIndex":checkbox_column,"endColumnIndex":checkbox_column+1},"cell":{"userEnteredFormat":{"backgroundColor":{"red":1,"green":0.95,"blue":0.75},"horizontalAlignment":"CENTER","verticalAlignment":"MIDDLE"}},"fields":"userEnteredFormat(backgroundColor,horizontalAlignment,verticalAlignment)"}},
            ])
        if is_backfill:
            for row_num in ignored_start_rows:
                requests.append({"repeatCell":{"range":{"sheetId":sheet_id,"startRowIndex":row_num-1,
                    "endRowIndex":row_num,"startColumnIndex":2,"endColumnIndex":3},"cell":{"userEnteredFormat":{
                        "backgroundColor":{"red":0.93,"green":0.93,"blue":0.93},
                        "textFormat":{"foregroundColor":{"red":0.45,"green":0.45,"blue":0.45},"italic":True},
                    }},"fields":"userEnteredFormat(backgroundColor,textFormat)"}})
        self.svc.spreadsheets().batchUpdate(spreadsheetId=self.sid,body={"requests":requests}).execute()
    def ensure_category_backfill_ui_sheet(self, header):
        self._ensure_category_workflow_sheet()
    def ensure_category_backfill_confirmation_sheet(self, header):
        self._ensure_category_workflow_sheet()
    def _replace_backfill_rows(self, title, header, hidden_from, rows, control_rows, ignored_start_rows=()):
        """Replace generated values in place; never insert physical rows."""
        self.ensure_sheet(title, header)
        self.clear(f"{title}!A2:{chr(64 + len(header))}")
        if rows:
            self.svc.spreadsheets().values().update(
                # Month controls are text identifiers (YYYY-MM), never dates.
                # USER_ENTERED reparses 2026-07 as a first-of-month serial,
                # which would later leak into the dropdown source.
                spreadsheetId=self.sid, range=f"{title}!A2", valueInputOption="RAW",
                body={"values":rows},
            ).execute()
        self._configure_backfill_mobile_sheet(title, header, hidden_from, control_rows, ignored_start_rows)
    def replace_category_backfill_ui_rows(self, rows:list[list], header:list[str]):
        self._replace_category_workflow_section("backfill", rows, header)
    def replace_category_backfill_confirmation_rows(self, rows:list[list], header:list[str]):
        self._replace_category_workflow_section("confirm", rows, header)
    def category_backfill_ui_rows(self):
        return self._category_workflow_blocks()["backfill"][1]

    def category_backfill_ui_table(self):
        header,rows=self._category_workflow_blocks()["backfill"]
        return list(header), [list(row) for row in rows]
    def category_backfill_confirmation_rows(self):
        return self._category_workflow_blocks()["confirm"][1]
    def expense_rows_for_import(self,import_id:str)->list[tuple[int,list]]:
        return [(i,r) for i,r in enumerate(self.get("支出明細!A2:M"),start=2)
                if len(r)>10 and r[10]==import_id]
    def ensure_expense_status_column(self):
        current=self.get("支出明細!M1:M1")
        if not current or not current[0] or current[0][0] != "計上状態":
            self.svc.spreadsheets().values().update(
                spreadsheetId=self.sid,range="支出明細!M1",valueInputOption="RAW",
                body={"values":[["計上状態"]]}
            ).execute()
        rows=self.get("支出明細!A2:M")
        if rows:
            statuses=[[r[12] if len(r)>12 and r[12] else "active"] for r in rows]
            if any(len(r)<=12 or not r[12] for r in rows):
                self.set_raw_range("支出明細!M2",statuses)
