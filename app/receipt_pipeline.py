from __future__ import annotations
import mimetypes, uuid
from .gemini_ai import GeminiAI
from .sheets import SheetsDB
from .utils import now_jst_string, canonical_hash

class ReceiptPipeline:
    def __init__(self,db:SheetsDB,ai:GeminiAI): self.db=db; self.ai=ai
    def process_bytes(self,image_bytes:bytes,mime_type:str,source_id:str,image_url:str=""):
        import_id=f"receipt:{source_id}"
        if import_id in self.db.import_ids(): return {"status":"skipped","reason":"already_imported"}
        cats=self.db.categories(); result=self.ai.analyze_receipt(image_bytes,mime_type,cats)
        allowed=set(cats)
        invalid=[x for x in result.items if (x.major_category,x.minor_category) not in allowed]
        item_sum=sum(x.amount for x in result.items)
        tolerance=max(10, round(abs(result.total)*0.01))
        ok=(not invalid) and abs(item_sum-result.total)<=tolerance and bool(result.date)
        receipt_id=f"R-{source_id}"
        status="解析済" if ok else "要確認"
        notes=[]
        if invalid: notes.append("カテゴリ不正")
        if abs(item_sum-result.total)>tolerance: notes.append(f"明細合計{item_sum}≠レシート合計{result.total}")
        if not result.date: notes.append("日付不明")
        self.db.append("レシート",[[receipt_id,result.date,result.merchant,result.total,result.payment_method,image_url,status,now_jst_string(),"; ".join(notes+[result.note] if result.note else notes)]])
        raw_hash=canonical_hash(result.model_dump())
        self.db.append("取込データ",[[import_id,now_jst_string(),"receipt",source_id,result.date,result.merchant,result.total,result.payment_method,status,"",raw_hash,"; ".join(notes)]])
        if not ok: return {"status":"needs_review","receipt":result.model_dump(),"issues":notes}
        rows=[]
        for idx,item in enumerate(result.items,1):
            spend_id=f"{receipt_id}-{idx:02d}"
            rows.append([spend_id,result.date,result.merchant,item.name,item.amount,item.major_category,item.minor_category,result.payment_method,"receipt",receipt_id,import_id,item.note])
        self.db.append("支出明細",rows)
        return {"status":"imported","items":len(rows),"total":result.total}
