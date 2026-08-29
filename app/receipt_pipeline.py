from __future__ import annotations
import mimetypes, uuid
from .gemini_ai import GeminiAI
from .medical_receipt_input import MedicalReceiptScreening, screen_medical_receipt
from .models import ReceiptItem, ReceiptResult
from .sheets import SheetsDB
from .utils import now_jst_string, canonical_hash

class ReceiptPipeline:
    def __init__(self,db:SheetsDB,ai:GeminiAI,medical_screener=screen_medical_receipt):
        self.db=db; self.ai=ai; self.medical_screener=medical_screener
    def process_bytes(self,image_bytes:bytes,mime_type:str,source_id:str,image_url:str=""):
        import_id=f"receipt:{source_id}"
        if import_id in self.db.import_ids(): return {"status":"skipped","reason":"already_imported"}
        screening=self.medical_screener(image_bytes,mime_type)
        analysis=screening.analysis
        if analysis.classification != "non_medical":
            if analysis.classification == "medical" and analysis.amount is not None:
                result=ReceiptResult(
                    merchant="", date="", total=analysis.amount, payment_method="",
                    items=[ReceiptItem(
                        name="医療費", amount=analysis.amount,
                        major_category="医療・保険", minor_category="病院",
                        note="medical_local",
                    )],
                    note=self._medical_note(screening),
                )
                return self._record_result(result,source_id,image_url,medical_local=True)
            return self._record_local_review(screening,source_id,image_url)
        cats=self.db.categories(); result=self.ai.analyze_receipt(image_bytes,mime_type,cats)
        return self._record_result(result,source_id,image_url)

    @staticmethod
    def _medical_note(screening:MedicalReceiptScreening)->str:
        analysis=screening.analysis
        parts=["medical_local",f"classification={analysis.classification}",
               f"source_method={screening.extraction}",f"reason={screening.reason_code}"]
        if analysis.amount_label: parts.append(f"amount_label={analysis.amount_label}")
        return "; ".join(parts)

    def _record_local_review(self,screening:MedicalReceiptScreening,source_id:str,image_url:str):
        analysis=screening.analysis
        import_id=f"receipt:{source_id}"; receipt_id=f"R-{source_id}"
        note=self._medical_note(screening); now=now_jst_string()
        amount=analysis.amount if analysis.amount is not None else ""
        self.db.append("レシート",[[receipt_id,"","",amount,"",image_url,"要確認",now,note]])
        raw_hash=canonical_hash({
            "classification":analysis.classification,
            "amount":analysis.amount,
            "amount_label":analysis.amount_label,
            "reason":screening.reason_code,
        })
        self.db.append("取込データ",[[import_id,now,"receipt",source_id,"","",amount,"",
                                      "要確認","",raw_hash,note]])
        return {"status":"needs_review","classification":analysis.classification,
                "amount":analysis.amount,"issues":[screening.reason_code]}

    def _record_result(self,result:ReceiptResult,source_id:str,image_url:str="",*,medical_local=False):
        import_id=f"receipt:{source_id}"
        cats=self.db.categories()
        allowed=set(cats)
        invalid=[x for x in result.items if (x.major_category,x.minor_category) not in allowed]
        item_sum=sum(x.amount for x in result.items)
        tolerance=max(10, round(abs(result.total)*0.01))
        ok=(not invalid) and abs(item_sum-result.total)<=tolerance and bool(result.date) and bool(result.merchant)
        receipt_id=f"R-{source_id}"
        status="解析済" if ok else "要確認"
        notes=[]
        if invalid: notes.append("カテゴリ不正")
        if abs(item_sum-result.total)>tolerance: notes.append(f"明細合計{item_sum}≠レシート合計{result.total}")
        if not result.date: notes.append("日付不明")
        if not result.merchant: notes.append("店舗不明")
        receipt_note="; ".join(notes+[result.note] if result.note else notes)
        import_note="; ".join(notes+([result.note] if medical_local and result.note else []))
        self.db.append("レシート",[[receipt_id,result.date,result.merchant,result.total,result.payment_method,image_url,status,now_jst_string(),receipt_note]])
        raw_hash=canonical_hash(result.model_dump())
        self.db.append("取込データ",[[import_id,now_jst_string(),"receipt",source_id,result.date,result.merchant,result.total,result.payment_method,status,"",raw_hash,import_note]])
        if not ok: return {"status":"needs_review","receipt":result.model_dump(),"issues":notes,
                           "classification":"medical" if medical_local else "non_medical"}
        self.db.ensure_expense_status_column()
        rows=[]
        for idx,item in enumerate(result.items,1):
            spend_id=f"{receipt_id}-{idx:02d}"
            rows.append([spend_id,result.date,result.merchant,item.name,item.amount,item.major_category,item.minor_category,result.payment_method,"receipt",receipt_id,import_id,item.note,"active"])
        self.db.append("支出明細",rows)
        return {"status":"imported","items":len(rows),"total":result.total}
