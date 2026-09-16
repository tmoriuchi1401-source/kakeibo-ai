"""Bounded machine authority, separate from the owner's confirmation fields."""
from copy import deepcopy
from datetime import date
import hmac

from .drive_run_state import StateError
from .models import ReceiptResult, ReceiptItem
from .receipt_reimport_production import digest

POLICY='medical-auto-v1'
AUTO_POLICIES={'auto-v1:free','auto-v1:free:canary'}
WRITE_LIMIT=1
VALIDATION='closed_payment_cell_positive_glyphs_complete_ink'
VALIDATIONS={VALIDATION,'whitespace_text_region_positive_glyphs_complete_ink',
             'tolerant_ruled_cell_positive_glyphs_complete_ink','adjacent_ruled_cells_positive_glyphs_complete_ink',
             'ruled_separator_text_fields_positive_glyphs_complete_ink'}
HOLD_TEXT={
    'owner_input_or_decision':'本人の入力・判断を保持しています。',
    'candidate_missing_or_changed':'現在の原本に対応する候補がありません。',
    'analysis_not_complete':'画像解析が完了していません。',
    'saved_analysis_requires_reconciliation':'画像解析の結果を要照合。再送は停止しています。',
    'payment_ambiguous_or_unreadable':'実支払額が不明または複数あります。',
    'payment_meaning_not_unique':'実支払額の意味を一意に確認できません。',
    'payment_role_conflict':'別欄・注記が今回の実支払額を変え得るか未確定です。',
    'payment_date_missing_or_ambiguous':'支払日・発行日の根拠が不足または競合しています。',
    'issuer_missing_or_ambiguous':'発行施設を一意に確認できません。',
    'category_not_verified':'既存マスタの医療費カテゴリを確認できません。',
    'accounting_fields_invalid':'必要な会計項目を確認できません。',
    'automatic_run_limit':'今回の自動反映上限に達しました。次回に再判定します。',
    'possible_existing_payment':'同額の既存支払いがあり、重複関係を確認してください。',
    'existing_accounting_or_review_conflict':'既存の記帳・確認内容と競合しています。',
    'source_changed':'原本の版が変わりました。現在の原本を再判定します。',
    'owner_input_changed':'処理中の本人入力を保持しました。',
    'payment_cell_boundary_unknown':'金額欄の境界を確定できず、画像を送信していません。',
    'payment_region_ambiguous_or_absent':'金額欄が不明または複数あり、画像を送信していません。',
    'unaccounted_cell_ink':'金額欄に未確認の文字・図形があり、画像を送信していません。',
    'text_region_boundary_unknown':'金額欄の罫線・空白境界を確認できず、画像を送信していません。',
    'numeric_region_not_verified':'数字領域を安全に分離できず、画像を送信していません。',
    'payment_label_not_verified':'金銭ラベルを検証できず、画像を送信していません。',
    'cell_content_not_allowed':'金額欄の内容を限定できず、画像を送信していません。',
    'candidate_geometry_limit':'候補領域が多いため自動検査を保留しました。画像は未送信です。',
}


def in_scope(source, value, policy):
    return policy=='auto-v1:free' or (policy=='auto-v1:free:canary'
        and value.get('medical_auto_canary',{}).get('source')==source)


def owner_blocked(source,value):
    # A metadata/version change must not erase an owner's hold on this file.
    return any(x['kind']=='medical' and x['source']['source_id']==source['source_id']
        and (any(x['inputs']) or x.get('require_reconfirm') or x['status']=='closed_user')
        for x in value['confirmation_items'].values())


def send_allowed(source, mapping, value, policy):
    from .medical_anonymization import VERSION
    return (in_scope(source,value,policy) and mapping.get('automatic_policy')==POLICY
        and mapping.get('validation') in VALIDATIONS and mapping.get('preprocessor')==VERSION
        and mapping.get('source_sha256')==source['sha256'] and mapping.get('metadata_removed') is True
        and type(mapping.get('unresolved_candidates')) is int and mapping['unresolved_candidates']>=0
        and type(mapping.get('verified_payment_cells')) is int and mapping['verified_payment_cells']>=1)


def decide(item, value, categories, identity_key):
    """Return a parsed receipt only after independently checking durable evidence."""
    from .medical_candidate_runtime import response_tag
    from .medical_image_candidate import PaymentAnswer
    from .medical_anonymization import LABELS
    if any(item['inputs']) or item.get('require_reconfirm') or item.get('error'):
        return None,'owner_input_or_decision'
    c=item.get('medical_candidates',{});p=c.get('provenance',{})
    if not c or c.get('source')!=item['source'] or c.get('candidate_id')!=digest({k:v for k,v in c.items() if k!='candidate_id'}):
        return None,'candidate_missing_or_changed'
    if p.get('automatic_policy')!=POLICY:return None,p.get('reason','automatic_evidence_missing')
    aid=p.get('analysis_id');record=value.get('medical_image_analyses',{}).get(aid)
    if not record or record.get('phase')!='complete':return None,'analysis_not_complete'
    if not hmac.compare_digest(record.get('integrity_tag') or '',response_tag(aid,record,record['result'],identity_key)):
        raise StateError('medical_saved_result_integrity_failed')
    mapping=record['mapping']
    if (record['source']!=item['source'] or not send_allowed(item['source'],mapping,value,'auto-v1:free')
            or p.get('crop_sha256')!=mapping['crop_sha256']):
        raise StateError('medical_auto_evidence_binding_changed')
    answer=PaymentAnswer.model_validate(record['result'])
    if mapping['verified_payment_cells']!=1:
        return None,'payment_meaning_not_unique'
    evaluation_id=p.get('accounting_evaluation_id')
    if evaluation_id:
        from .medical_accounting_roles import verify_evaluation,ACCOUNTING_SCOPE
        evidence=verify_evaluation(value,evaluation_id,aid,record,identity_key)
        impact=evidence.get('payment_impact',{})
        if (evidence.get('complete_candidate_correspondence') is not True
                or evidence.get('independent_payment_fields')!=1
                or impact.get('policy')!=ACCOUNTING_SCOPE
                or impact.get('target')!='current_actual_payment'
                or impact.get('receipt_context_verified') is not True
                or impact.get('complete_candidate_correspondence') is not True
                or impact.get('independent_competing_payment_fields')!=0
                or impact.get('unresolved_influence_groups')!=0
                or impact.get('unassigned_currency_fields')!=0):
            return None,'payment_role_conflict'
    elif mapping['unresolved_candidates']:
        return None,'payment_meaning_not_unique'
    if answer.status!='readable' or len(answer.candidates)!=1:return None,'payment_ambiguous_or_unreadable'
    amount=answer.candidates[0]
    if (amount.label not in LABELS or amount.label!=mapping.get('payment_label')
            or c.get('amount_yen')!=amount.amount_yen
            or p.get('admission')!='AUTO_ADMITTED_FOR_AUTHORITY_EVALUATION'):
        return None,'payment_meaning_not_unique'
    local=p.get('local',{});binding=local.get('document_binding',{})
    if any(binding.get(k)!=mapping.get(k) for k in ('source_sha256','source_image_sha256','page','unit')):
        raise StateError('medical_auto_local_binding_changed')
    if not c.get('date') or local.get('date_candidates')!=1 or local.get('date_evidence_verified') is not True:
        return None,'payment_date_missing_or_ambiguous'
    if (local.get('date_basis')=='issue' and local.get('paid_receipt_evidence') is not True
            and mapping.get('payment_label')!='今回入金額'):
        return None,'payment_date_missing_or_ambiguous'
    if not c.get('issuer') or local.get('issuer_status')!='SELECTED_ISSUER':return None,'issuer_missing_or_ambiguous'
    category=str(c.get('category','')).split('｜')
    if len(category)!=2 or tuple(category) not in categories or category[0]!='医療・保険':return None,'category_not_verified'
    try:
        if date.fromisoformat(c['date']).isoformat()!=c['date']:return None,'payment_date_missing_or_ambiguous'
        parsed=ReceiptResult(date=c['date'],merchant=c['issuer'],total=amount.amount_yen,payment_method='',
            items=[ReceiptItem(name='医療費（自動判定）',amount=amount.amount_yen,major_category=category[0],minor_category=category[1])])
        from .receipt_pipeline import validate_receipt_result
        if not validate_receipt_result(parsed,categories)[0]:return None,'accounting_fields_invalid'
    except (ValueError,TypeError):return None,'accounting_fields_invalid'
    return parsed,'eligible'


def possible_duplicate(parsed,tables):
    """Conservative automation; uncertain card/other payments stay for review."""
    from .receipt_reimport import _date,_money
    for kind,rows in tables.items():
        if kind not in {'expense_rows','import_rows'}:continue
        for row in rows:
            if kind=='expense_rows':
                if len(row)<13 or row[12]!='active':continue
                day,amount=row[1],row[4]
            else:
                if len(row)<9:continue
                day,amount=row[4],row[6]
            if _money(str(amount))!=parsed.total:continue
            day=_date(day)
            # An equal amount with an unusable date cannot be ruled out.
            if not day or abs((date.fromisoformat(day)-date.fromisoformat(parsed.date)).days)<=31:return True
    return False


def apply_automatic(review, *, identity_key, policy):
    if policy not in AUTO_POLICIES:return 0
    written=0
    for key,old in list(review.items.items()):
        if old['kind']!='medical' or old['status']!='waiting':continue
        if not in_scope(old['source'],review.store.value,policy):continue
        if owner_blocked(old['source'],review.store.value):continue
        live=review.ui_rows().get(key)
        if live is None or live[1][7:15]!=old['inputs'] or any(live[1][7:15]):continue
        parsed,reason=decide(old,review.store.value,review.db.categories(),identity_key)
        item=deepcopy(old)
        if parsed is not None:
            if written>=WRITE_LIMIT:reason='automatic_run_limit';parsed=None
            elif possible_duplicate(parsed,review.tables()):reason='possible_existing_payment';parsed=None
        if parsed is None:
            if item.get('automatic_hold')!=reason:
                item['automatic_hold']=reason;review.save_item(key,item)
            continue
        try:
            review.verify_source(item['source'],item['folder_id'])
            plan=review._plan(item,parsed,'',automatic=True)
        except ValueError:
            item['automatic_hold']='existing_accounting_or_review_conflict';review.save_item(key,item);continue
        except StateError as error:
            if str(error)!='confirmation_source_changed':raise
            item['automatic_hold']='source_changed';review.save_item(key,item);continue
        # No fabricated M-column choice or human signature. The separate
        # machine decision and full write intent precede every accounting call.
        from .medical_accounting_roles import ACCOUNTING_SCOPE
        item.update(status='pending',plan=plan,decision_origin='automatic',automatic_decision={
            'policy':POLICY,'source':deepcopy(item['source']),
            'accounting_scope':ACCOUNTING_SCOPE,
            'candidate_id':item['medical_candidates']['candidate_id'],
            'analysis_id':item['medical_candidates']['provenance']['analysis_id'],
            'accounting_evaluation_id':item['medical_candidates']['provenance'].get('accounting_evaluation_id')})
        item.pop('automatic_hold',None);review.save_item(key,item)
        try:review.verify_source(item['source'],item['folder_id'])
        except StateError as error:
            if str(error)!='confirmation_source_changed':raise
            item.update(status='waiting',automatic_hold='source_changed',aborted_before_accounting=True)
            item.pop('plan',None);review.save_item(key,item);continue
        live=review.ui_rows().get(key)
        if live is None or any(live[1][7:15]):
            # This invocation made zero writes. Earlier pending intents are
            # reconciled by the existing writer, never reset through this path.
            item.update(status='waiting',automatic_hold='owner_input_changed',aborted_before_accounting=True)
            item.pop('plan',None);review.save_item(key,item);continue
        review._write_accounting_plan(key,item);written+=1
    return written
