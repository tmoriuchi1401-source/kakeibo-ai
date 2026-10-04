from __future__ import annotations
import mimetypes, uuid
from collections.abc import Callable
from .gemini_ai import GeminiAI
from .receipt_privacy_gate import evaluate_receipt_privacy
from .receipt_pdf_units import (PdfUnitManifestStore, SinglePageGrouping, document_result,
                                is_pdf, observe_pdf, update_document_status)
from .medical_receipt_privacy import Classification
from .sheets import SheetsDB
from .utils import now_jst_string, canonical_hash
from .receipt_validation import validate_receipt_result, apply_receipt_policy
from .receipt_review_snapshot import save_candidate
from .bank_income import (INCOME_HEADERS, INCOME_SHEET, RECEIPT_BUYBACK_REASON,
                          RECEIPT_BUYBACK_SOURCE, income_id, validate_income_rows)

class ReceiptPipeline:
    def __init__(self,db:SheetsDB,ai:GeminiAI | None, *, medical_review_observer=None,
                 gemini_factory:Callable[[], GeminiAI] | None=None, pdf_manifest_store=None,
                 pdf_unit_intake=None, clock=now_jst_string):
        self.db=db; self.ai=ai
        self.medical_review_observer=medical_review_observer
        self._gemini_factory=gemini_factory
        self.pdf_manifest_store = pdf_manifest_store or PdfUnitManifestStore()
        self.pdf_unit_intake = pdf_unit_intake
        self.clock = clock
        # Restrictive source provenance survives retries within this pipeline.
        # Callers carry known_source_classification across pipeline lifetimes.
        self._source_privacy: dict[str, Classification] = {}
    def _require_ai(self):
        if self.ai is None:
            if self._gemini_factory is None:
                raise RuntimeError("未設定: GEMINI_API_KEY")
            self.ai=self._gemini_factory()
    def _analyze(self, image_bytes, mime_type, categories, source_policy, *, destination=None):
        if is_pdf(image_bytes, mime_type):
            from .receipt_privacy_gate import ReceiptPrivacyBlocked
            raise ReceiptPrivacyBlocked()
        self._require_ai()
        if destination is not None:
            from urllib.parse import urlsplit
            actual=urlsplit(self.ai.client._api_client._http_options.base_url)
            if (destination != "https://generativelanguage.googleapis.com"
                    or actual.scheme != "https" or actual.hostname != "generativelanguage.googleapis.com"
                    or actual.port not in (None,443) or actual.username or actual.password):
                raise RuntimeError("receipt_destination_mismatch")
        return self.ai.analyze_receipt(image_bytes,mime_type,categories,**source_policy)

    def reanalyze_bytes(self, image_bytes, mime_type, source_id, *, destination):
        """Explicit fixed-scope caller only; no dedupe mutation or Sheets write."""
        if is_pdf(image_bytes, mime_type):
            return {"status":"privacy_blocked", "classification":"sensitive_unknown",
                    "reason":"pdf_requires_page_units"}
        known=self._source_privacy.get(source_id)
        policy={"known_source_classification":known} if known else {}
        gate=evaluate_receipt_privacy(image_bytes,mime_type,**policy)
        if gate.classification != "normal" or not gate.gemini_allowed:
            self._source_privacy[source_id]=gate.classification
            return {"status":"privacy_blocked","classification":gate.classification}
        self._require_ai()
        categories=self.db.categories()
        result=self._analyze(image_bytes,mime_type,categories,policy,destination=destination)
        notes=self._posting_policy(result,categories,image_bytes,mime_type,gate)
        ok = not notes
        return {"status":"analyzed" if ok else "needs_review", "parsed":result.model_dump(),"issues":notes}

    @staticmethod
    def _posting_policy(result,categories,payload,mime_type,gate):
        from .receipt_text_extraction import _extract_receipt_text
        text=''
        if result.payment_method or any('調整' in x.name or '補正' in x.name for x in result.items):
            try:
                extracted=_extract_receipt_text(payload,mime_type)
                if extracted.status=='extracted' and extracted.observation_complete:text=extracted.text or ''
            except Exception:pass
        issues,_=apply_receipt_policy(result,categories,text=text,gate=gate)
        return issues

    @staticmethod
    def _kind_issues(result, gate):
        if result.transaction_kind == "unknown":
            return ["購入・買取の別が不明"]
        if gate.buyback_evidence != (result.transaction_kind == "buyback"):
            return ["原本の買取表示と取引種別が不一致。支出・収入とも自動記帳しない"]
        return []

    def _buyback_income(self, import_id, result, raw_hash):
        """Return the existing row for replay, or a row safe to append."""
        if INCOME_SHEET not in self.db.sheet_titles():
            raise RuntimeError("receipt_income_sheet_missing")
        if self.db.get(f"{INCOME_SHEET}!A1:J1") != [INCOME_HEADERS]:
            raise RuntimeError("receipt_income_header_mismatch")
        existing=validate_income_rows(self.db.get(f"{INCOME_SHEET}!A2:J"))
        row=[income_id(import_id),result.date,result.total,"その他確認済収入",
             result.merchant,"",import_id,RECEIPT_BUYBACK_SOURCE,
             RECEIPT_BUYBACK_REASON,raw_hash]
        if row[0] in existing and existing[row[0]] != row:
            raise RuntimeError("receipt_income_existing_content_conflict")
        return row, row[0] in existing

    def process_bytes(self,image_bytes:bytes,mime_type:str,source_id:str,image_url:str="", *,
                      known_source_classification: Classification | None = None):
        if is_pdf(image_bytes, mime_type):
            if self.pdf_unit_intake is not None:
                return self.pdf_unit_intake.process(image_bytes,source_id,
                    known_source_classification=known_source_classification)
            return self._process_pdf(image_bytes, source_id, image_url,
                                     known_source_classification=known_source_classification)
        return self._process_image_bytes(image_bytes, mime_type, source_id, image_url,
                                        known_source_classification=known_source_classification)

    def _process_pdf(self, content, source_id, image_url, *, known_source_classification):
        from hashlib import sha256
        restrictions = self.pdf_manifest_store.restrictions(source_id, sha256(content).hexdigest())
        observations = observe_pdf(content, source_id,
            known_source_classification=self._source_privacy.get(source_id, known_source_classification),
            known_page_classifications=restrictions)
        report = document_result(observations)
        self.pdf_manifest_store.save(report)
        if len(observations.pages) != 1:
            # Observation and privacy do not establish transaction grouping.
            # Stop before AI resolution, any ledger access or Medical handoff.
            return report
        # observe_pdf is complete before resolving Gemini or touching the ledger.
        for unit, record in zip(SinglePageGrouping().units(observations.pages), report['units']):
            page = unit.observation
            if page.classification != 'normal' or page._payload is None:
                continue
            # Older production may have imported this entire PDF under its
            # parent ID. A new page ID must not create a second receipt.
            if f'receipt:{source_id}' in self.db.import_ids():
                record['status']='legacy_import_review_required'
                continue
            from .receipt_privacy_gate import ReceiptPrivacyBlocked
            try:
                outcome = self._process_image_bytes(page._payload, 'image/png', unit.source_id,
                                                   image_url, observe_medical=False)
            except ReceiptPrivacyBlocked:
                # The final exact-payload gate may fail on a later OCR pass.
                # That unit remains private while other observed pages proceed.
                outcome = {'status': 'privacy_blocked', 'classification': 'sensitive_unknown',
                           'extraction_status': 'extraction_failed'}
            state = outcome['status']
            if state == 'skipped' and outcome.get('reason') == 'already_imported':
                # A review import marker also suppresses replay. It does not
                # prove the unit is resolved, so reconcile the ledger status.
                rows = self.db.get('取込データ!A2:L')
                matching = [row for row in rows if row and row[0] == record['import_id']]
                state = ('imported' if len(matching) == 1 and len(matching[0]) > 8
                         and matching[0][8] == '解析済' else 'needs_review')
            if state == 'privacy_blocked':
                state = 'medical_pending' if outcome['classification'] == 'medical' else 'privacy_pending'
                record['classification'] = outcome['classification']
                record['extraction_status'] = outcome.get('extraction_status', 'extraction_failed')
            record['status'] = state
            update_document_status(report)
            self.pdf_manifest_store.save(report)
        update_document_status(report)
        self.pdf_manifest_store.save(report)
        return report

    def _process_image_bytes(self,image_bytes:bytes,mime_type:str,source_id:str,image_url:str="", *,
                             known_source_classification: Classification | None = None,
                             observe_medical=True):
        if is_pdf(image_bytes, mime_type):
            from .receipt_privacy_gate import ReceiptPrivacyBlocked
            raise ReceiptPrivacyBlocked()
        import_id=f"receipt:{source_id}"
        if import_id in self.db.import_ids(): return {"status":"skipped","reason":"already_imported"}
        known_source_classification = self._source_privacy.get(source_id, known_source_classification)
        if known_source_classification in ("medical", "payroll", "sensitive_unknown"):
            self._source_privacy[source_id] = known_source_classification
        source_policy = ({"known_source_classification": known_source_classification}
                         if known_source_classification is not None else {})
        privacy=evaluate_receipt_privacy(image_bytes,mime_type,**source_policy)
        if privacy.classification != "normal" or not privacy.gemini_allowed:
            self._source_privacy[source_id] = privacy.classification
            medical_shadow_status = None
            if (observe_medical and privacy.classification == "medical"
                    and self.medical_review_observer is not None):
                try:
                    observed = self.medical_review_observer.observe(source_id=source_id, gate=privacy)
                    medical_shadow_status = getattr(observed, "action", "observed")
                except Exception:
                    # Shadow review generation must never weaken or replace the
                    # existing fail-closed privacy decision.
                    medical_shadow_status = "handoff_failed"
            result = {
                "status":"privacy_blocked",
                "classification":privacy.classification,
                "reason_code":privacy.reason_code,
                "gemini_allowed":privacy.gemini_allowed,
                "extraction_status":privacy.extraction_status,
                "extraction_method":privacy.extraction_method,
                "text_present":privacy.text_present,
                "medical_payment_amount":privacy.medical_payment_amount,
                "medical_candidate_count":privacy.medical_candidate_count,
                "category":privacy.category,
            }
            if medical_shadow_status is not None:
                result["medical_shadow_status"] = medical_shadow_status
            return result
        self._require_ai()
        cats=self.db.categories(); result=self._analyze(image_bytes,mime_type,cats,source_policy)
        notes=self._posting_policy(result,cats,image_bytes,mime_type,privacy)
        ok = not notes
        receipt_id=f"R-{source_id}"
        status="解析済" if ok else "要確認"
        receipt_row=[receipt_id,result.date,result.merchant,result.total,result.payment_method,image_url,status,self.clock(),"; ".join(notes+[result.note] if result.note else notes)]
        raw_hash=canonical_hash(result.model_dump())
        income_row=None
        income_exists=False
        if ok and result.transaction_kind == "buyback":
            # Check the shared ledger before writing even the receipt row.
            income_row,income_exists=self._buyback_income(import_id,result,raw_hash)
        import_row=[import_id,self.clock(),"receipt",source_id,result.date,result.merchant,result.total,result.payment_method,status,"",raw_hash,"; ".join(notes)]
        if any(note.startswith("明細合計") for note in notes):
            # The rejected item list never reaches 支出明細. Save the values
            # actually used by validation before the import commit marker.
            save_candidate(self.db, import_id, raw_hash, result)
        if receipt_id not in self.db.receipt_ids():
            self.db.append("レシート",[receipt_row])
        if not ok:
            # The import row is the commit marker and must be written last.
            self.db.append("取込データ",[import_row])
            return {"status":"needs_review","receipt":result.model_dump(),"issues":notes}
        if income_row is not None:
            if not income_exists:
                self.db.append_raw(INCOME_SHEET,[income_row])
            actual=validate_income_rows(self.db.get(f"{INCOME_SHEET}!A2:J"))
            if actual.get(income_row[0]) != income_row:
                raise RuntimeError("receipt_income_readback_mismatch")
            self.db.append("取込データ",[import_row])
            return {"status":"imported","kind":"buyback","items":len(result.items),"total":result.total}
        rows=[]
        for idx,item in enumerate(result.items,1):
            spend_id=f"{receipt_id}-{idx:02d}"
            rows.append([spend_id,result.date,result.merchant,item.name,item.amount,item.major_category,item.minor_category,result.payment_method,"receipt",receipt_id,import_id,item.note,"active"])
        self.db.ensure_expense_status_column()
        existing_expense_ids=set(self.db.expense_index())
        self.db.append("支出明細",[row for row in rows if row[0] not in existing_expense_ids])
        # A present import ID means all earlier receipt materialization completed.
        self.db.append("取込データ",[import_row])
        return {"status":"imported","items":len(rows),"total":result.total}

