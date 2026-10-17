"""Offline draft-to-existing-validator previews. No acceptance or execution API.

Trusted canonical/native objects must come from the future host's fresh reader.
Passing this preview is NOT authentication, consent, authority or a POST request.
"""
from copy import deepcopy
import json

from .iphone_review_drafts import validate_handoff, fail
from .unified_receipt_eligibility import current_snapshot
from .unified_receipt_review import digest


def check_native_case(row, case, expected_native_digest):
    i = case.identity
    native = dict(source_file_id=i.get('source_file_id'), source_content_hash=i.get('source_content_hash'),
                  source_version=i.get('source_version'), page_numbers=case.pages,
                  page_identities=i.get('page_identities', [i.get('stable_page_identity')]),
                  receipt_unit_id=i.get('receipt_unit_id', ''), receipt_index=i.get('receipt_index', 0))
    kinds = {'normal': {'normal', 'general_receipt_item_review'}, 'medical': {'medical'}, 'grouping': {'grouping'}}
    if (row['case_id'] != case.case_id or row['identity'] != native or case.kind not in kinds[row['kind']]
            or case.history or case.related_to or case.issues or case.disposition == 'LEGACY_UNMAPPED_HOLD'
            or digest(current_snapshot(case)) != expected_native_digest):
        fail('native_case_or_snapshot_changed')


def result(handoff, checked, native_digest):
    return dict(state='dry_run_only', handoff=handoff, validation=deepcopy(checked),
                native_snapshot_digest=native_digest, operation_targets=[], authorized=False)


def preview_medical(row, canonical, expected_snapshot, case, native_digest, port):
    handoff = validate_handoff(row, canonical, expected_snapshot)
    check_native_case(row, case, native_digest)
    if row['kind'] != 'medical':
        fail('medical_route_required')
    # Preserve the existing native action/duplicate target. A Sheet has no grant
    # to alter those; this is validation of proposed manual fields only.
    draft = deepcopy(case)
    for f in draft.fields:
        if f[0] in row['inputs']:
            f[2] = deepcopy(row['inputs'][f[0]])
    from .unified_receipt_native import ReceiptConfirmationPort
    if not isinstance(port, ReceiptConfirmationPort):
        fail('existing_medical_port_required')
    checked = port.validate_draft(case, draft)
    return result(handoff, checked, native_digest)


def preview_general(row, canonical, expected_snapshot, case, native_digest,
                    record, accepted_snapshot, expected_accepted_digest, categories):
    handoff = validate_handoff(row, canonical, expected_snapshot)
    check_native_case(row, case, native_digest)
    if row['kind'] != 'normal' or digest(accepted_snapshot) != expected_accepted_digest:
        fail('general_snapshot_changed')
    # A single receipt-level category cannot overwrite categories of its items.
    if row['inputs']['category'] != '':
        fail('native_item_category_contract_required')
    from .receipt_item_review import card, check_snapshot, snapshot, evaluate
    view = card(record, categories=categories)
    identity = record['legacy']['identity']
    for k in ('source_file_id', 'source_content_hash', 'receipt_unit_id', 'receipt_index'):
        if identity[k] != row['identity'][k]:
            fail('general_item_identity_changed')
    if identity['stable_page_identity'] != row['identity']['page_identities'][0]:
        fail('general_page_identity_changed')
    current = check_snapshot(accepted_snapshot, view)
    for f in ('date', 'merchant', 'amount', 'payment', 'memo'):
        current[f] = deepcopy(row['inputs'][f])
    proposed = snapshot(view, current)
    current = check_snapshot(proposed, view)
    checked = evaluate(record, current, categories)  # No owner proof inferred.
    return result(handoff, dict(native_status=checked['status'],
        proposed_native_snapshot=proposed, proposed_native_digest=digest(proposed)), native_digest)


def structure_selection(observation):
    e = observation.evidence
    return json.dumps(dict(operation=e['operation'], partition=e['partition']),
                      sort_keys=True, separators=(',', ':'), ensure_ascii=True)


def preview_structure(row, canonical, expected_snapshot, observation, request_id):
    handoff = validate_handoff(row, canonical, expected_snapshot)
    from .unified_receipt_purposes import validate_observation, STRUCTURE_REVIEW
    o = validate_observation(observation, request_id)
    e, i = o.evidence, row['identity']
    if (row['kind'] != 'grouping' or o.purpose != STRUCTURE_REVIEW or o.case_id != row['case_id']
            or any(e['source'][k] != i[k] for k in ('source_file_id', 'source_content_hash', 'source_version'))
            or [p['page_number'] for p in e['pages']] != i['page_numbers']
            or [p['stable_page_identity'] for p in e['pages']] != i['page_identities']
            or row['inputs']['partition'] != structure_selection(o)):
        fail('structure_scope_or_selection_changed')
    return result(handoff, dict(native_status='contract_validated_not_executed',
                  accounting_allowed=False), digest(e))
