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
from .models import ReceiptResult
from .receipt_validation import apply_receipt_policy, validate_receipt_result
from .receipt_reimport import _date
from .pdf_page_general import input_rows, manual_values, CONFIRM_ACTION
from .pdf_page_review import SCHEMA as UI_SCHEMA, check_snapshot
from .page_receipt_model import digest, page_key, ordered, segmentation_issues, Box, iou, LocatedReceipt
from .page_receipt_manifest import validate as validate_manifest, SCHEMA as MANIFEST_SCHEMA
from .human_general_authority import (HumanGeneralAuthorityStore, binding_fields,
    eligible, validate_grant)

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


def evaluate(record,fields,categories):
    validate_draft(record)
    provenance={}
    for f in FIELDS:
        old=record['prefill'][f];new=fields.get(f,'')
        provenance[f]=('missing' if new in ('',None) else 'gemini' if old!='' and _normalized(f,old)==_normalized(f,new)
                       else 'human_override' if old!='' else 'human')
    try:inputs=manual_values({**fields,'amount':_normalized('amount',fields.get('amount','')),
        'manual_action':CONFIRM_ACTION},categories)
    except StateError:
        return {'status':'needs_human_completion','provenance':provenance,'issues':['required_or_input_field_invalid'],**FLAGS}
    issues=record['hard_issues'][:]
    parsed=deepcopy(record['parsed'])
    if parsed is not None:
        parsed.update(date=inputs['date'],total=inputs['amount'],merchant=inputs['merchant'],
                      payment_method=inputs['payment'],note=inputs['note'])
        # Legacy single-item forms remain compatible. Multiple items require
        # the item review form, never a silently broadcast receipt category.
        if len(parsed['items'])==1:
            parsed['items'][0].update(major_category=inputs['major'],minor_category=inputs['minor'])
        else:
            issues.append('item_category_review_required')
        result=ReceiptResult.model_validate(parsed)
        _,normal_issues=validate_receipt_result(result,categories)
        issues.extend(normal_issues)
        if result.transaction_kind=='unknown':issues.append('transaction_kind_unknown')
        parsed=result.model_dump()
    return {'status':'needs_review' if issues else 'ready_to_confirm','inputs':inputs,
        'parsed':parsed,'provenance':provenance,'issues':list(dict.fromkeys(issues)),**FLAGS}


def card(record,fields=None,*,categories):
    validate_draft(record)
    values={**record['prefill'],**(fields or {})}
    state=evaluate(record,values,categories)
    states={'needs_human_completion':'不足欄を入力してください','needs_review':'要確認・明細等の安全検証未完了',
            'ready_to_confirm':'入力確認済み・明示確定待ち'}
    parsed=record['parsed']
    constraints={'hard_blocked':bool(record['hard_issues']) or bool(parsed is not None and
        (not parsed['items'] or parsed['transaction_kind']=='unknown' or
         any(not i['name'].strip() or (i['quantity'] is not None and i['quantity']<=0) for i in parsed['items']))),
        'item_sum':sum(i['amount'] for i in parsed['items']) if parsed is not None else None}
    ident={**record['identity'],'schema':UI_SCHEMA,'kind':'general_receipt_completion',
        'completion_constraints':constraints,
        'page_numbers':[record['identity']['page_number']],'candidate_digest':record['candidate_digest']}
    n=ident['page_number'];i=ident['receipt_index']
    rows=[('target','対象',f'p{n} レシート {i}'),('kind','種別','一般レシート'),
        ('state','状態',states[state['status']]),('original','原本',f'ページ全体を見る（p{n}）'),
        ('notice','入力方法','原本を確認し、不足欄を入力。自動入力値も修正できます。確定だけではAI再解析しません。'),
        *input_rows(values)]
    # UI values remain editable; baseline/candidate and receipt identity do not.
    token=digest([ident,[(f,l,v) for f,l,v in rows if f not in FIELDS and f not in {'state','result','manual_action'}]])
    return {'identity':ident,'token':token,'rows':rows}


def empty_state(binding):
    return {'schema':SCHEMA,'binding':binding,'drafts':{},'requests':{},'generation':0,**FLAGS}


def validate_state(value,binding):
    try:
        if (set(value)!={'schema','binding','drafts','requests','generation',*FLAGS}
                or value['schema']!=SCHEMA or value['binding']!=binding
                or type(value['generation']) is not int or value['generation']<0
                or any(value[k] is not False for k in FLAGS)):raise ValueError()
        for key,record in value['drafts'].items():
            validate_draft(record)
            if key!=record['identity']['receipt_unit_id']:raise ValueError()
        targets=set()
        if value['generation']!=len(value['drafts'])+len(value['requests']):raise ValueError()
        for rid,request in value['requests'].items():
            if (not UUID.fullmatch(rid) or request['request_id']!=rid or request['status']!='validated_not_written'
                    or request['snapshot_digest']!=digest(request['snapshot'])
                    or request['plan_digest']!=digest(request['plan'])
                    or any(request['plan'].get(k) is not False for k in FLAGS)
                    or request['plan'].get('posting_authority') is not False
                    or request['receipt_unit_id'] not in value['drafts']
                    or request['plan']['candidate_digest']!=value['drafts'][request['receipt_unit_id']]['candidate_digest']
                    or request['plan']['identity']!=value['drafts'][request['receipt_unit_id']]['identity']
                    or request['plan']['request_id']!=rid or request['plan']['status']!='ready_to_confirm'
                    or request['receipt_unit_id'] in targets):raise ValueError()
            targets.add(request['receipt_unit_id'])
    except Exception:raise StateError('completion_state_invalid') from None
    return value


class CompletionStore(HumanGeneralAuthorityStore):
    def __init__(self,transport,binding,*,preflight):
        super().__init__(transport,binding,preflight=preflight,validator=validate_state)


class GeneralCompletion:
    """Fresh validation-only boundary; never exposes a writer or posting flag.

    current_manifest/load_grant and read_owner_snapshot must be trusted backend
    adapters, never identities reconstructed from hidden Sheets cells.
    """
    def __init__(self,store,current_page,load_source,current_manifest,load_grant,
                 categories,read_owner_snapshot,duplicate_check):
        self.store,self.current_page,self.load_source=store,current_page,load_source
        self.manifest,self.grant=current_manifest,load_grant
        self.categories,self.owner,self.duplicates=categories,read_owner_snapshot,duplicate_check

    def fresh(self,record):
        ident=record['identity']
        page=self.current_page(ident['source_file_id'],ident['page_number'])
        if unit_identity(page,self.manifest(page),ident['receipt_unit_id'])!=ident:
            raise StateError('completion_source_page_review_or_manifest_stale')
        if (page.automatic_classification in {'medical','payroll'} or page.clearly_sensitive
                or page.human_page_kind in {'medical','payroll'}
                or (page.automatic_classification=='normal' and page.human_page_kind=='unknown')
                or not page.observation_complete or page.extraction_status!='extracted'):
            raise StateError('completion_sensitive_page')
        raw=self.load_source(ident['source_file_id'])
        if type(raw) is not bytes or sha256(raw).hexdigest()!=ident['source_content_hash']:
            raise StateError('completion_source_changed')
        if page.automatic_classification!='normal':
            if not eligible(page):raise StateError('completion_sensitive_page')
            validate_grant(self.grant(page),page)
        return page

    def remember(self,record):
        validate_draft(record);self.fresh(record)
        state=self.store.load();key=record['identity']['receipt_unit_id']
        if key in state['drafts']:
            if state['drafts'][key]!=record:raise StateError('completion_candidate_changed')
            return deepcopy(record)
        state['drafts'][key]=deepcopy(record);state['generation']+=1
        self.store.save(state)
        return self.store.load()['drafts'][key]

    def projected(self,read_current=None):
        """Caller merges these with ALL other cards in the existing shared sheet.

        Do not replace other pending/Medical cards with this collection. A
        stale protected draft fails closed instead of rendering old values.
        """
        output=[]
        for record in self.store.load()['drafts'].values():
            self.fresh(record)
            expected=card(record,categories=self.categories())
            fields=check_snapshot(read_current(expected['token']),expected) if read_current else None
            output.append(card(record,fields,categories=self.categories()))
        return sorted(output,key=lambda c:(c['identity']['source_file_id'],
            c['identity']['page_number'],c['identity']['receipt_index']))

    def confirm(self,request_id,unit_id,snapshot):
        if not isinstance(request_id,str) or not UUID.fullmatch(request_id):
            raise StateError('completion_request_invalid')
        state=self.store.load();record=state['drafts'].get(unit_id)
        if not record:raise StateError('completion_candidate_missing')
        self.fresh(record)
        expected=card(record,categories=self.categories())
        fields=check_snapshot(snapshot,expected)
        if fields.get('manual_action')!=CONFIRM_ACTION:raise StateError('completion_explicit_confirm_required')
        if self.owner(request_id)!=snapshot:raise StateError('completion_owner_snapshot_changed')
        prior=state['requests'].get(request_id)
        if prior:
            if prior['receipt_unit_id']!=unit_id or prior['snapshot_digest']!=digest(snapshot):
                raise StateError('completion_request_replaced')
            return {**deepcopy(prior),'replayed':True}
        if any(r['receipt_unit_id']==unit_id for r in state['requests'].values()):
            raise StateError('completion_receipt_already_confirmed')
        plan=evaluate(record,fields,self.categories())
        if plan['status']!='ready_to_confirm':raise StateError('completion_validation_required')
        if self.duplicates(record['identity'],plan) is not False:
            raise StateError('completion_duplicate_or_unknown')
        plan.update(identity=deepcopy(record['identity']),candidate_digest=record['candidate_digest'],
            request_id=request_id,posting_authority=False,
            existing_writer_route='receipt_itemized' if record['mode']=='itemized' else 'general_manual')
        request={'request_id':request_id,'receipt_unit_id':unit_id,'snapshot':deepcopy(snapshot),
            'snapshot_digest':digest(snapshot),'status':'validated_not_written','plan':plan,'plan_digest':digest(plan)}
        # Recheck owner and protected state across validation/duplicate reads.
        self.fresh(record)
        if self.owner(request_id)!=snapshot:raise StateError('completion_owner_snapshot_changed')
        state['requests'][request_id]=request;state['generation']+=1
        self.store.save(state)
        saved=self.store.load()['requests'][request_id]
        if saved!=request:raise StateError('completion_readback_mismatch')
        return {**saved,'replayed':False}
