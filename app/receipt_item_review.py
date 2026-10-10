"""Item-level review in the existing PDF sheet; cells are intent, not authority.

The caller supplies existing trusted freshness/actor/duplicate and writer
adapters. Execution is disabled by default; UI projection never enables it.
The limited live runner validates a Google-authenticated snapshot, PLAN ONLY.
"""
from copy import deepcopy
import re
import time
from datetime import datetime,timezone

from .drive_run_state import StateError
from .general_receipt_completion import validate_draft, _normalized, FLAGS, UUID
from .models import ReceiptResult
from .page_receipt_model import digest
from .pdf_review_fields import original_uri
from .receipt_validation import validate_receipt_result
from .human_general_authority import HumanGeneralAuthorityStore
from .human_general_auth_transport import VerifiedActor, ISSUER

SCHEMA='general-receipt-item-review-v1'
ACTIONS=('未選択','記帳する','保留する')
FIELDS=('date','amount','merchant','payment','memo')
TERMINAL={'imported','manual_imported','medical_manual_imported',
          'reconciled_existing','duplicate_confirmed','intentionally_skipped'}


def prepare(legacy,categories,*,adjustment_targets=None,legacy_category=''):
    """Compatibility conversion is not new HGA or posting permission.

    Old mixed-category drafts lack independent item-category evidence: blank
    those cells rather than infer it from the last reading. A legacy receipt
    category is never broadcast across its items.
    """
    validate_draft(legacy)
    if legacy['parsed'] is None:raise StateError('item_review_actual_items_required')
    record={'schema':SCHEMA,'legacy':deepcopy(legacy),'items':[],
            'adjustment_targets':deepcopy(adjustment_targets or {}),'legacy_category':legacy_category,**FLAGS}
    evidence=legacy.get('item_category_evidence',[])
    for index,item in enumerate(legacy['parsed']['items'],1):
        category='';pair=(item['major_category'],item['minor_category'])
        if not legacy['hard_issues'] and pair in set(categories):
            if index<=len(evidence) and evidence[index-1]['corroborated']:
                category=evidence[index-1]['category']
        kind=('discount' if item['amount']<0 else 'tax' if
              re.fullmatch(r'.*(外税|消費税|税額).*',item['name']) else 'product')
        identity=digest(['receipt-item-v1',legacy['identity']['receipt_unit_id'],
                         legacy['candidate_digest'],index,item['name'],item['quantity'],item['amount']])
        record['items'].append({'item_id':identity,'item_index':index,'kind':kind,
            'name':item['name'],'amount':item['amount'],'category':category})
    if not record['items']:raise StateError('item_review_actual_items_required')
    record['digest']=digest(record)
    return validate(record)


def validate(record):
    try:
        copied=deepcopy(record);signature=copied.pop('digest')
        required={'schema','legacy','items','adjustment_targets','legacy_category','digest',*FLAGS}
        if (not required<=set(record) or set(record)-required-{'rule_evidence','review_evidence'}
                or signature!=digest(copied) or record['schema']!=SCHEMA
                or any(record[k] is not False for k in FLAGS)
                or not isinstance(record['legacy_category'],str) or len(record['legacy_category'])>100):raise ValueError()
        validate_draft(record['legacy'])
        parsed=record['legacy']['parsed'];items=record['items']
        if not parsed or len(items)!=len(parsed['items']) or not 1<=len(items)<=100:raise ValueError()
        for index,(item,raw) in enumerate(zip(items,parsed['items']),1):
            if (set(item)!={'item_id','item_index','kind','name','amount','category'}
                    or item['item_index']!=index or item['name']!=raw['name'] or item['amount']!=raw['amount']
                    or item['item_id']!=digest(['receipt-item-v1',record['legacy']['identity']['receipt_unit_id'],
                        record['legacy']['candidate_digest'],index,raw['name'],raw['quantity'],raw['amount']])
                    or item['kind'] not in {'product','tax','discount'} or not isinstance(item['category'],str)):
                raise ValueError()
        if len({x['item_id'] for x in items})!=len(items):raise ValueError()
        if 'review_evidence' in record:
            evidence=record['review_evidence']
            if (set(evidence)!={'schema','items','full_structure_confirmation_required'} or evidence['schema']!=1
                or type(evidence['full_structure_confirmation_required']) is not bool
                or len(evidence['items'])!=len(items)):raise ValueError()
            for item,e in zip(items,evidence['items']):
                if (set(e)!={'item_id','mapped','shape_agrees','category_agrees','category','readings'}
                    or e['item_id']!=item['item_id'] or any(type(e[k]) is not bool for k in ('mapped','shape_agrees','category_agrees'))
                    or e['category']!=item['category'] or len(e['readings'])<2):raise ValueError()
        for key,evidence in record.get('rule_evidence',{}).items():
            if (key not in {x['item_id'] for x in items} or
                    set(evidence)!={'rule_id','revision','method'} or
                    not isinstance(evidence['rule_id'],str) or not evidence['rule_id'] or
                    type(evidence['revision']) is not int or evidence['revision']<1 or
                    evidence['method']!='approved_product_rule'):raise ValueError()
        targets=record['adjustment_targets'];by_id={x['item_id']:x for x in items}
        if not isinstance(targets,dict):raise ValueError()
        for source,target in targets.items():
            if (source==target or source not in by_id or target not in by_id or
                by_id[source]['kind'] not in {'tax','discount'} or by_id[target]['kind']!='product'):raise ValueError()
    except StateError:raise
    except Exception:raise StateError('item_review_candidate_invalid') from None
    return record


def approved_product_prefill(record,categories,rules,*,imported_at):
    """Reuse exact, previously approved PRODUCT rules before owner editing.

    The trusted caller supplies original ingestion time, not Sheet input.
    Receipt-wide/service rules never classify individual products. No rule
    can clear a structural Gate or manufacture a trusted item boundary.
    """
    from .category_rules import match_transaction
    from .reconciliation import ImportTransaction
    validate(record);result=deepcopy(record);base=record['legacy']
    if (base['hard_issues'] or not base['prefill']['date'] or not base['prefill']['amount'] or
            len(base.get('item_category_evidence',[]))!=len(record['items'])):return result
    product_rules=[rule for rule in rules if rule.kind=='product']
    for item in result['items']:
        if item['kind']!='product':continue
        tx=ImportTransaction(0,'receipt:'+base['identity']['receipt_unit_id'],'receipt',
            base['prefill']['date'],base['prefill']['merchant'],item['amount'],'', '', '', [],imported_at)
        match=match_transaction(product_rules,tx,set(categories),product_name=item['name'],aggregate_only=False)
        if match.state=='conflict':item['category']=''
        elif match.state=='matched':
            pair='｜'.join(match.rule.category)
            if item['category'] and item['category']!=pair:
                item['category']='' # Conflicting Gemini/rule evidence is not a prefill.
            else:
                item['category']=pair
                result.setdefault('rule_evidence',{})[item['item_id']]={
                    'rule_id':match.rule.rule_id,'revision':match.rule.revision,'method':'approved_product_rule'}
    result.pop('digest');result['digest']=digest(result)
    return validate(result)


def fields(record):
    validate(record)
    return {**{f:record['legacy']['prefill'][f] for f in FIELDS},'action':'未選択',
            'items':deepcopy(record['items']),**({'structure_confirmation':'未選択'} if 'review_evidence' in record else {})}


def bulk_category(record,current,category,categories):
    """Explicit convenience only: fill blanks, preserve EVERY nonblank choice."""
    validate(record)
    if tuple(category.split('｜')) not in set(categories):raise StateError('item_category_invalid')
    result=deepcopy(current)
    for item in result['items']:
        if item['category']=='':item['category']=category
    return result


def evaluate(record,current,categories,*,confirmation=None):
    validate(record);base=record['legacy'];issues=base['hard_issues'][:];missing=[];provenance={}
    review='review_evidence' in record
    from .receipt_item_confirmation import covers,RESOLVABLE,CHOICES
    confirmed=review and covers(confirmation,record,current)
    if confirmed:issues=[i for i in issues if i not in RESOLVABLE]
    keys=set(FIELDS)|{'items','action'}|({'structure_confirmation'} if review else set())
    if set(current)!=keys or current['action'] not in ACTIONS or (review and current['structure_confirmation'] not in CHOICES):
        raise StateError('item_review_input_invalid')
    parsed=deepcopy(base['parsed'])
    for field in FIELDS:
        value=current[field];old=base['prefill'][field]
        provenance[field]=('missing' if value in ('',None) else 'gemini' if old not in ('',None) and
             _normalized(field,value)==_normalized(field,old) else 'human_override' if old not in ('',None) else 'human')
    day=_normalized('date',current['date']);amount=_normalized('amount',current['amount'])
    from .receipt_reimport import _date
    if not _date(day):missing.append('date')
    if type(amount) is not int or not 1<=amount<=99999999:missing.append('amount')
    optional={}
    for field,maximum in [('merchant',100),('payment',50),('memo',300)]:
        value=current[field]
        if not isinstance(value,str) or len(value)>maximum:issues.append('optional_field_invalid')
        else:optional[field]=value
    incoming=current['items']
    if not isinstance(incoming,list) or len(incoming)!=len(record['items']):raise StateError('item_review_item_count_changed')
    category_sources={};item_sources={};pairs={};seen=set();changed=False
    for original,item,out in zip(record['items'],incoming,parsed['items']):
        editable={'category','name','amount'} if review else {'category'}
        if (set(item)!=set(original) or any(item[k]!=original[k] for k in original if k not in editable)
                or item['item_id'] in seen):raise StateError('item_review_item_identity_changed')
        if (not isinstance(item['name'],str) or not item['name'].strip() or len(item['name'])>200
                or type(item['amount']) is not int or abs(item['amount'])>99999999
                or (item['kind']=='discount' and item['amount']>=0)
                or (item['kind']!='discount' and item['amount']<0)):
            raise StateError('item_review_corrected_item_invalid')
        altered=any(item[k]!=original[k] for k in ('name','amount'));changed=changed or altered
        item_sources[item['item_id']]={k:'human_override' if item[k]!=original[k] else 'gemini' for k in ('name','amount')}
        out.update(name=item['name'],amount=item['amount'])
        seen.add(item['item_id']);value=item['category'];pair=tuple(str(value).split('｜'))
        category_sources[item['item_id']]=('missing' if value=='' else 'human' if
            item['item_id'] in record.get('rule_evidence',{}) and value==original['category'] else 'gemini' if value==original['category'] and
            value!='' else 'human_override' if original['category']!='' else 'human')
        if pair not in set(categories):missing.append('item:'+item['item_id']);continue
        pairs[item['item_id']]=pair;out.update(major_category=pair[0],minor_category=pair[1])
        out['note']='; '.join(filter(None,[out.get('note',''),
            'category_source='+category_sources[item['item_id']]]))
    if review and not confirmed and (changed or record['review_evidence']['full_structure_confirmation_required']):
        issues.append('human_structure_confirmation_required')
    # Net item discounts are already reflected in printed amounts. Do not add
    # them twice. Standalone adjustments are retained with their printed sign.
    # For mixed product categories an unallocated global tax/discount is held;
    # this phase does not invent a proportional split or a balancing item.
    product_pairs={pairs[i['item_id']] for i in incoming if i['kind']=='product' and i['item_id'] in pairs}
    for item in incoming:
        if item['kind']=='product' or item['item_id'] not in pairs:continue
        target=record['adjustment_targets'].get(item['item_id'])
        if target:
            if target not in pairs or pairs[target]!=pairs[item['item_id']]:issues.append('adjustment_category_conflict')
        elif len(product_pairs)>1:issues.append('tax_discount_allocation_review')
        elif product_pairs and pairs[item['item_id']] not in product_pairs:issues.append('adjustment_category_conflict')
    if not missing:
        parsed.update(date=day,total=amount,merchant=optional.get('merchant',''),
                      payment_method=optional.get('payment',''),note=optional.get('memo',''))
        result=ReceiptResult.model_validate(parsed)
        _,validation=validate_receipt_result(result,categories);issues.extend(validation)
        if result.transaction_kind!='purchase':issues.append('transaction_kind_review')
        parsed=result.model_dump()
    if any(re.search('調整|差額補正',x['name']) for x in incoming):
        issues.append('adjustment_original_evidence_required')
    issues=list(dict.fromkeys(issues))
    status='needs_review' if issues else 'needs_human_completion' if missing else 'ready_to_confirm'
    if current['action']=='保留する':status='held'
    return {'status':status,'required_fields_complete':not missing,'missing_required':missing,
        'issues':issues,'parsed':parsed if not missing else None,'provenance':provenance,
        'item_category_provenance':category_sources,'item_field_provenance':item_sources,
        'item_sum':sum(i['amount'] for i in incoming),
        'posting_authority':False,**FLAGS}


def card(record,current=None,*,categories):
    validate(record);current=current or fields(record);result=evaluate(record,current,categories)
    legacy=record['legacy'];ident={**legacy['identity'],'schema':SCHEMA,
        'kind':'general_receipt_item_review','page_numbers':[legacy['identity']['page_number']],
        'candidate_digest':record['digest'],'item_ids':[i['item_id'] for i in record['items']]}
    labels={'needs_review':'記帳保留：商品明細等の確認が必要です',
            'needs_human_completion':'不足項目だけ入力してください',
            'ready_to_confirm':'入力検証済み・記帳要求待ち','held':'保留・入力値を保持しています'}
    evidence={e['item_id']:e for e in record.get('review_evidence',{}).get('items',[])}
    item_rows=[]
    for i in current['items']:
        item_rows.append((f"item:{i['item_id']}",i['name'],i['amount']))
        e=evidence.get(i['item_id'])
        if e and (not e['shape_agrees'] or not e['category_agrees']) and i['kind']=='product':
            detail=' ／ '.join(('初回' if n==0 else '再読取')+'：'+s['name']+'・'+str(s['amount'])+'円・'+s['category']
                              for n,s in enumerate(e['readings']))
            item_rows.append((f"compare:{i['item_id']}",'読取比較',detail))
    rows=[('target','対象',f"p{ident['page_number']} レシート {ident['receipt_index']}"),
        ('state','状態',labels[result['status']]),('original','原本','ページ全体を見る'),
        ('date','支払日',current['date']),('amount','レシート合計（円）',current['amount']),
        ('merchant','店舗名（任意）',current['merchant']),('payment','支払方法（任意）',current['payment']),
        ('memo','メモ（任意）',current['memo']),
        *([('legacy_category','旧カテゴリ（参考）',record['legacy_category'])] if record['legacy_category'] else []),
        ('reason','確認理由','商品明細の再読取結果が一致しません' if
         any('item_structure_changed' in i for i in result['issues']) else '／'.join(result['issues']) or '必須欄と各商品カテゴリを確認してください'),
        ('notice','入力案内','黄色のカテゴリとオレンジの明細を原本確認。「確認済み」→「記帳する」→本人確認。今回はplan検証のみ。' if 'review_evidence' in record else '解析候補を原本と照合。商品ごとのカテゴリを選択してください。'),
        ('item_header','商品名','金額'),
        *item_rows,
        *([('structure_confirmation','原本と明細を確認',current['structure_confirmation'])] if 'review_evidence' in record else []),
        ('action','レシート単位の操作',current['action']),
        ('result','処理結果','検証中：会計writeは無効。要求・記帳完了は別状態です。')]
    token=digest([ident,[(r[0],'' if r[0].startswith('item:') and 'review_evidence' in record else r[1]) for r in rows]])
    review_items=[e['item_id'] for e in record.get('review_evidence',{}).get('items',[]) if not e['shape_agrees'] or not e['category_agrees']]
    return {'identity':ident,'token':token,'rows':rows,'items':deepcopy(current['items']),
            'hard_blocked':bool(result['issues']),'state':result['status'],
            **({'editable_items':True,'review_items':review_items} if 'review_evidence' in record else {})}


def snapshot(card,current):
    by_id={i['item_id']:i for i in current['items']}
    rows=[[f,by_id[f[5:]]['name'] if f.startswith('item:') and card.get('editable_items') else l,
           by_id[f[5:]]['amount'] if f.startswith('item:') and card.get('editable_items') else
           current[f] if f in FIELDS or f in {'action','structure_confirmation'} else v] for f,l,v in card['rows']]
    return {'identity':deepcopy(card['identity']),'token':card['token'],'rows':rows,
            'items':deepcopy(current['items']),'original_link':original_uri(
                card['identity']['source_file_id'],card['identity']['page_number'])}


def check_snapshot(value,expected):
    if (set(value)!={'identity','token','rows','items','original_link'} or
            value['identity']!=expected['identity'] or value['token']!=expected['token'] or
            value['original_link']!=original_uri(expected['identity']['source_file_id'],expected['identity']['page_number'])
            or len(value['rows'])!=len(expected['rows'])):raise StateError('item_review_snapshot_stale')
    current={'items':deepcopy(value['items'])}
    by_id={i['item_id']:i for i in current['items']}
    for actual,trusted in zip(value['rows'],expected['rows']):
        if trusted[0].startswith('item:') and expected.get('editable_items'):
            key=trusted[0][5:]
            if len(actual)!=3 or actual[0]!=trusted[0] or key not in by_id or actual[1:]!=[by_id[key]['name'],by_id[key]['amount']]:
                raise StateError('item_review_presentation_changed')
            continue
        if len(actual)!=3 or actual[:2]!=list(trusted[:2]):raise StateError('item_review_presentation_changed')
        f=trusted[0]
        if f in FIELDS or f in {'action','structure_confirmation'}:current[f]=actual[2]
        elif f not in {'state','result'} and actual[2]!=trusted[2]:raise StateError('item_review_presentation_changed')
    return current


def empty_state(binding):
    return {'schema':SCHEMA,'binding':binding,'requests':{},'generation':0,**FLAGS}


def permanent_event(request,record,confirmed_at):
    """Reuse the existing compact permanent/year-partitioned event schema.

    Per-item enums stay bound to the immutable completion digest and ledger
    item identities; no image, raw response, token or full UI snapshot here.
    The caller invokes this ONLY after exact accounting read-back.
    """
    from .receipt_audit import event
    ident=record['legacy']['identity']
    authority=digest(['receipt-item-posting-v1',request['request_id'],request['snapshot_digest'],
                      request['plan_digest'],request['actor']])
    sources=set(request['plan']['item_category_provenance'].values())
    provenance={**request['plan']['provenance'],'category':'human_override' if 'human_override' in sources
        else 'human' if 'human' in sources else 'gemini'}
    identity={'source_file_id':ident['source_file_id'],'source_content_hash':ident['source_content_hash'],
        'page_count':ident['page_count'],'page_number':ident['page_number'],
        'page_identity':ident['stable_page_identity'],'receipt_unit_id':ident['receipt_unit_id'],
        'review_identity':ident['review_identity'],'revision':ident['authority_revision']}
    return event(identity,request_id=request['request_id'],request_digest=request['actor']['request_digest'],
        event_type='imported',ledger_id='R-'+request['unit_id'],decision='confirmed',
        actor_id=request['actor']['actor_id'],confirmed_at=request.get('completed_at',confirmed_at),authority_digest=authority,
        reason_code='writer_exact_readback',provenance=provenance)


def validate_state(value,binding):
    try:
        if (set(value)!={'schema','binding','requests','generation',*FLAGS} or value['schema']!=SCHEMA
                or value['binding']!=binding or type(value['generation']) is not int or value['generation']<0
                or any(value[k] is not False for k in FLAGS)):raise ValueError()
        for rid,r in value['requests'].items():
            if (not UUID.fullmatch(rid) or r['request_id']!=rid or
                    r['status'] not in {'held','validated_not_written','needs_review','running','unknown','not_written','complete'}
                    or r['snapshot_digest']!=digest(r['snapshot']) or
                    r['candidate_digest']!=r['snapshot']['identity']['candidate_digest']
                    or r['plan_digest']!=digest(r['plan']) or r['plan']['posting_authority'] is not False):raise ValueError()
    except Exception:raise StateError('item_review_state_invalid') from None
    return value


class ReviewStore(HumanGeneralAuthorityStore):
    def __init__(self,transport,binding,*,preflight):
        super().__init__(transport,binding,preflight=preflight,validator=validate_state)


class ReviewRequests:
    """Existing queue/runner adapter, NOT onEdit and NOT a writer.

    fresh(record) delegates to GeneralCompletion.fresh for source/page/HGA.
    verified_actor is trusted server evidence bound to request+snapshot, never
    a Sheet email or the script's effective user. Durable ETag writes remain.
    """
    def __init__(self,store,*,fresh,categories,read_current,verified_actor,duplicates,owner_actor_id,clock=time.time,
                 structure_confirmation=None):
        self.store,self.fresh,self.categories=store,fresh,categories
        self.read_current,self.actor,self.duplicates=read_current,verified_actor,duplicates
        if not re.fullmatch('[0-9a-f]{64}',owner_actor_id):raise StateError('item_review_owner_policy_required')
        self.owner_actor_id,self.clock=owner_actor_id,clock
        self.structure_confirmation=structure_confirmation or (lambda *_:None)

    def actor_evidence(self,rid,snapshot_digest,identity):
        actor=self.actor(rid,snapshot_digest,identity)
        bound=digest([SCHEMA,'post_receipt',rid,snapshot_digest,identity])
        if (not isinstance(actor,VerifiedActor) or actor.issuer!=ISSUER or
                actor.actor_id!=self.owner_actor_id or actor.method!='google_oidc_code_pkce_v1' or
                actor.request_id!=rid or actor.request_digest!=bound or actor.verification_revision!=1 or
                type(actor.policy_revision) is not int or actor.policy_revision<1 or
                type(actor.verified_at) is not int or not actor.verified_at<=self.clock()<actor.verified_at+600):
            raise StateError('item_review_verified_actor_required')
        # No email, token or subject copy into the receipt completion journal.
        return {'actor_id':actor.actor_id,'method':actor.method,'verified_at':actor.verified_at,
                'request_id':rid,'request_digest':bound,'policy_revision':actor.policy_revision}

    def capture(self,rid,record,snap):
        if not isinstance(rid,str) or not UUID.fullmatch(rid):raise StateError('item_review_request_invalid')
        self.fresh(record['legacy']);expected=card(record,categories=self.categories())
        current=check_snapshot(snap,expected)
        if current['action']=='未選択':raise StateError('item_review_explicit_request_required')
        if self.read_current(record)!=snap:raise StateError('item_review_input_changed')
        state=self.store.load();prior=state['requests'].get(rid)
        if prior:
            if prior['snapshot_digest']!=digest(snap):raise StateError('item_review_request_replaced')
            return {**deepcopy(prior),'replayed':True}
        unit=record['legacy']['identity']['receipt_unit_id']
        if any(r['unit_id']==unit and r['status'] in {'validated_not_written','running','complete','unknown'} for r in state['requests'].values()):
            raise StateError('item_review_existing_request_requires_readback')
        proof=self.structure_confirmation(rid,record,snap,current)
        plan=evaluate(record,current,self.categories(),confirmation=proof);actor=None
        if current['action']=='記帳する':
            actor=self.actor_evidence(rid,digest(snap),record['legacy']['identity'])
            if plan['status']=='ready_to_confirm' and self.duplicates(record['legacy']['identity'],plan) is not False:
                plan['status']='needs_review';plan['issues'].append('duplicate_or_unknown')
        status='held' if current['action']=='保留する' else 'validated_not_written' if plan['status']=='ready_to_confirm' else 'needs_review'
        request={'request_id':rid,'unit_id':unit,'snapshot':deepcopy(snap),'snapshot_digest':digest(snap),
            'candidate_digest':record['digest'],'actor':deepcopy(actor),'status':status,
            'plan':plan,'plan_digest':digest(plan)}
        self.fresh(record['legacy'])
        if self.read_current(record)!=snap:raise StateError('item_review_input_changed')
        state['requests'][rid]=request;state['generation']+=1;self.store.save(state)
        if self.store.load()['requests'][rid]!=request:raise StateError('item_review_readback_mismatch')
        return {**request,'replayed':False}

    def execute(self,rid,record,*,existing_writer,accounting_readback,append_history,execution_enabled=False):
        if not execution_enabled:raise StateError('item_review_live_write_disabled')
        state=self.store.load();r=state['requests'].get(rid)
        if not r or r['candidate_digest']!=record['digest']:raise StateError('item_review_request_stale')
        self.fresh(record['legacy'])
        if r['status']=='complete':
            if accounting_readback(r)!='complete':raise StateError('item_review_accounting_conflict')
            return {'status':'complete','replayed':True,'additional_write':0}
        if r['status']=='running':
            outcome=accounting_readback(r)
        elif r['status']=='validated_not_written':
            if self.read_current(record)!=r['snapshot']:raise StateError('item_review_input_changed')
            if self.actor_evidence(rid,r['snapshot_digest'],record['legacy']['identity'])!=r['actor']:
                raise StateError('item_review_actor_changed')
            inputs=check_snapshot(r['snapshot'],card(record,categories=self.categories()))
            proof=self.structure_confirmation(rid,record,r['snapshot'],inputs)
            plan=evaluate(record,inputs,self.categories(),confirmation=proof)
            if plan!=r['plan'] or plan['status']!='ready_to_confirm' or self.duplicates(record['legacy']['identity'],plan) is not False:
                raise StateError('item_review_execution_stale')
            r['status']='running';state['generation']+=1;self.store.save(state)
            try:existing_writer(r) # Existing append-only writer only; no new row builder.
            except Exception:pass # Read-back decides outcome; NEVER resend.
            outcome=accounting_readback(r)
        else:raise StateError('item_review_not_executable')
        if outcome=='complete':
            if 'completed_at' not in r:
                r['completed_at']=datetime.fromtimestamp(self.clock(),timezone.utc).isoformat()
                state['generation']+=1;self.store.save(state)
            # Adapter persists only compact event metadata, keyed by the same
            # request UUID; repeat invocation cannot duplicate a history event.
            append_history(r)
            r['status']='complete'
        elif outcome=='not_written':r['status']='not_written'
        else:r['status']='unknown'
        state['generation']+=1;self.store.save(state)
        return {'status':r['status'],'replayed':False,'terminal':r['status']=='complete',
                'visible_in_daily_review':r['status']!='complete'}
