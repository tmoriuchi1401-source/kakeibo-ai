"""Exact single-page PNG transport. No writer, Medical parser, move or fallback."""
import base64,re
from hashlib import sha256
from io import BytesIO
from PIL import Image
from .drive_run_state import StateError
from .page_receipt_model import PageUnit,PageReceiptExtraction,stable_page
from dataclasses import dataclass
from .human_general_authority import eligible,validate_grant
from .receipt_text_extraction import _extract_receipt_text
from .medical_receipt_privacy import classify_receipt_text
from .receipt_privacy_gate import evaluate_receipt_privacy,require_receipt_ai_permission,ReceiptPrivacyBlocked
from .receipt_pdf_units import MAX_PAGE_PIXELS,is_pdf

_CLEAR_PERSONAL=re.compile(r'マイナンバー|個人番号|患者氏名|生年月日|被保険者番号|保険証番号|口座番号')

@dataclass(frozen=True)
class FreshRenderProof:
    source_file_id:str
    source_content_hash:str
    page_count:int
    page_number:int
    stable_page_identity:str
    payload_sha256:str

def fresh_render_proof(page,source_bytes,rendered_number,rendered_count,payload):
    """Called by the trusted renderer with its actual selection, never the UI.

    This ephemeral record is not durable authority. It prevents a fresh payload
    from being accidentally associated with another page's permission context.
    """
    if (sha256(source_bytes).hexdigest()!=page.source.source_content_hash
            or rendered_number!=page.page_number or rendered_count!=page.source.page_count):
        raise StateError('page_render_binding_changed')
    return FreshRenderProof(page.source.source_file_id,page.source.source_content_hash,rendered_count,
        rendered_number,stable_page(page.source,rendered_number),sha256(payload).hexdigest())

def authorize_payload(page,payload,*,current_page,load_source,load_grant):
    """No caller allow flag. Reload protected authority/source immediately.

    Automatic classification never changes. New explicit authority is the
    sole additional basis for unknown; normal uses the unchanged old gate.
    """
    if (page.automatic_classification in {'medical','payroll'} or page.clearly_sensitive
            or (page.automatic_classification=='normal' and page.human_page_kind=='unknown')
            or page.human_page_kind in {'medical','payroll'}):raise ReceiptPrivacyBlocked()
    if type(payload) is not bytes or is_pdf(payload,'image/png'):raise ReceiptPrivacyBlocked()
    try:
        with Image.open(BytesIO(payload)) as image:
            if (image.format!='PNG' or image.mode!='RGB' or image.info or getattr(image,'n_frames',1)!=1
                    or image.width*image.height>MAX_PAGE_PIXELS):raise ReceiptPrivacyBlocked()
        latest=current_page(page.source.source_file_id,page.page_number)
        # Render fingerprint is diagnostics, never source/page authority.
        keys=('source','page_number','stable_page_identity','automatic_classification','automatic_reason',
              'human_page_kind','review_identity','authority_revision','clearly_sensitive')
        if any(getattr(latest,k)!=getattr(page,k) for k in keys):raise StateError('page_authority_stale')
        raw=load_source(page.source.source_file_id)
        if type(raw) is not bytes or sha256(raw).hexdigest()!=page.source.source_content_hash:
            raise StateError('page_source_changed')
        extraction=_extract_receipt_text(payload,'image/png')
        if extraction.status!='extracted' or not extraction.observation_complete:raise ReceiptPrivacyBlocked()
        text='\n'.join([extraction.text or '', ' '.join(t.text for t in extraction.structured_tokens)])
        decision=classify_receipt_text(text)
        if (decision.classification in {'medical','payroll'} or decision.reason_code=='conflicting_sensitive_evidence'
                or _CLEAR_PERSONAL.search(text.replace(' ',''))):raise ReceiptPrivacyBlocked()
        gate=evaluate_receipt_privacy(payload,'image/png')
        if gate.classification in {'medical','payroll'} or gate.reason_code in {
                'conflicting_sensitive_evidence','known_sensitive_source','empty_text','ocr_or_text_extraction_failed'}:
            raise ReceiptPrivacyBlocked()
        if page.automatic_classification=='normal':
            require_receipt_ai_permission(payload,'image/png')
            return {'basis':'automatic_normal','automatic_classification':'normal',
                    'exact_classification':gate.classification,'payload_sha256':sha256(payload).hexdigest()}
        if not eligible(page):raise ReceiptPrivacyBlocked()
        validate_grant(load_grant(page),page)
        if gate.classification not in {'normal','sensitive_unknown'}:raise ReceiptPrivacyBlocked()
        if gate.classification=='sensitive_unknown' and gate.reason_code not in {'insufficient_evidence','sensitive_signal_insufficient'}:
            raise ReceiptPrivacyBlocked()
        return {'basis':'human_general_receipt','automatic_classification':page.automatic_classification,
                'exact_classification':gate.classification,'payload_sha256':sha256(payload).hexdigest()}
    except (ReceiptPrivacyBlocked,StateError):raise
    except Exception:raise ReceiptPrivacyBlocked() from None

class GeminiPageReceipts:
    """Two independent bounded readings; only one exact fresh PNG is sent."""
    def __init__(self,client,model,permission):
        self.client,self.model,self.permission=client,model,permission
        self.calls=0
    def analyze(self,page,payload,categories,*,expected_payload_sha256,render_proof):
        fingerprint=sha256(payload).hexdigest()
        if fingerprint!=expected_payload_sha256:raise StateError('page_payload_changed')
        expected=FreshRenderProof(page.source.source_file_id,page.source.source_content_hash,page.source.page_count,
            page.page_number,page.stable_page_identity,fingerprint)
        if type(render_proof) is not FreshRenderProof or render_proof!=expected:
            raise StateError('page_render_binding_changed')
        prompt='''日本の一般レシート解析。ページに独立したレシートが複数ある場合はreceipts配列で別取引に分離。
1枚の場合も配列長1。枚数をユーザーへ質問しない。店舗・日付・明細・totalを別レシートと混ぜず、合算禁止。
各bboxと各item_boxesは画像全体で正規化したleft/top/right/bottom座標(0..1)。明細ごとの印字領域を示す。
上から下、同じ高さは左から右。ページ間の続きと思われる場合はcross_page_continuation_suspected=true。
医療/給与等が混在する疑いならmixed_page_kind_suspected=true。分離が不明ならseparation_complete=false。
支払日YYYY-MM-DD、原本の正のtotal、明細とtotal整合、既存カテゴリだけを使用。新カテゴリ禁止。
商品/数量/税/値引きを原本から読み、架空の調整額禁止。transaction_kindはpurchase/buyback/unknown。
支払方法と店舗は読めた場合のみ。推測・過去履歴・ファイル名で補完禁止。読めなければ空欄。
カテゴリ一覧:
'''+ '\n'.join(a+' / '+b for a,b in categories)
        readings=[];proof=None
        for _ in range(2):
            if sha256(payload).hexdigest()!=fingerprint:raise StateError('page_payload_changed')
            proof=self.permission(page,payload) # durable authority/source/exact OCR on each send
            if proof.get('payload_sha256')!=fingerprint:raise StateError('page_payload_changed')
            kwargs={'model':self.model,'input':[{'type':'text','text':prompt},
                {'type':'image','mime_type':'image/png','data':base64.b64encode(payload).decode('ascii')}],
                'response_format':{'type':'text','mime_type':'application/json','schema':PageReceiptExtraction.model_json_schema()}}
            # SDK arguments, not a cached gate: no other media/bytes can pass.
            sent=base64.b64decode(kwargs['input'][1]['data'],validate=True)
            if sha256(sent).hexdigest()!=fingerprint:raise StateError('page_payload_changed')
            self.calls+=1
            response=self.client.interactions.create(**kwargs)
            if type(response.output_text) is not str or len(response.output_text.encode('utf-8'))>1024*1024:
                raise StateError('page_response_resource_limit')
            readings.append(PageReceiptExtraction.model_validate_json(response.output_text))
        return readings,proof
