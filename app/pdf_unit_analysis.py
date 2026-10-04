"""One exact fresh PNG through existing Gemini receipt parsing, bounded rereads.

Recheck durable authority and the original PDF at every SDK invocation. The
wrapper exposes only the normal receipt parser; no production writer/Medical
capability. Keys stay inside the caller's existing Gemini client.
"""
import base64
from hashlib import sha256
from types import SimpleNamespace
from urllib.parse import urlsplit

from .drive_run_state import StateError
from .gemini_ai import GeminiAI
from .models import ReceiptResult
from .pdf_unit_payload import _normal


class PdfUnitAnalyzer:
    def __init__(self,ai=None,*,gemini_factory=None):
        if (ai is not None and not isinstance(ai,GeminiAI) or
                ai is None and not callable(gemini_factory)):
            raise StateError('pdf_receipt_adapter_required')
        self.ai,self.factory=ai,gemini_factory

    def analyze(self,payload,payload_sha256,categories,spec,*,verify_current):
        _normal(payload)
        if sha256(payload).hexdigest()!=payload_sha256:
            raise StateError('pdf_receipt_payload_changed')
        if set(spec['automatic_classifications']+spec['human_classifications'])!={'normal'}:
            raise StateError('pdf_receipt_normal_authority_required')
        if verify_current(spec) is not True:
            raise StateError('pdf_receipt_source_or_authority_changed')
        if self.ai is None:
            ai=self.factory()
            if not isinstance(ai,GeminiAI):raise StateError('pdf_receipt_adapter_required')
            self.ai=ai
        actual=urlsplit(self.ai.client._api_client._http_options.base_url)
        if (actual.scheme!='https' or actual.hostname!='generativelanguage.googleapis.com'
                or actual.port not in (None,443) or actual.username or actual.password):
            raise StateError('pdf_receipt_destination_mismatch')
        calls=0;headers=[]
        def create(**kwargs):
            nonlocal calls
            inputs=kwargs.get('input');media=[p for p in inputs if p.get('type')!='text'] if isinstance(inputs,list) else []
            if (set(kwargs)!= {'model','input','response_format'} or calls>=3
                    or not isinstance(inputs,list) or len(inputs)!=2
                    or set(inputs[0])!={'type','text'} or inputs[0]['type']!='text'
                    or not isinstance(inputs[0]['text'],str) or len(inputs[0]['text'])>128_000
                    or len(media)!=1 or media[0].get('type')!='image'
                    or media[0].get('mime_type')!='image/png' or kwargs.get('model')!=self.ai.model):
                raise StateError('pdf_receipt_payload_changed')
            try:sent=base64.b64decode(media[0]['data'],validate=True)
            except Exception:raise StateError('pdf_receipt_payload_changed') from None
            if sent!=payload or sha256(sent).hexdigest()!=payload_sha256:
                raise StateError('pdf_receipt_payload_changed')
            if verify_current(spec) is not True:
                raise StateError('pdf_receipt_source_or_authority_changed')
            calls+=1;response=self.ai.client.interactions.create(**kwargs)
            try:
                reading=ReceiptResult.model_validate_json(response.output_text)
                headers.append((reading.date,reading.total,reading.transaction_kind))
            except Exception:pass  # Existing parser handles invalid JSON, bounded.
            return response
        # Do not mutate a shared SDK client or change another intake's transport.
        parser=object.__new__(GeminiAI);parser.model=self.ai.model
        # Preserve the existing SDK fingerprint deny cache across per-Unit
        # parser wrappers; a fresh wrapper must never forget an earlier block.
        parser._blocked_receipts=getattr(self.ai,'_blocked_receipts',set())
        self.ai._blocked_receipts=parser._blocked_receipts
        parser.client=SimpleNamespace(interactions=SimpleNamespace(create=create))
        result=parser.analyze_receipt(payload,'image/png',categories,known_source_classification='normal')
        if len(set(headers))>1:raise StateError('pdf_receipt_unstable_reread')
        if verify_current(spec) is not True:
            raise StateError('pdf_receipt_source_or_authority_changed')
        return result
