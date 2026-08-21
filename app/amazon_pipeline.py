from __future__ import annotations
import hashlib
import pandas as pd
from .gemini_ai import GeminiAI
from .sheets import SheetsDB
from .utils import canonical_hash, now_jst_string

def money(v)->int:
    if pd.isna(v): return 0
    s=str(v).replace(",","").replace("¥","").strip()
    try:return int(round(float(s)))
    except:return 0

def date_ymd(v)->str:
    if pd.isna(v):return ""
    d=pd.to_datetime(v,utc=True,errors="coerce")
    return "" if pd.isna(d) else d.tz_convert("Asia/Tokyo").strftime("%Y-%m-%d")

def load_amazon_rows(path:str):
    df=pd.read_csv(path)
    required={"Order ID","ASIN","Order Date","Product Name","Original Quantity","Total Amount","Payment Method Type"}
    miss=required-set(df.columns)
    if miss:
        raise ValueError("Amazon CSVに必要列がありません: "+", ".join(sorted(miss)))
    qty=pd.to_numeric(df["Original Quantity"],errors="coerce").fillna(0)
    df=df[qty>0].copy()  # 取消・調整行など quantity=0 を除外
    if df.duplicated(["Order ID","ASIN"]).any():
        dup=df[df.duplicated(["Order ID","ASIN"],keep=False)][["Order ID","ASIN"]]
        raise ValueError("有効行に Order ID + ASIN の重複があります。安全のため自動取込を停止しました: "
                         + dup.head(5).to_dict(orient="records").__repr__())
    return df


def shipping_fields(df:pd.DataFrame)->dict[str,tuple[str,int]]:
    """Return per-item ship date and per-order shipment count without retaining tracking IDs."""
    groups:dict[str,set[tuple[str,str]]]={}
    for _,r in df.iterrows():
        order_id=str(r["Order ID"])
        ship_date=date_ymd(r.get("Ship Date",""))
        tracking="" if pd.isna(r.get("Carrier Name & Tracking Number","")) else str(r.get("Carrier Name & Tracking Number","")).strip()
        if ship_date or tracking:
            groups.setdefault(order_id,set()).add((ship_date,tracking))
    counts={order_id:len(values) for order_id,values in groups.items()}
    return {
        f"{str(r['Order ID'])}|{str(r['ASIN'])}":(
            date_ymd(r.get("Ship Date","")),counts.get(str(r["Order ID"]),0),
        )
        for _,r in df.iterrows()
    }

class AmazonPipeline:
    def __init__(self,db:SheetsDB,ai:GeminiAI|None):
        self.db=db
        self.ai=ai

    @staticmethod
    def _row_obj(r):
        return {
            "Order ID":str(r["Order ID"]),
            "ASIN":str(r["ASIN"]),
            "Order Date":str(r["Order Date"]),
            "Product Name":str(r["Product Name"]),
            "Original Quantity":float(r["Original Quantity"]),
            "Total Amount":money(r["Total Amount"]),
            "Payment Method Type":str(r["Payment Method Type"]),
        }

    @staticmethod
    def _expense_id(key:str)->str:
        return "A-"+hashlib.sha256(key.encode("utf-8")).hexdigest()[:24]

    def import_csv(self,path:str,batch_size:int=20,baseline:bool=False):
        df=load_amazon_rows(path)
        shipping=shipping_fields(df)
        idx=self.db.amazon_index()
        baseline_keys=self.db.amazon_baseline_keys()
        master=self.db.product_master()
        cats=self.db.categories()
        allowed=set(cats)

        # Determine genuinely new/changed rows BEFORE calling Gemini.
        pending=[]
        unchanged=0
        for _,r in df.iterrows():
            obj=self._row_obj(r)
            key=f"{obj['Order ID']}|{obj['ASIN']}"
            h=canonical_hash(obj)
            if key not in idx:
                pending.append((r,key,h,"new"))
            else:
                row_num,old_hash=idx[key]
                if old_hash != h:
                    pending.append((r,key,h,"updated"))
                else:
                    unchanged += 1

        # First full-history import is only a baseline: no Gemini calls.
        if baseline:
            new_rows=[]
            update_rows=[]
            for r,key,h,kind in pending:
                asin=str(r["ASIN"])
                out=[key,str(r["Order ID"]),asin,date_ymd(r["Order Date"]),str(r["Product Name"]),
                     float(r["Original Quantity"]),money(r["Total Amount"]),str(r["Payment Method Type"]),
                     "","", "baseline",h,now_jst_string(),*shipping.get(key,("",0))]
                if kind=="new":
                    new_rows.append(out)
                else:
                    row_num,_=idx[key]
                    update_rows.append((row_num,out))
            # One append request for all new rows and one batchUpdate for all changed rows.
            # This avoids the Google Sheets per-user write quota (60 requests/minute).
            self.db.append("Amazon注文",new_rows)
            self.db.update_rows("Amazon注文",update_rows)
            return {
                "mode":"baseline","source_rows":len(df),"new":len(new_rows),
                "updated":len(update_rows),"unchanged":unchanged,"gemini_calls":0
            }

        if self.ai is None:
            raise RuntimeError("通常Amazon取込にはGemini設定が必要です")

        # Classify only ASINs involved in new/changed rows and absent from master.
        uniq={}
        for r,_,_,_ in pending:
            asin=str(r["ASIN"])
            if asin not in master:
                uniq[asin]={"asin":asin,"name":str(r["Product Name"])}

        classified={}
        vals=list(uniq.values())
        gemini_calls=0
        for i in range(0,len(vals),batch_size):
            batch=vals[i:i+batch_size]
            if not batch: continue
            gemini_calls += 1
            ans=self.ai.classify_products(batch,cats)
            for p in ans.products:
                pair=(p.major_category,p.minor_category)
                if pair not in allowed: pair=("その他","未分類")
                classified[p.asin]=(pair[0],pair[1],p.note)

        if classified:
            rows=[]
            for asin,(maj,minr,note) in classified.items():
                name=uniq[asin]["name"]
                rows.append([asin,name,maj,minr,note,now_jst_string()])
                master[asin]=(maj,minr,name)
            self.db.append("商品マスタ",rows)

        new_rows=[]
        update_rows=[]
        expense_new=[]
        expense_updates=[]
        expense_idx=self.db.expense_index()
        materialized=[]
        for r,key,h,kind in pending:
            asin=str(r["ASIN"])
            maj,minr,_=master.get(asin,("その他","未分類",""))
            baseline_update=kind=="updated" and key in baseline_keys
            out=[key,str(r["Order ID"]),asin,date_ymd(r["Order Date"]),str(r["Product Name"]),
                 float(r["Original Quantity"]),money(r["Total Amount"]),str(r["Payment Method Type"]),
                 maj,minr,"baseline" if baseline_update else "incremental",h,now_jst_string(),
                 *shipping.get(key,("",0))]
            if kind=="new":
                new_rows.append(out)
            else:
                row_num,_=idx[key]
                update_rows.append((row_num,out))

            if baseline_update:
                continue
            expense_id=self._expense_id(key)
            import_id=f"amazon:{str(r['Order ID'])}"
            expense=[expense_id,date_ymd(r["Order Date"]),"Amazon.co.jp",str(r["Product Name"]),
                     money(r["Total Amount"]),maj,minr,str(r["Payment Method Type"]),"Amazon","",import_id,
                     f"Amazonキー={key}","active"]
            if expense_id in expense_idx:
                expense_updates.append((expense_idx[expense_id],expense))
            else:
                expense_new.append(expense)
            materialized.append((r,key))

        self.db.append("Amazon注文",new_rows)
        self.db.update_rows("Amazon注文",update_rows)
        if materialized:
            self.db.ensure_expense_status_column()
        self.db.append("支出明細",expense_new)
        self.db.update_rows("支出明細",expense_updates)

        # Store one canonical import row per order for receipt reconciliation.
        import_idx=self.db.import_index()
        order_ids={str(r["Order ID"]) for r,_ in materialized}
        import_new=[]; import_updates=[]
        for order_id in sorted(order_ids):
            group=df[df["Order ID"].astype(str)==order_id]
            total=sum(money(v) for v in group["Total Amount"])
            first=group.iloc[0]
            import_id=f"amazon:{order_id}"
            raw={"order_id":order_id,"date":date_ymd(first["Order Date"]),"amount":total,
                 "keys":sorted(f"{order_id}|{str(x)}" for x in group["ASIN"])}
            row=[import_id,now_jst_string(),"Amazon",order_id,raw["date"],"Amazon.co.jp",total,
                 str(first["Payment Method Type"]),"canonical_amazon","",canonical_hash(raw),
                 f"商品明細={len(group)}件; Amazon商品単位を支出計上"]
            if import_id in import_idx:
                row_num,old_hash=import_idx[import_id]
                if old_hash != row[10]: import_updates.append((row_num,row))
            else:
                import_new.append(row)
        self.db.append("取込データ",import_new)
        self.db.update_rows("取込データ",import_updates)

        return {
            "mode":"incremental","source_rows":len(df),"pending":len(pending),
            "new":len(new_rows),"updated":len(update_rows),"unchanged":unchanged,
            "new_products":len(classified),"gemini_calls":gemini_calls,
            "expense_new":len(expense_new),"expense_updated":len(expense_updates),
            "order_import_new":len(import_new),"order_import_updated":len(import_updates),
        }
