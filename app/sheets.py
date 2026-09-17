from __future__ import annotations
from datetime import datetime, timezone
import re
from .google_clients import sheets_service

CATEGORY_SEPARATOR = "｜"
CATEGORY_RULE_UI_SHEET = "カテゴリ自動分類"
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
                                      row_count: int) -> list[dict]:
    """Return controls for the rows actually rendered by the rule UI.

    The minor choice is a native range dropdown whose source is a row-specific
    formula in the established hidden category helper.  It deliberately uses
    one validation request per rendered row: Sheets otherwise freezes a
    range-backed source when the rule is filled down.
    """
    if row_count <= 0:
        return []
    end_row = row_count + 1
    helper_rows = []
    for row_num in range(2, end_row + 1):
        helper_rows.append({"values": [{"userEnteredValue": {"formulaValue": (
            "=IFERROR(TRANSPOSE(UNIQUE(FILTER('カテゴリ'!$B$2:$B,"
            f"'カテゴリ'!$A$2:$A='{CATEGORY_RULE_UI_SHEET}'!C{row_num}))),\"\")"
        )}}]})
    requests = [
        {"updateCells": {"range": {"sheetId": helper_sheet_id,
            "startRowIndex": 1, "endRowIndex": end_row,
            "startColumnIndex": CATEGORY_RULE_UI_HELPER_COLUMN,
            "endColumnIndex": CATEGORY_RULE_UI_HELPER_COLUMN + 1},
            "rows": helper_rows, "fields": "userEnteredValue"}},
        {"setDataValidation": {"range": {"sheetId": sheet_id,
            "startRowIndex": 1, "endRowIndex": end_row,
            "startColumnIndex": 2, "endColumnIndex": 3}, "rule": {
                "condition": {"type": "ONE_OF_RANGE", "values": [
                    {"userEnteredValue": "='カテゴリ'!$A$2:$A"}
                ]}, "strict": True, "showCustomUi": True,
                "inputMessage": "カテゴリマスタの大カテゴリを選択してください。"}}},
        {"setDataValidation": {"range": {"sheetId": sheet_id,
            "startRowIndex": 1, "endRowIndex": end_row,
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
    for row_num in range(2, end_row + 1):
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
"要確認":["確認ID","優先度","日付","データ元","店舗","金額","状態","推奨対応","備考",
       "ユーザー判断","統合先取込ID","カテゴリ（大｜小）","小カテゴリ（従来）","ユーザー備考","反映結果",
       "Amazon候補","Amazon候補数","Amazon注文候補選択","Amazon候補ID","Amazon選択状態"],
"支出一覧":["日付","店舗","商品名","金額","大カテゴリ","小カテゴリ","支払方法","データ元","備考","支出ID"],
"カテゴリ自動分類ルール":["ルールID","条件種別","データ元","口座別名","請求名","店舗名","商品ID","商品名","金額","大カテゴリ","小カテゴリ","承認元支出ID","承認日時","revision","有効","適用件数","最終適用支出ID","最終適用日時"],
}

class SheetsDB:
    def __init__(self, spreadsheet_id:str, service=None):
        self.sid=spreadsheet_id; self.svc=service or sheets_service()
    def sheet_titles(self):
        meta=self.svc.spreadsheets().get(spreadsheetId=self.sid).execute()
        return [s["properties"]["title"] for s in meta["sheets"]]
    def ensure_schema(self, categories:list[tuple[str,str]]|None=None):
        titles=set(self.sheet_titles()); req=[]
        for title in HEADERS:
            if title not in titles: req.append({"addSheet":{"properties":{"title":title}}})
        if req: self.svc.spreadsheets().batchUpdate(spreadsheetId=self.sid,body={"requests":req}).execute()
        for title, hdr in HEADERS.items():
            existing=self.get(f"{title}!1:1")
            if not existing or existing[0][:len(hdr)] != hdr:
                self.svc.spreadsheets().values().update(spreadsheetId=self.sid,range=f"{title}!A1",valueInputOption="RAW",body={"values":[hdr]}).execute()
        if categories:
            rows=[[a,b] for a,b in categories]
            self.svc.spreadsheets().values().clear(spreadsheetId=self.sid,range="カテゴリ!A2:B",body={}).execute()
            self.svc.spreadsheets().values().update(spreadsheetId=self.sid,range="カテゴリ!A2",valueInputOption="RAW",body={"values":rows}).execute()
    def get(self, rng:str):
        return self.svc.spreadsheets().values().get(spreadsheetId=self.sid,range=rng).execute().get("values",[])
    def append(self, sheet:str, rows:list[list]):
        if not rows:return
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
        meta = self.svc.spreadsheets().get(spreadsheetId=self.sid).execute()
        helper = next((sheet for sheet in meta.get("sheets", [])
                       if sheet["properties"].get("sheetId") == EXPENSE_CATEGORY_HELPER_ID), None)
        if not helper or not any(
            marker.get("metadataKey") == EXPENSE_CATEGORY_HELPER_MARKER
            and marker.get("metadataValue") == VERSION
            for marker in helper.get("developerMetadata", [])
        ):
            return
        from .sheets_ui_actions import expense_minor_category_validation_requests
        self.svc.spreadsheets().batchUpdate(spreadsheetId=self.sid, body={
            "requests": expense_minor_category_validation_requests(start, end),
        }).execute()
    def append_raw(self, sheet:str, rows:list[list]):
        """Append literal ledger values; do not interpret bank descriptions as formulas."""
        if not rows:return
        self.svc.spreadsheets().values().append(spreadsheetId=self.sid,range=f"{sheet}!A:A",valueInputOption="RAW",insertDataOption="INSERT_ROWS",body={"values":rows}).execute()
    def clear(self,rng:str):
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
        existing=self.get(f"{title}!1:1")
        if not existing or existing[0][:len(header)] != header:
            self.svc.spreadsheets().values().update(
                spreadsheetId=self.sid,range=f"{title}!A1",valueInputOption="RAW",
                body={"values":[header]},
            ).execute()
    def configure_review_validation(self, categories:list[tuple[str,str]],
                                    amazon_options_by_row:dict[int,list[str]]|None=None):
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
            rule(9,10,{"type":"ONE_OF_LIST","values":[{"userEnteredValue":x} for x in
                 ["支出として計上","重複として除外","レシートと統合","Amazon注文と照合","保留"]]}),
            rule(11,12,{"type":"ONE_OF_LIST","values":[{"userEnteredValue":x}
                 for x in combined_category_options(categories)]}),
            {"setDataValidation":{"range":{"sheetId":sheet_id,"startRowIndex":1,
             "startColumnIndex":12,"endColumnIndex":13}}},
        ]
        for row_num,options in (amazon_options_by_row or {}).items():
            if options:
                requests.append({"setDataValidation":{"range":{"sheetId":sheet_id,
                    "startRowIndex":row_num-1,"endRowIndex":row_num,
                    "startColumnIndex":17,"endColumnIndex":18},
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
        self.svc.spreadsheets().values().update(spreadsheetId=self.sid,range=f"{sheet}!A{row_num}",valueInputOption="USER_ENTERED",body={"values":[row]}).execute()
    def set_raw_range(self, rng:str, rows:list[list]):
        self.svc.spreadsheets().values().update(spreadsheetId=self.sid,range=rng,
            valueInputOption="RAW",body={"values":rows}).execute(num_retries=0)
    def update_row_raw(self,sheet:str,row_num:int,row:list):
        self.set_raw_range(f"'{sheet}'!A{row_num}",[row])
    def update_rows(self,sheet:str,rows:list[tuple[int,list]]):
        if not rows:return
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
    def ensure_category_rule_ui_sheet(self, header):
        """Create the opt-in mobile request surface only after UI approval."""
        self.ensure_sheet(CATEGORY_RULE_UI_SHEET, header)
        meta=self.svc.spreadsheets().get(spreadsheetId=self.sid).execute()
        sheet=next(value for value in meta["sheets"] if value["properties"]["title"] == CATEGORY_RULE_UI_SHEET)
        sheet_id=sheet["properties"]["sheetId"]
        requests=[
            {"updateDimensionProperties":{"range":{"sheetId":sheet_id,"dimension":"COLUMNS","startIndex":index,"endIndex":index+1},"properties":{"pixelSize":width},"fields":"pixelSize"}}
            for index,width in enumerate((90,100,60,60,40,40))
        ]
        requests += [
            {"repeatCell":{"range":{"sheetId":sheet_id,"startRowIndex":0,"endRowIndex":1},"cell":{"userEnteredFormat":{"backgroundColor":{"red":0.11,"green":0.24,"blue":0.38},"textFormat":{"foregroundColor":{"red":1,"green":1,"blue":1},"bold":True},"wrapStrategy":"WRAP"}},"fields":"userEnteredFormat(backgroundColor,textFormat,wrapStrategy)"}},
            # The actual rows get their controls only after refresh writes
            # them.  Applying range validation before INSERT_ROWS was the
            # cause of the controls drifting below the visible candidates.
            {"updateDimensionProperties":{"range":{"sheetId":sheet_id,"dimension":"COLUMNS","startIndex":0,"endIndex":6},"properties":{"hiddenByUser":False},"fields":"hiddenByUser"}},
            {"updateDimensionProperties":{"range":{"sheetId":sheet_id,"dimension":"COLUMNS","startIndex":6,"endIndex":len(header)},"properties":{"hiddenByUser":True},"fields":"hiddenByUser"}},
            {"updateSheetProperties":{"properties":{"sheetId":sheet_id,"gridProperties":{"frozenRowCount":1}},"fields":"gridProperties.frozenRowCount"}},
        ]
        self.svc.spreadsheets().batchUpdate(spreadsheetId=self.sid,body={"requests":requests}).execute()

    def replace_category_rule_ui_rows(self, rows:list[list], header:list[str]):
        """Replace the UI-owned values without inserting rows below controls."""
        self.ensure_category_rule_ui_sheet(header)
        self.clear(f"{CATEGORY_RULE_UI_SHEET}!A2:L")
        if rows:
            self.svc.spreadsheets().values().update(
                spreadsheetId=self.sid, range=f"{CATEGORY_RULE_UI_SHEET}!A2",
                valueInputOption="USER_ENTERED", body={"values": rows},
            ).execute()
        if not rows:
            return
        meta=self.svc.spreadsheets().get(spreadsheetId=self.sid).execute()
        sheet_id=next(value["properties"]["sheetId"] for value in meta["sheets"]
                      if value["properties"]["title"] == CATEGORY_RULE_UI_SHEET)
        helper_id=next(value["properties"]["sheetId"] for value in meta["sheets"]
                       if value["properties"]["title"] == CATEGORY_RULE_UI_HELPER_SHEET)
        self.svc.spreadsheets().batchUpdate(spreadsheetId=self.sid, body={"requests":
            category_rule_ui_control_requests(sheet_id=sheet_id, helper_sheet_id=helper_id,
                                              row_count=len(rows))
        }).execute()
    def category_rule_ui_rows(self):
        if CATEGORY_RULE_UI_SHEET not in set(self.sheet_titles()): return []
        return self.get(f"{CATEGORY_RULE_UI_SHEET}!A2:L")

    def consume_category_rule_ui_past_choice(self, condition_key: str, result: dict) -> bool:
        """Consume only the processed past checkbox for its stable condition key."""
        for row_num, row in enumerate(self.category_rule_ui_rows(), start=2):
            cells=list(row) + [""] * max(0, 12-len(row))
            if cells[6] != condition_key:
                continue
            state=str(result.get("state", "processed"))
            suffix=(f"\n過去プレビュー: {state}"
                    + (f" / 要求={result['request_id']}" if result.get("request_id") else ""))
            self.svc.spreadsheets().values().batchUpdate(spreadsheetId=self.sid, body={
                "valueInputOption": "USER_ENTERED", "data": [
                    {"range": f"{CATEGORY_RULE_UI_SHEET}!B{row_num}", "values": [[str(cells[1]) + suffix]]},
                    {"range": f"{CATEGORY_RULE_UI_SHEET}!F{row_num}", "values": [[False]]},
                ],
            }).execute()
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
        self.svc.spreadsheets().values().batchUpdate(
            spreadsheetId=self.sid, body={"valueInputOption":"RAW", "data":[
                {"range":f"支出明細!F{row_num}:G{row_num}", "values":[[major,minor]]}
                for row_num,major,minor in rows
            ]},
        ).execute()
    def _configure_backfill_mobile_sheet(self, title, header, hidden_from, control_rows):
        """Render backfill controls only on the rows that can be actioned.

        These sheets are regenerated views.  Clearing their values must not
        leave old checkbox validation (or header styling copied by inserted
        rows) on a later detail/blank row.
        """
        self.ensure_sheet(title, header)
        meta=self.svc.spreadsheets().get(spreadsheetId=self.sid).execute()
        sheet=next(value for value in meta["sheets"] if value["properties"]["title"] == title)
        sheet_id=sheet["properties"]["sheetId"]
        requests=[
            {"updateDimensionProperties":{"range":{"sheetId":sheet_id,"dimension":"COLUMNS","startIndex":index,"endIndex":index+1},"properties":{"pixelSize":width},"fields":"pixelSize"}}
            for index,width in enumerate((120,150,90,90))
        ] + [
            {"repeatCell":{"range":{"sheetId":sheet_id,"startRowIndex":0,"endRowIndex":1},"cell":{"userEnteredFormat":{"backgroundColor":{"red":0.11,"green":0.24,"blue":0.38},"textFormat":{"foregroundColor":{"red":1,"green":1,"blue":1},"bold":True},"wrapStrategy":"WRAP"}},"fields":"userEnteredFormat(backgroundColor,textFormat,wrapStrategy)"}},
            # Reset the generated body so an INSERT_ROWS-era dark header or
            # checkbox cannot survive on a now-empty/detail row.
            {"repeatCell":{"range":{"sheetId":sheet_id,"startRowIndex":1,"endRowIndex":1000,"startColumnIndex":0,"endColumnIndex":len(header)},"cell":{"userEnteredFormat":{"backgroundColor":{"red":1,"green":1,"blue":1},"textFormat":{"foregroundColor":{"red":0.16,"green":0.20,"blue":0.23},"bold":False},"wrapStrategy":"WRAP","verticalAlignment":"MIDDLE"}},"fields":"userEnteredFormat(backgroundColor,textFormat,wrapStrategy,verticalAlignment)"}},
            {"setDataValidation":{"range":{"sheetId":sheet_id,"startRowIndex":1,"endRowIndex":1000,"startColumnIndex":2,"endColumnIndex":3}}},
            {"updateDimensionProperties":{"range":{"sheetId":sheet_id,"dimension":"COLUMNS","startIndex":hidden_from,"endIndex":len(header)},"properties":{"hiddenByUser":True},"fields":"hiddenByUser"}},
            {"updateSheetProperties":{"properties":{"sheetId":sheet_id,"gridProperties":{"frozenRowCount":1}},"fields":"gridProperties.frozenRowCount"}},
        ]
        for row_num in control_rows:
            requests.extend([
                {"setDataValidation":{"range":{"sheetId":sheet_id,"startRowIndex":row_num-1,"endRowIndex":row_num,"startColumnIndex":2,"endColumnIndex":3},"rule":{"condition":{"type":"BOOLEAN"},"strict":True,"showCustomUi":True}}},
                {"repeatCell":{"range":{"sheetId":sheet_id,"startRowIndex":row_num-1,"endRowIndex":row_num,"startColumnIndex":2,"endColumnIndex":3},"cell":{"userEnteredFormat":{"backgroundColor":{"red":1,"green":0.95,"blue":0.75},"horizontalAlignment":"CENTER","verticalAlignment":"MIDDLE"}},"fields":"userEnteredFormat(backgroundColor,horizontalAlignment,verticalAlignment)"}},
            ])
        self.svc.spreadsheets().batchUpdate(spreadsheetId=self.sid,body={"requests":requests}).execute()
    def ensure_category_backfill_ui_sheet(self, header):
        self._configure_backfill_mobile_sheet("カテゴリ過去反映", header, 3, ())
    def ensure_category_backfill_confirmation_sheet(self, header):
        self._configure_backfill_mobile_sheet("カテゴリ過去反映確認", header, 4, ())
    def _replace_backfill_rows(self, title, header, hidden_from, rows, control_rows):
        """Replace generated values in place; never insert physical rows."""
        self.ensure_sheet(title, header)
        self.clear(f"{title}!A2:{chr(64 + len(header))}")
        if rows:
            self.svc.spreadsheets().values().update(
                spreadsheetId=self.sid, range=f"{title}!A2", valueInputOption="USER_ENTERED",
                body={"values":rows},
            ).execute()
        self._configure_backfill_mobile_sheet(title, header, hidden_from, control_rows)
    def replace_category_backfill_ui_rows(self, rows:list[list], header:list[str]):
        self._replace_backfill_rows("カテゴリ過去反映", header, 3, rows,
                                    range(2, len(rows)+2))
    def replace_category_backfill_confirmation_rows(self, rows:list[list], header:list[str]):
        self._replace_backfill_rows("カテゴリ過去反映確認", header, 4, rows,
                                    [row_num for row_num,row in enumerate(rows, start=2)
                                     if len(row) > 4 and str(row[4]).strip()])
    def category_backfill_ui_rows(self):
        if "カテゴリ過去反映" not in set(self.sheet_titles()): return []
        return self.get("カテゴリ過去反映!A2:G")
    def category_backfill_confirmation_rows(self):
        if "カテゴリ過去反映確認" not in set(self.sheet_titles()): return []
        return self.get("カテゴリ過去反映確認!A2:E")
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
                self.svc.spreadsheets().values().update(
                    spreadsheetId=self.sid,range="支出明細!M2",valueInputOption="RAW",
                    body={"values":statuses}
                ).execute()
