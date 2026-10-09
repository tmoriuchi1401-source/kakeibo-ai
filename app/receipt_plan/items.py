"""Plan-only subset of PR #91 (1ac98bd); no live authority/writer transport."""
from copy import deepcopy
import re
from ..drive_run_state import StateError
from .completion import validate_draft,_normalized,FLAGS,UUID
from .models import ReceiptResult
from .identity import digest
from .fields import original_uri
from .validation import validate_receipt_result
SCHEMA='general-receipt-item-review-v1'
ACTIONS=('未選択','記帳する','保留する')
FIELDS=('date','amount','merchant','payment','memo')
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

def fields(record):
    validate(record)
    return {**{f:record['legacy']['prefill'][f] for f in FIELDS},'action':'未選択',
            'items':deepcopy(record['items']),**({'structure_confirmation':'未選択'} if 'review_evidence' in record else {})}

def evaluate(record,current,categories,*,confirmation=None):
    validate(record);base=record['legacy'];issues=base['hard_issues'][:];missing=[];provenance={}
    review='review_evidence' in record
    from .proof import covers,RESOLVABLE,CHOICES
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
    from ..receipt_reimport import _date
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
