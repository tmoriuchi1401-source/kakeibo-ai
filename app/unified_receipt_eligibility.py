"""Read-only eligibility planning; never dispatches, issues or migrates authority.

Evidence is a local audit manifest of existing validator results. A proposed
target is a reference, not an enabled action or financial permission. A future
writable adapter must freshly run the native validators; this module is not one.
"""
from dataclasses import dataclass
import re

from .drive_run_state import StateError
from .unified_receipt_review import digest, LEGACY_UNMAPPED_HOLD

PURPOSES = {'normal', 'general_receipt_item_review', 'medical', 'receipt_reconciliation'}
WRITERS = {'receipt_confirmation', 'pdf_intake', 'receipt_unit_posting'}
IDENTITY_FIELDS = ('source_file_id', 'source_content_hash', 'source_version',
                   'stable_page_identity', 'receipt_unit_id', 'receipt_index', 'review_purpose')


@dataclass(frozen=True)
class Eligibility:
    eligible: bool
    reasons: tuple
    proposed_target: str | None = None
    # PR #123 cannot expose an enabled action, even for a fully clean case.
    operation_targets: tuple = ()


def identity_scope(case):
    i = case.identity
    scope = {k:i.get(k) for k in IDENTITY_FIELDS}
    scope['page_numbers'] = case.pages
    scope['page_identities'] = i.get('page_identities', [i.get('stable_page_identity')])
    return scope


def current_snapshot(case):
    # Keep the full schema/token/immutable identity and raw field values. Do not
    # apply display-date normalization to a confirmation/freshness digest.
    return dict(schema=case.schema,legacy_id=case.legacy_id,identity=case.identity,fields=case.fields)


def routes_for(evidence):
    routes = evidence.get('routes', [])
    if not isinstance(routes, list) or any(not isinstance(r,dict) for r in routes):
        raise StateError('unified_operation_routes_invalid')
    if any(r.get('state') not in {'active','readonly','retired'} for r in routes):
        raise StateError('unified_operation_route_state_unknown')
    active = [r for r in routes if r.get('state') == 'active']
    targets = {r.get('target_id') for r in active}
    if len(targets) > 1:
        # Safety stop even when another eligibility condition is missing.
        raise StateError('unified_multiple_operation_targets')
    if active and (None in targets or '' in targets):
        raise StateError('unified_operation_target_missing')
    bindings = {(r.get('scope_digest'),r.get('snapshot_digest'),r.get('writer')) for r in active}
    if len(bindings) > 1:
        raise StateError('unified_operation_target_binding_conflict')
    return active


def evaluate(case, evidence=None):
    evidence = evidence or {}
    active = routes_for(evidence)
    if case.disposition == LEGACY_UNMAPPED_HOLD:
        if active:
            raise StateError('unified_legacy_operation_forbidden')
        return Eligibility(False, ('LEGACY_UNMAPPED_HOLD',))
    reasons = []
    if case.history or case.related_to or case.issues or case.authority_status in {
        'superseded','applied','imported','closed_machine','closed_user'}:
        reasons.append('not_a_clean_new_case')
    i = case.identity; scope = identity_scope(case)
    if any(not scope[k] for k in IDENTITY_FIELDS):
        reasons.append('identity_incomplete')
    if (not isinstance(scope['source_file_id'],str)
            or not re.fullmatch(r'[A-Za-z0-9_-]{1,150}',scope['source_file_id'] or '')
            or not isinstance(scope['source_version'],str) or not scope['source_version']
            or not isinstance(scope['receipt_unit_id'],str) or not scope['receipt_unit_id']
            or type(scope['receipt_index']) is not int or scope['receipt_index'] < 1
            or scope['review_purpose'] not in PURPOSES):
        reasons.append('identity_invalid')
    if any(not isinstance(scope[k],str) or not re.fullmatch('[0-9a-f]{64}',scope[k] or '')
           for k in ('source_content_hash','stable_page_identity')):
        reasons.append('hash_or_page_identity_invalid')
    if i.get('page_identity') and i['page_identity'] != scope['stable_page_identity']:
        reasons.append('page_identity_conflict')
    pages=scope['page_numbers']; page_ids=scope['page_identities']
    if (not pages or any(type(n) is not int or n < 1 for n in pages) or len(set(pages))!=len(pages)
            or not isinstance(page_ids,list) or len(page_ids)!=len(pages)
            or len(set(page_ids))!=len(page_ids) or page_ids[0]!=scope['stable_page_identity']):
        reasons.append('page_range_invalid')
    expected_pages = [dict(source_file_id=scope['source_file_id'],source_content_hash=scope['source_content_hash'],
                           source_version=scope['source_version'],page_number=n,stable_page_identity=p)
                      for n,p in zip(pages,page_ids)] if isinstance(page_ids,list) else []
    if evidence.get('page_bindings') != expected_pages:
        reasons.append('page_identity_not_uniquely_observed')
    if evidence.get('observed_identity') != scope:
        reasons.append('source_identity_or_version_changed')
    scope_digest = digest(scope); snapshot_digest = digest(current_snapshot(case))
    if evidence.get('snapshot_digest') != snapshot_digest:
        reasons.append('current_snapshot_changed_or_unverified')
    duplicate=evidence.get('duplicate_check', {})
    if (duplicate.get('scope_digest') != scope_digest or duplicate.get('snapshot_digest') != snapshot_digest
            or duplicate.get('status') not in {'none','resolved_existing','resolved_distinct'}
            or duplicate.get('verified') is not True):
        reasons.append('duplicate_check_unresolved')
    permission=evidence.get('permissions', {})
    if (permission.get('scope_digest') != scope_digest or permission.get('snapshot_digest') != snapshot_digest
            or permission.get('verified') is not True or not permission.get('authority_reference')
            or permission.get('posting') not in {'allowed','link_only'}):
        reasons.append('unit_posting_permission_missing')
    # Denied sending is sufficient for a manual/no-send route. Required sending
    # needs its own exact Unit permission; a type selection never substitutes.
    if (permission.get('ai_send') not in {'allowed','denied'}
            or type(evidence.get('send_required')) is not bool
            or evidence.get('send_required') is True and permission.get('ai_send') != 'allowed'):
        reasons.append('unit_send_permission_missing')
    if case.kind == 'medical' and (permission.get('ai_send') != 'denied'
                                  or evidence.get('send_required') is not False
                                  or evidence.get('input_origin') != 'fully_manual'):
        reasons.append('medical_manual_no_ai_required')
    if scope['review_purpose']=='receipt_reconciliation' and (
            duplicate.get('status')!='resolved_existing' or permission.get('posting')!='link_only'):
        reasons.append('reconciliation_link_only_required')
    if permission.get('posting')=='link_only' and duplicate.get('status')!='resolved_existing':
        reasons.append('link_without_resolved_existing')
    writer=evidence.get('writer', {})
    if (writer.get('name') not in WRITERS or writer.get('scope_digest')!=scope_digest
            or writer.get('readback') is not True or writer.get('replay_protected') is not True
            or writer.get('verified') is not True):
        reasons.append('native_readback_or_replay_unavailable')
    if (not active or any(r.get('scope_digest')!=scope_digest or r.get('snapshot_digest')!=snapshot_digest
                         or r.get('writer')!=writer.get('name') for r in active)):
        reasons.append('unique_operation_owner_unavailable')
    return Eligibility(not reasons,tuple(reasons),active[0]['target_id'] if active and not reasons else None)


def evaluate_batch(cases, evidence_by_case=None):
    evidence_by_case=evidence_by_case or {}
    if len({c.case_id for c in cases}) != len(cases):
        raise StateError('unified_duplicate_case_reference')
    decisions = {c.case_id:evaluate(c,evidence_by_case.get(c.case_id)) for c in cases}
    quarantined=[c for c in cases if c.disposition==LEGACY_UNMAPPED_HOLD]
    for c in cases:
        if decisions[c.case_id].eligible and any(
                c.source_id==old.source_id and c.identity.get('source_content_hash')==old.identity.get('source_content_hash')
                and set(c.pages).intersection(old.pages) for old in quarantined):
            # This is a deny fence, never a Unit mapping or an authority join.
            decisions[c.case_id]=Eligibility(False,('legacy_page_reuse_forbidden',))
    # Two reference cards must never independently own operations for one Unit,
    # even if their review purposes differ. Do not silently pick one to run.
    owned={}; target_scopes={}; page_scopes={}
    for c in cases:
        d=decisions[c.case_id]
        if not d.eligible:
            continue
        if c.unit_key in owned:
            raise StateError('unified_multiple_operation_owners')
        owned[c.unit_key]=c.case_id
        scope=identity_scope(c)
        if d.proposed_target in target_scopes and target_scopes[d.proposed_target]!=scope:
            raise StateError('unified_operation_target_cross_unit')
        target_scopes[d.proposed_target]=scope
        for n,p in zip(scope['page_numbers'],scope['page_identities']):
            bound=(c.source_id,scope['source_content_hash'],scope['source_version'],n)
            if p in page_scopes and page_scopes[p]!=bound:
                raise StateError('unified_page_identity_ambiguous')
            page_scopes[p]=bound
    return decisions


def counts(decisions):
    return dict(eligible=sum(d.eligible for d in decisions.values()),
                proposed_target_0=sum(d.proposed_target is None for d in decisions.values()),
                proposed_target_1=sum(d.proposed_target is not None for d in decisions.values()),
                proposed_target_multiple=0,
                enabled_target_0=len(decisions),enabled_target_1=0,enabled_target_multiple=0)
