from __future__ import annotations
from datetime import datetime, timezone
from .google_clients import sheets_service

CATEGORY_SEPARATOR = "｜"


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
"Amazon照合候補":["候補ID","カード取込ID","Order ID","候補順位","カード日","注文日","カード金額",
              "注文金額","差額","差額率","日付差","商品数","商品概要","大カテゴリ",
              "支払方法","データ種別","注文fingerprint","選択表示","生成日時","発送日","発送日差","発送数"],
"商品マスタ":["商品ID","商品名","大カテゴリ","小カテゴリ","備考","最終更新日時"],
"要確認":["確認ID","優先度","日付","データ元","店舗","金額","状態","推奨対応","備考",
       "ユーザー判断","統合先取込ID","カテゴリ（大｜小）","小カテゴリ（従来）","ユーザー備考","反映結果",
       "Amazon候補","Amazon候補数","Amazon注文候補選択","Amazon候補ID","Amazon選択状態"],
"支出一覧":["日付","店舗","商品名","金額","大カテゴリ","小カテゴリ","支払方法","データ元","備考","支出ID"],
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
        self.svc.spreadsheets().values().append(spreadsheetId=self.sid,range=f"{sheet}!A:A",valueInputOption="USER_ENTERED",insertDataOption="INSERT_ROWS",body={"values":rows}).execute()
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
        self.svc.spreadsheets().batchUpdate(
            spreadsheetId=self.sid,body={"requests":requests}
        ).execute()
    def format_date_column(self,sheet_title:str,column_index:int=0):
        meta=self.svc.spreadsheets().get(spreadsheetId=self.sid).execute()
        sheet_id=next(s["properties"]["sheetId"] for s in meta["sheets"]
                      if s["properties"]["title"]==sheet_title)
        self.svc.spreadsheets().batchUpdate(
            spreadsheetId=self.sid,body={"requests":[{"repeatCell":{"range":{
                "sheetId":sheet_id,"startRowIndex":1,
                "startColumnIndex":column_index,"endColumnIndex":column_index+1},
                "cell":{"userEnteredFormat":{"numberFormat":{"type":"DATE","pattern":"yyyy/mm/dd"}}},
                "fields":"userEnteredFormat.numberFormat"}}]}
        ).execute()
    def update_row(self,sheet:str,row_num:int,row:list):
        self.svc.spreadsheets().values().update(spreadsheetId=self.sid,range=f"{sheet}!A{row_num}",valueInputOption="USER_ENTERED",body={"values":[row]}).execute()
    def update_rows(self,sheet:str,rows:list[tuple[int,list]]):
        if not rows:return
        data=[{"range":f"{sheet}!A{row_num}","values":[row]} for row_num,row in rows]
        self.svc.spreadsheets().values().batchUpdate(
            spreadsheetId=self.sid,
            body={"valueInputOption":"USER_ENTERED","data":data}
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
    def import_ids(self)->set[str]:
        return {r[0] for r in self.get("取込データ!A2:A") if r}
    def import_index(self)->dict[str,tuple[int,str]]:
        out={}
        for i,r in enumerate(self.get("取込データ!A2:L"),start=2):
            if r: out[r[0]]=(i,r[10] if len(r)>10 else "")
        return out
    def expense_index(self)->dict[str,int]:
        return {r[0]:i for i,r in enumerate(self.get("支出明細!A2:A"),start=2) if r}
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
