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
from pydantic import BaseModel,ConfigDict,Field,model_validator
from typing import Annotated
from .models import ReceiptResult

Coordinate=Annotated[int,Field(strict=True,ge=0,le=1000)]

class WireLocatedReceipt(BaseModel):
    """Gemini's documented page-relative [ymin,xmin,ymax,xmax] geometry."""
    model_config=ConfigDict(extra='forbid',hide_input_in_errors=True)
    bbox:list[Coordinate]=Field(min_length=4,max_length=4)
    receipt:ReceiptResult
    item_boxes:list[list[Coordinate]]=Field(default_factory=list,max_length=300)
    @model_validator(mode='after')
    def strict_geometry(self):
        for box in [self.bbox,*self.item_boxes]:
            if (len(box)!=4 or any(type(x) is not int or not 0<=x<=1000 for x in box)
                    or box[0]>=box[2] or box[1]>=box[3]):raise ValueError('wire_bbox_invalid')
        return self

class WirePageExtraction(BaseModel):
    model_config=ConfigDict(extra='forbid',hide_input_in_errors=True)
    receipts:list[WireLocatedReceipt]=Field(min_length=1,max_length=20)
    separation_complete:bool=Field(strict=True)
    mixed_page_kind_suspected:bool=Field(strict=True)
    cross_page_continuation_suspected:bool=Field(strict=True)

def parse_wire_response(raw):
    value=WirePageExtraction.model_validate_json(raw).model_dump()
    def box(b):return dict(left=b[1]/1000,top=b[0]/1000,right=b[3]/1000,bottom=b[2]/1000)
    for located in value['receipts']:
        located['bbox']=box(located['bbox'])
        located['item_boxes']=[box(b) for b in located['item_boxes']]
    return PageReceiptExtraction.model_validate(value)

def wire_value(reading):
    """Canonical inverse for synthetic adapter tests; never a live fallback."""
    value=reading.model_dump()
    def box(b):return [round(b[k]*1000) for k in ('top','left','bottom','right')]
    for located in value['receipts']:
        located['bbox']=box(located['bbox'])
        located['item_boxes']=[box(b) for b in located['item_boxes']]
    return value

_CLEAR_PERSONAL=re.compile(r'マイナンバー|個人番号|患者氏名|生年月日|被保険者番号|保険証番号|口座番号')

def response_wire_schema():
    """Small generation grammar; all actual bounds remain local validation.

    Inline local references and omit large array repetition constraints from
    the API grammar. This is not a runtime retry/fallback or permission change.
    Local PageReceiptExtraction validation still rejects oversized results.
    """
    source=WirePageExtraction.model_json_schema()
    definitions=source.get('$defs',{})
    def expand(value):
        if isinstance(value,list):return [expand(x) for x in value]
        if not isinstance(value,dict):return value
        if '$ref' in value:
            target=value['$ref']
            if not target.startswith('#/$defs/') or target[8:] not in definitions:
                raise StateError('page_response_schema_invalid')
            return expand(definitions[target[8:]])
        return {key:expand(item) for key,item in value.items()
                if key not in {'$defs','title','default','maxItems','minItems'}}
    return expand(source)

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
              'human_page_kind','review_identity','authority_revision','clearly_sensitive',
              'observation_complete','extraction_status')
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
        self.responses=0
        self.last_diagnostic={}
    def analyze(self,page,payload,categories,*,expected_payload_sha256,render_proof):
        self.last_diagnostic={}
        fingerprint=sha256(payload).hexdigest()
        if fingerprint!=expected_payload_sha256:raise StateError('page_payload_changed')
        expected=FreshRenderProof(page.source.source_file_id,page.source.source_content_hash,page.source.page_count,
            page.page_number,page.stable_page_identity,fingerprint)
        if type(render_proof) is not FreshRenderProof or render_proof!=expected:
            raise StateError('page_render_binding_changed')
        prompt='''日本の一般レシート解析。ページに独立したレシートが複数ある場合はreceipts配列で別取引に分離。
1枚の場合も配列長1。枚数をユーザーへ質問しない。店舗・日付・明細・totalを別レシートと混ぜず、合算禁止。
各bboxと各item_boxesは画像全体の[ymin,xmin,ymax,xmax]整数配列、0..1000に正規化。明細ごとの印字領域を示す。
座標はレシート内の相対座標ではなく、すべてページ画像全体の同じ座標系。
bboxはヘッダ・全明細・合計を含むレシート全体の外接領域。すべてのitem_boxesをbboxの中へ含める。
item_boxesは各明細に一対一で対応し、itemsと同じ順序。位置を確認できなければ分離不完全とする。
上から下、同じ高さは左から右。ページ間の続きと思われる場合はcross_page_continuation_suspected=true。
医療/給与等が混在する疑いならmixed_page_kind_suspected=true。分離が不明ならseparation_complete=false。
支払日YYYY-MM-DD、原本の正のtotal、明細とtotal整合、既存カテゴリだけを使用。新カテゴリ禁止。
商品/数量/税/値引きを原本から読み、架空の調整額禁止。transaction_kindはpurchase/buyback/unknown。
商品ごとに税込の明細金額を抽出。数量が読めれば数量も。数量×単価と明細金額を区別する。
特定商品に対応する値引きはその商品のamountへ反映。全体値引き・クーポンは印字金額を負の明細とし、二重値引き禁止。
税抜商品が並ぶ場合は、印字された外税を独立明細にしてよい。税率別外税をすべて読む。内税・小計・税対象額を加算しない。
全商品・値引き・外税を上から下まで読む。差額を埋める架空の調整額は禁止。totalを明細合計で置き換えない。
同じ取引のカード控え・領収証再掲・小計は別レシートにしない。独立した別取引だけを分離する。
支払方法と店舗は読めた場合のみ。推測・過去履歴・ファイル名で補完禁止。読めなければ空欄。
上限は1ページ20枚・各レシート300明細。これを超える/不足する/不確実な分離はseparation_complete=false。
カテゴリ一覧:
'''+ '\n'.join(a+' / '+b for a,b in categories)
        readings=[];proof=None
        for _ in range(2):
            if sha256(payload).hexdigest()!=fingerprint:raise StateError('page_payload_changed')
            proof=self.permission(page,payload) # durable authority/source/exact OCR on each send
            if proof.get('payload_sha256')!=fingerprint:raise StateError('page_payload_changed')
            kwargs={'model':self.model,'input':[{'type':'text','text':prompt},
                {'type':'image','mime_type':'image/png','data':base64.b64encode(payload).decode('ascii')}],
                'response_format':{'type':'text','mime_type':'application/json','schema':response_wire_schema()}}
            # SDK arguments, not a cached gate: no other media/bytes can pass.
            sent=base64.b64decode(kwargs['input'][1]['data'],validate=True)
            if sha256(sent).hexdigest()!=fingerprint:raise StateError('page_payload_changed')
            self.calls+=1
            stage='api'
            try:
                response=self.client.interactions.create(**kwargs)
                self.responses+=1;stage='response_schema'
                if type(response.output_text) is not str or len(response.output_text.encode('utf-8'))>1024*1024:
                    raise StateError('page_response_resource_limit')
                readings.append(parse_wire_response(response.output_text))
            except Exception as error:
                from .gemini_errors import gemini_api_status,is_gemini_api_error
                from pydantic import ValidationError
                self.last_diagnostic={'failure_stage':stage}
                code=gemini_api_status(error)
                if code is not None:self.last_diagnostic['http_status']=code
                if is_gemini_api_error(error):
                    self.last_diagnostic['failure_kind']='gemini_api'
                    # Fixed vocabulary only; never persist the API body/message.
                    import json
                    body=getattr(error,'body',None)
                    message=(str(error)+' '+json.dumps(body,ensure_ascii=False,default=lambda _:'' )).lower()
                    self.last_diagnostic['schema_keywords']=[key for key in
                        ('schema','additionalProperties','$defs','$ref','maxItems','minimum','maximum') if key.lower() in message]
                    # Some SDK families expose a generic message and keep the
                    # actual reason in body. Classify in memory, emit only
                    # fixed labels: never an arbitrary field, value or message.
                    self.last_diagnostic['api_reason_labels']=[label for label,terms in (
                        ('schema_complexity',('complex','nesting','depth')),
                        ('unsupported_feature',('not support','unsupported')),
                        ('background',('background',)),('tool_required',('requires the use','computer use')),
                        ('model',('model',)),('response_format',('response_format','response format')),
                        ('input_shape',('input','invalid json','unknown field','unknown name')),
                        ('media',('image','mime','base64')),
                        ('resource_size',('too large','size limit','token limit')),
                        ('authentication',('api key','api_key','permission','credential')),
                        ('parameter',('parameter','argument','field')),
                        ('required',('required','missing')),
                        ('invalid_request',('invalid_request','invalid_argument')),
                    ) if any(term in message for term in terms)]
                elif isinstance(error,ValidationError):
                    fields={'receipts','receipt','bbox','item_boxes','left','top','right','bottom','date',
                        'total','merchant','payment_method','items','name','quantity','amount','major_category',
                        'minor_category','note','confidence','transaction_kind','separation_complete',
                        'mixed_page_kind_suspected','cross_page_continuation_suspected'}
                    self.last_diagnostic.update(failure_kind='response_schema',schema_issues=[{
                        'type':x['type'],'path':[part if type(part) is int and 0<=part<=300 or
                            isinstance(part,str) and part in fields else 'field' for part in x['loc']]}
                        for x in error.errors(include_input=False,include_context=False,include_url=False)[:8]])
                else:self.last_diagnostic['failure_kind']='adapter_contract' if isinstance(error,(TypeError,AttributeError)) else 'response_processing'
                raise
        return readings,proof
