from __future__ import annotations
from datetime import datetime, timezone
from .google_clients import sheets_service

HEADERS={
"支出明細":["支出ID","日付","店舗","商品名","金額","大カテゴリ","小カテゴリ","支払方法","データ元","レシートID","取込ID","備考","計上状態"],
"レシート":["レシートID","日付","店舗","合計金額","支払方法","画像URL","解析状態","解析日時","備考"],
"カテゴリ":["大カテゴリ","小カテゴリ"],
"店舗":["店舗ID","店舗名","標準店舗名","備考"],
"取込データ":["取込ID","取込日時","データ元","元データID","日付","店舗","金額","支払方法","処理状態","統合先支出ID","元データハッシュ","備考"],
"Amazon注文":["Amazonキー","Order ID","ASIN","注文日","商品名","数量","商品金額","支払方法","大カテゴリ","小カテゴリ","備考","データハッシュ","最終取込日時"],
"商品マスタ":["商品ID","商品名","大カテゴリ","小カテゴリ","備考","最終更新日時"],
}

class SheetsDB:
    def __init__(self, spreadsheet_id:str): self.sid=spreadsheet_id; self.svc=sheets_service()
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
