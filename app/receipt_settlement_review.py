"""Explicit unsigned candidate revision, preserving every owner input.

No IO, authority creation or writer. The caller must fresh-read source, Sheet
and registry, CAS the revision, and verify UI projection. A new confirmation
is required; an older confirmation is never migrated to the changed items.
"""
from copy import deepcopy
from .drive_run_state import StateError
from .page_receipt_model import digest
from .receipt_plan.models import ReceiptResult
from .receipt_item_review import validate, prepare, card, FIELDS
from .general_receipt_completion import validate_draft
from .receipt_settlement import add_external_tax


def repair_candidate(unit, current, facts, categories):
    candidate = unit['candidate']
    if unit['status'] in {'imported', 'duplicate_confirmed', 'reconciled_existing'} or any(
            unit[k] is not None for k in ('posting_authority', 'intent', 'readback')) or any(
            k in candidate for k in ('posting', 'confirmation_journal', 'settlement_repair')):
        raise StateError('settlement_authenticated_or_repaired_candidate_immutable')
    old = validate(candidate['record'])
    if len(current['items']) != len(old['items']) or any(
            a['item_id'] != b['item_id'] for a, b in zip(current['items'], old['items'])):
        raise StateError('settlement_item_identity_changed')
    parsed = deepcopy(old['legacy']['parsed'])
    for raw, item in zip(parsed['items'], current['items']):
        if item['kind'] == 'discount' and item['amount'] >= 0 or item['kind'] != 'discount' and item['amount'] < 0:
            raise StateError('settlement_item_sign_changed')
        pair = tuple(item['category'].split('｜'))
        if pair not in set(categories):
            raise StateError('settlement_owner_category_required')
        raw.update(name=item['name'], amount=item['amount'], major_category=pair[0], minor_category=pair[1])
    if type(current['amount']) not in (int, float) or current['amount'] != facts.payable:
        raise StateError('settlement_owner_total_conflict')
    parsed['total'] = facts.payable
    fixed = add_external_tax(ReceiptResult.model_validate(parsed), facts, categories)
    if len(fixed.items) != len(current['items']) + 1:
        raise StateError('settlement_tax_already_present')
    legacy = deepcopy(old['legacy'])
    legacy['parsed'] = fixed.model_dump()
    legacy['item_category_evidence'] = [
        {'item_index':i+1, 'category':'', 'corroborated':False} for i in range(len(fixed.items))]
    # A printed fact fixes a tax omission, never unrelated structural gates.
    legacy.pop('candidate_digest'); legacy['candidate_digest'] = digest(legacy)
    validate_draft(legacy)
    record = prepare(legacy, categories)
    if 'review_evidence' in old:
        evidence = deepcopy(old['review_evidence'])
        for item, e in zip(record['items'], evidence['items']):
            e.update(item_id=item['item_id'], category=item['category'])
        tax = record['items'][-1]
        pair = '｜'.join((fixed.items[-1].major_category, fixed.items[-1].minor_category))
        tax['category'] = pair
        # These are corrected readings sharing the SAME local printed tax
        # evidence, not a claim of independent Gemini observation of that row.
        evidence['items'].append({'item_id':tax['item_id'], 'mapped':True, 'shape_agrees':True,
            'category_agrees':True, 'category':pair, 'readings':[
                {'name':tax['name'], 'quantity':1, 'amount':tax['amount'], 'category':pair}] * 2})
        record['review_evidence'] = evidence
    record.pop('digest'); record['digest'] = digest(record); validate(record)
    changed = {f:deepcopy(current[f]) for f in FIELDS}
    changed['items'] = []
    for original, revised in zip(current['items'], record['items']):
        changed['items'].append({**revised, **{k:original[k] for k in ('name', 'amount', 'category')}})
    tax = record['items'][-1]
    changed['items'].append({**tax, 'category':'｜'.join((fixed.items[-1].major_category, fixed.items[-1].minor_category))})
    changed['action'] = '未選択'
    if 'review_evidence' in record:
        changed['structure_confirmation'] = '未選択'
    for f in FIELDS:
        if changed[f] != current[f]:
            raise StateError('settlement_owner_input_changed')
    result = deepcopy(candidate)
    result.update(record=record, view=card(record, changed, categories=categories), ui_projected=True)
    result['settlement_repair'] = {'schema_version':1, 'before_record_digest':old['digest'],
        'before_input_digest':digest(current), 'after_input_digest':digest(changed),
        'evidence_digest':facts.evidence_digest, 'method':facts.method,
        'net':facts.net, 'gross':facts.gross, 'external_tax':facts.tax, 'payable':facts.payable,
        'tax_item_id':tax['item_id'], 'provenance':'source_document',
        'category_provenance':'same_product_category', 'confirmation_required':True}
    return result, changed
