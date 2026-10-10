"""One general form, trusted prefill and durable validation-only intents.

No writer/AI/Medical/mover imports or live provisioning. Posting still belongs
to established manual/receipt writers and their read-back journals. These
plans are explicitly NOT accounting authority and cannot be auto-dispatched.
"""
from copy import deepcopy
from hashlib import sha256
import math
import re

from .drive_run_state import StateError
from .receipt_plan.models import ReceiptResult
from .pdf_receipt_policy import apply_receipt_policy, validate_receipt_result
from .receipt_reimport import _date
from .pdf_page_general import input_rows, manual_values, CONFIRM_ACTION
from .pdf_review_fields import SCHEMA as UI_SCHEMA
from .page_receipt_model import digest, page_key, ordered, segmentation_issues, Box, iou, LocatedReceipt
from .receipt_plan.manifest import validate as validate_manifest, SCHEMA as MANIFEST_SCHEMA
from .receipt_plan.authority import binding_fields, eligible, validate_grant

SCHEMA = 'general-receipt-completion-v1'
FIELDS = ('date','amount','category','merchant','payment','memo')
FLAGS = {'accounting_allowed':False,'medical_handoff_allowed':False,'archive_allowed':False}
UUID = re.compile(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}')


def unit_identity(page, manifest, unit_id):
    # Check the established frozen separation, not the model's array index.
    validate_manifest({'schema':MANIFEST_SCHEMA,'binding':'0'*64,'generation':1,
        'pages':{page_key(page):manifest},'accounting_allowed':False},'0'*64)
    units=[u for u in manifest['units'] if u['receipt_unit_id']==unit_id]
    if len(units)!=1:raise StateError('completion_receipt_identity_stale')
    return {**binding_fields(page),'processing_status':page.processing_status,
        'receipt_unit_id':unit_id,'segmentation_digest':manifest['segmentation_digest'],
        'receipt_index':units[0]['receipt_index'],'bbox':units[0]['bbox']}


def _category(result):
    pairs={(i.major_category,i.minor_category) for i in result.items}
    return '｜'.join(next(iter(pairs))) if len(pairs)==1 else ''


def _values(result):
    return dict(date=result.date,amount=result.total,category=_category(result),
        merchant=result.merchant,payment=result.payment_method,memo=result.note)


def draft(page,manifest,unit_id,readings=(),*,categories,text='',gate=None,confidence=None,ambiguous=()):
    """Corroborated field validation, no guesses and no whole OCR persistence.

    Two independent schema-valid readings are required for any machine prefill.
    Explicit low/unknown confidence or ambiguity vetoes that field. Existing
    item confidence also governs category prefill. No fabricated confidence.
    """
    if (page.automatic_classification in {'medical','payroll'} or page.clearly_sensitive
            or page.human_page_kind in {'medical','payroll'}
            or (page.automatic_classification=='normal' and page.human_page_kind=='unknown')
            or not page.observation_complete or page.extraction_status!='extracted'):
        raise StateError('completion_general_only')
    identity=unit_identity(page,manifest,unit_id)
    values={f:'' for f in FIELDS}; reasons={f:['not_obtained'] for f in FIELDS}
    trusted=[];hard=[]
    try:trusted=[ReceiptResult.model_validate(r.model_dump() if isinstance(r,ReceiptResult) else r) for r in readings]
    except Exception:trusted=[]
    last=trusted[-1] if trusted else None
    issues=[]
    if len(trusted)>=2:
        # The final reading is retained; field disagreement blanks only that field.
        views=[]
        for result in trusted:
            policy,checks=apply_receipt_policy(result,categories,text=text,gate=gate)
            issues.extend(policy);views.append(_values(result))
        for field in FIELDS:
            same=all(v[field]==views[-1][field] for v in views)
            value=views[-1][field]
            score=(confidence or {}).get(field)
            confident=(field not in (confidence or {}) or
                (type(score) in (int,float) and math.isfinite(score) and .8<=score<=1))
            ok=same and confident and field not in ambiguous and value not in ('',None)
            why=[]
            if not same:why.append('reread_disagreement')
            if not confident:why.append('confidence_insufficient')
            if field in ambiguous:why.append('ambiguous')
            if field=='date':ok=ok and _date(value)==value
            elif field=='amount':
                ok=ok and type(value) is int and 1<=value<=99999999 and all(sum(i.amount for i in r.items)==r.total for r in trusted)
            elif field=='category':
                ok=ok and tuple(str(value).split('｜')) in set(categories) and all(i.confidence>=.8 for r in trusted for i in r.items)
            elif field in {'merchant','payment','memo'}:
                ok=ok and isinstance(value,str) and len(value)<={'merchant':100,'payment':50,'memo':300}[field]
            if ok:values[field]=value;reasons[field]=[]
            else:reasons[field]=why or ['field_validation_failed']
        # A total correction cannot approve unstable, mixed or fabricated items.
        item_shape=lambda r:[(i.name,i.quantity,i.amount) for i in r.items]
        if len({digest(item_shape(r)) for r in trusted})>1:hard.append('reread_item_structure_changed')
        if len({r.transaction_kind for r in trusted})>1:hard.append('reread_transaction_kind_changed')
        if any('原本で確認できない調整明細' in i for i in issues):hard.append('unverified_adjustment')
        if any('明細不正' in i for i in issues):hard.append('invalid_item_structure')
        if any('原本の買取表示と取引種別が不一致' in i for i in issues):hard.append('transaction_kind_evidence_conflict')
        if any('原本の明示的な支払方法と解析結果が矛盾' in i for i in issues):
            values['payment']='';reasons['payment']=['explicit_payment_contradiction']
    elif last is not None:
        # Retain actual items but do not offer uncorroborated guesses as values.
        issues,checks=apply_receipt_policy(last,categories,text=text)
        hard.append('independent_reread_required')
    record={'schema':SCHEMA,'identity':identity,'prefill':values,'blank_reasons':reasons,
        'parsed':last.model_dump() if last else None,'hard_issues':sorted(set(hard)),
        'provenance':{f:'gemini' if values[f]!='' else 'missing' for f in FIELDS},
        'mode':'itemized' if last is not None else 'manual',**FLAGS}
    # Item categories are corroborated independently. A mixed-category receipt
    # is valid; the old receipt-level category must not erase its item choices.
    record['item_category_evidence']=[]
    if last is not None:
        for index,item in enumerate(last.items):
            pair=(item.major_category,item.minor_category)
            stable=(len(trusted)>=2 and not hard and
                all(len(r.items)==len(last.items) and
                    (r.items[index].name,r.items[index].quantity,r.items[index].amount)==
                    (item.name,item.quantity,item.amount) and
                    (r.items[index].major_category,r.items[index].minor_category)==pair and
                    r.items[index].confidence>=.8 for r in trusted))
            record['item_category_evidence'].append({
                'item_index':index+1,'category':'｜'.join(pair) if stable and pair in set(categories) else '',
                'corroborated':bool(stable and pair in set(categories))})
    record['candidate_digest']=digest(record)
    return record


def drafts_for_page(page,manifest,first,second,*,categories,unit_texts=None,unit_gates=None):
    """Bind corroborated spatial results to frozen IDs, never SDK array order."""
    a,b=ordered(first),ordered(second)
    if segmentation_issues(first,second) or len(a)!=len(manifest['units']):
        raise StateError('completion_segmentation_review')
    output=[]
    for unit,one,two in zip(manifest['units'],a,b):
        unit_identity(page,manifest,unit['receipt_unit_id'])
        box=Box.model_validate(unit['bbox'])
        if min(iou(box,one.bbox),iou(box,two.bbox))<.8:
            raise StateError('completion_segmentation_review')
        index=unit['receipt_index']
        output.append(draft(page,manifest,unit['receipt_unit_id'],[one.receipt,two.receipt],
            categories=categories,text=(unit_texts or {}).get(index,''),gate=(unit_gates or {}).get(index)))
    return output


def validate_draft(record):
    copied=deepcopy(record)
    signature=copied.pop('candidate_digest',None)
    if (signature!=digest(copied) or record.get('schema')!=SCHEMA
            or set(record)-{'item_category_evidence'}!={'schema','identity','prefill','blank_reasons','parsed','hard_issues',
                              'provenance','mode','candidate_digest',*FLAGS}
            or any(record.get(k) is not False for k in FLAGS)
            or set(record.get('prefill',{}))!=set(FIELDS)
            or set(record.get('blank_reasons',{}))!=set(FIELDS)
            or set(record.get('provenance',{}))!=set(FIELDS)
            or record.get('mode')!=('manual' if record.get('parsed') is None else 'itemized')):
        raise StateError('completion_candidate_changed')
    if record['parsed'] is not None:
        LocatedReceipt(bbox=record['identity']['bbox'],receipt=record['parsed'])
    if 'item_category_evidence' in record:
        evidence=record['item_category_evidence']
        items=(record.get('parsed') or {}).get('items',[])
        if (not isinstance(evidence,list) or len(evidence)!=len(items) or
                any(set(e)!={'item_index','category','corroborated'} or e['item_index']!=i or
                    type(e['corroborated']) is not bool or not isinstance(e['category'],str) or
                    bool(e['category'])!=e['corroborated'] for i,e in enumerate(evidence,1))):
            raise StateError('completion_item_evidence_invalid')
    return record


def _normalized(field,value):
    if field=='date':return _date(value) or value
    if field=='amount':
        from .receipt_reimport import _money
        number=_money(str(value).replace(',',''))
        return int(number) if number is not None else value
    return value
