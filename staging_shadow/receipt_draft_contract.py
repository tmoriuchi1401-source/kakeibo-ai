"""Opt-in synthetic v3 facade, same ConditionalRequests CAS, no dispatch.

Inheritance is on the same memory backend, requires the caller's pinned v2
digest, and retains every old operation byte-for-byte in canonical JSON.
There is deliberately no live factory, migration, completion or retry API.
"""
from copy import deepcopy
from dataclasses import asdict
from functools import wraps

from app.drive_run_state import StateError
from app.iphone_accounting_ownership import hashed, relation
from app.iphone_accounting_gate import AccountingProbeRequest, SCHEMA as SHARED_SCHEMA
from app.receipt_confirmation_contract import (IsolatedConfirmationContract, IsolatedReimportStore,
    SCHEMA as OLD_SCHEMA, encoded, fail, load, memory_only, observe_ledger,
    planned_after, token, validate_operation as old_operation, validate_token, verify_attestation)
from app.receipt_confirmation_draft_request import (SCHEMA as INPUTS_SCHEMA, DraftReaders,
    pending_projection, validate_shape, validate_draft_request_v2)
from app.unified_receipt_purposes import PurposeBoundary, VerifiedPost
from app.unified_receipt_review import digest
from app.unified_receipt_write import ConditionalRequests

SCHEMA = 'receipt-reimport-v3'
PROVIDER = 'isolated-receipt-draft-v3-fixture'
LINEAGE = '__synthetic_v2_lineage__'


def new_request(r):
    return r.get('request', {}).get('inputs', {}).get('schema') == INPUTS_SCHEMA


def validate_operation(op, r):
    if set(r) != {'request', 'phase', 'qualification', 'claim', 'consume', 'pending', 'unstarted', 'start', 'completion'}:
        fail('v3_operation')
    c = validate_shape(r['request'])
    if op != c['operation'] or r['phase'] not in {'qualified', 'pending', 'writer_started'} or r['completion'] is not None:
        fail('v3_phase')
    q = r['qualification']
    if set(q) != {'provider', 'attestation', 'at', 'before_observation'} or q['provider'] != PROVIDER:
        fail('v3_qualification')
    validate_token(q['before_observation'])
    if type(q['at']) is not int: fail('v3_time')
    verify_attestation(VerifiedPost(**q['attestation']), c, q['at'])
    if r['phase'] == 'qualified':
        if any(r[k] is not None for k in ('claim', 'consume', 'pending', 'unstarted', 'start')): fail('v3_qualification')
        return c
    claim = r['claim']
    if (set(claim) != {'operation', 'request_id', 'fingerprint', 'accepted_at'} or claim['operation'] != op
            or claim['request_id'] != c['shared_request_id'] or not hashed(claim['fingerprint'])
            or type(claim['accepted_at']) is not int): fail('v3_claim')
    if r['pending'] != pending_projection(c): fail('v3_pending')
    consumed = r['consume']
    if (set(consumed) != {'authority_revision', 'request_id', 'fingerprint', 'pending_digest', 'at'}
            or type(consumed['at']) is not int or consumed != dict(authority_revision=c['authority']['revision'],
                request_id=c['request_id'], fingerprint=c['fingerprint'], pending_digest=digest(r['pending']), at=consumed['at'])
            or not c['authority']['issued_at'] <= consumed['at'] < c['authority']['expires_at']): fail('v3_consume')
    u = r['unstarted']
    if (set(u) != {'fingerprint', 'pending_digest', 'sequence', 'ledger'} or type(u['sequence']) is not int
            or u != dict(fingerprint=c['fingerprint'], pending_digest=digest(r['pending']), sequence=0,
                         ledger=q['before_observation'])): fail('v3_unstarted')
    if r['phase'] == 'pending':
        if r['start'] is not None: fail('v3_start')
        return c
    s = r['start']
    if (set(s) != {'fingerprint', 'claim_digest', 'consume_digest', 'pending_digest', 'sequence', 'at'}
            or type(s['at']) is not int or type(s['sequence']) is not int
            or s != dict(fingerprint=c['fingerprint'], claim_digest=digest(claim), consume_digest=digest(consumed),
                         pending_digest=digest(r['pending']), sequence=1, at=s['at'])
            or not consumed['at'] <= s['at'] < c['authority']['expires_at']): fail('v3_start')
    return c


class IsolatedDraftStore(IsolatedReimportStore):
    def __init__(self, io, expected_digest, spreadsheet_id, *, parent_digest=None, enabled=False, isolated_synthetic=False):
        super().__init__(io, expected_digest, spreadsheet_id, enabled=enabled, isolated_synthetic=isolated_synthetic)
        if parent_digest is not None and not hashed(parent_digest): fail('v3_trusted_parent_pin')
        self.parent_digest = parent_digest

    def validate(self, v):
        if v.get('schema') in {'receipt-reimport-v1', OLD_SCHEMA}:
            return super().validate(v)
        if (set(v) != {'schema', 'revision', 'legacy', 'operations'} or v['schema'] != SCHEMA
                or type(v['revision']) is not int or v['revision'] < 1 or type(v['operations']) is not dict
                or self.parent_digest is None): fail('v3_schema_UNKNOWN')
        marker = v['operations'].get(LINEAGE)
        if (type(marker) is not dict or set(marker) != {'schema', 'parent_revision', 'parent_digest', 'observation'}
                or marker['schema'] != 'synthetic-v2-inheritance-v1' or type(marker['parent_revision']) is not int
                or marker['parent_revision'] < 0 or marker['parent_digest'] != self.parent_digest
                or v['revision'] <= marker['parent_revision']): fail('v3_lineage_UNKNOWN')
        validate_token(marker['observation'])
        old_ops = {k: r for k, r in v['operations'].items() if k != LINEAGE and not new_request(r)}
        parent = dict(schema=OLD_SCHEMA, revision=marker['parent_revision'], legacy=v['legacy'], operations=old_ops)
        super().validate(parent)
        if digest(parent) != self.parent_digest: fail('v3_history_immutable')
        ids, shared_ids, sources = set(), set(), set()
        for op, r in v['operations'].items():
            if op == LINEAGE: continue
            c = validate_operation(op, r) if new_request(r) else old_operation(op, r)
            if (c['request_id'] in ids or c['shared_request_id'] in shared_ids or c['source']['source_id'] in sources):
                fail('v3_duplicate_or_fence')
            ids.add(c['request_id']); shared_ids.add(c['shared_request_id']); sources.add(c['source']['source_id'])
            if new_request(r) and (digest(v['legacy']) != c['inputs']['baseline']['legacy_digest']
                    or v['legacy'].get('confirmation_items', {}).get(c['review_id']) != c['inputs']['baseline']['item']):
                fail('v3_baseline_immutable')
        return v

    @property
    def value(self):
        v = self.read()[0]
        return deepcopy(v['legacy'] if v.get('schema') in {SCHEMA, OLD_SCHEMA} else v)

    def read(self):
        try:
            return super().read()
        except Exception as error:
            self.uncertain = True
            raise StateError('legacy_contract_v3_read_UNKNOWN') from error

    def _replace_exact(self, before, raw, tag, after):
        try:
            ConditionalRequests.replace(self, before, raw, tag, after)
            value, actual, new_tag = self.read()
            if value != after or actual != encoded(after) or new_tag == tag: fail('v3_readback_UNKNOWN')
            return token(actual, new_tag)
        except Exception:
            self.uncertain = True
            fail('v3_CAS_UNKNOWN_read_only')

    def inherit_memory_v2(self, *, expected_revision):
        """Explicit synthetic inheritance on the SAME backend. Never a live migration."""
        if self.uncertain: fail('v3_UNKNOWN_read_only')
        before, raw, tag = self.read()
        if (before.get('schema') != OLD_SCHEMA or self.parent_digest != digest(before)
                or type(expected_revision) is not int or before['revision'] != expected_revision
                or LINEAGE in before['operations']): fail('v3_inheritance_requires_pinned_v2')
        after = deepcopy(before); after.update(schema=SCHEMA, revision=before['revision']+1)
        after['operations'][LINEAGE] = dict(schema='synthetic-v2-inheritance-v1', parent_revision=before['revision'],
            parent_digest=self.parent_digest, observation=token(raw, tag))
        self.validate(after)
        return self._replace_exact(before, raw, tag, after)

    def _commit(self, before, raw, tag, after, expected_revision):
        if self.uncertain: fail('v3_UNKNOWN_read_only')
        if (before.get('schema') != SCHEMA or type(expected_revision) is not int
                or before['revision'] != expected_revision or load(raw) != before): fail('v3_revision_UNKNOWN')
        self.validate(before); self.validate(after); token(raw, tag)
        if before['legacy'] != after['legacy'] or before['operations'][LINEAGE] != after['operations'][LINEAGE]: fail('v3_immutable')
        if not set(before['operations']) <= set(after['operations']): fail('v3_fence_removal')
        changed = []
        for op, r in after['operations'].items():
            old = before['operations'].get(op)
            if old == r: continue
            changed.append(op)
            if not new_request(r): fail('v3_old_history_read_only')
            if old is None:
                if r['phase'] != 'qualified': fail('v3_initial_phase')
            else:
                if {'qualified': 'pending', 'pending': 'writer_started'}.get(old['phase']) != r['phase']: fail('v3_transition')
                for field in set(old)-{'phase'}:
                    if old[field] is not None and old[field] != r[field]: fail('v3_evidence_immutable')
        if len(changed) != 1: fail('v3_exact_operation')
        return self._replace_exact(before, raw, tag, after)


def mutation(method):
    @wraps(method)
    def guarded(self, *args, **kwargs):
        if self.uncertain or self.store.uncertain or not self.fresh_process: fail('v3_UNKNOWN_read_only')
        try:
            return method(self, *args, **kwargs)
        except Exception:
            self.uncertain = True  # No write to record uncertainty, no recovery API.
            self._qualification_ticket = self._pending_ticket = self._start_ticket = None
            raise
    return guarded


class IsolatedDraftContract(IsolatedConfirmationContract):
    def __init__(self, store, shared, auth, readers, *, clock, enabled=False, isolated_synthetic=False,
                 fresh_process=False, checkpoint=None):
        if (enabled is not True or isolated_synthetic is not True or type(store) is not IsolatedDraftStore
                or type(shared) is not ConditionalRequests or type(auth) is not PurposeBoundary or type(readers) is not DraftReaders):
            fail('v3_isolation_required')
        memory_only(shared.io)
        self.store, self.shared, self.auth, self.readers = store, shared, auth, readers
        self.clock, self.checkpoint, self.fresh_process = clock, checkpoint, fresh_process is True
        self.uncertain = False
        self._native_observation = None
        self._qualification_ticket = self._pending_ticket = self._start_ticket = None
        self._claim_ticket = None
        self._shared_observation = None

    def _begin(self, c, expected_revision):
        validate_shape(c)
        v, raw, tag = self.store.read()
        if v.get('schema') != SCHEMA or type(expected_revision) is not int or v['revision'] != expected_revision:
            fail('v3_revision_UNKNOWN')
        if self._native_observation is not None and token(raw, tag) != self._native_observation:
            fail('v3_native_observation_UNKNOWN')
        return v, raw, tag, deepcopy(v)

    def _fresh(self, c, *, eligibility):
        v, raw, tag = self.store.read()
        validate_draft_request_v2(c, v['legacy'], self.readers, check_ledger=eligibility)
        if self.store.read() != (v, raw, tag): fail('v3_native_double_read')

    def _shared(self, c, *, executing=False):
        v, raw, tag = self.shared.read()
        claim = super()._shared(c, executing=executing)
        if executing and v['operations'][c['operation']]['state'] != 'executing': fail('v3_shared_executing_required')
        if self._shared_observation is not None and token(raw, tag) != self._shared_observation:
            fail('v3_shared_observation_UNKNOWN')
        if self.shared.read() != (v, raw, tag): fail('v3_shared_double_read')
        return claim

    @mutation
    def accept_fixture(self, c, context, *, expected_revision):
        v, raw, tag, after = self._begin(c, expected_revision)
        self._fresh(c, eligibility=True); p = self._auth(c, context)
        if token(raw, tag) != c['inputs']['baseline']['native_initial_observation']: fail('v3_initial_native_changed')
        for key, r in v['operations'].items():
            if key == LINEAGE: continue
            old = r['request']
            if (old['source']['source_id'] == c['source']['source_id'] or old['request_id'] == c['request_id']
                    or old['shared_request_id'] == c['shared_request_id']): fail('v3_owned_or_replay')
        ledger, observation = observe_ledger(self.readers.ledger)
        if ledger != c['before']: fail('v3_before_changed')
        after['operations'][c['operation']] = dict(request=deepcopy(c), phase='qualified',
            qualification=dict(provider=PROVIDER, attestation=asdict(p), at=self.clock(), before_observation=observation),
            claim=None, consume=None, pending=None, unstarted=None, start=None, completion=None)
        after['revision'] += 1
        self._point('before_qualification')
        self._fresh(c, eligibility=True); self._auth(c, context)
        if observe_ledger(self.readers.ledger) != (ledger, observation): fail('v3_observation_changed')
        self._native_observation = self.store._commit(v, raw, tag, after, expected_revision)
        self._point('after_qualification'); self._qualification_ticket = c['fingerprint']

    @mutation
    def claim_fixture(self, c, context, *, expected_shared_revision):
        """Existing shared CAS engine and schema, synthetic fixture claims only."""
        if self._qualification_ticket != c['fingerprint']: fail('v3_claim_ticket')
        self._fresh(c, eligibility=True); self._auth(c, context)
        v, raw, tag = self.shared.read()
        if type(expected_shared_revision) is not int or v['revision'] != expected_shared_revision:
            fail('v3_shared_revision_UNKNOWN')
        for other in v['operations'].values():
            req = self.shared.parse_request(other['request'])
            if (not isinstance(req, AccountingProbeRequest) or req.request_id == c['shared_request_id']
                    or relation(req.ownership, c['ownership']) != 'independent'): fail('v3_shared_owned')
        req = AccountingProbeRequest.parse(dict(schema=SHARED_SCHEMA, synthetic_only=True, owner=c['owner'],
            request_id=c['shared_request_id'], expected_state_revision=expected_shared_revision,
            operation_identity=c['operation'], ownership=c['ownership'], native_preflight_digest=c['fingerprint']))
        after = deepcopy(v); after['revision'] += 1
        after['operations'][c['operation']] = dict(request=asdict(req), fingerprint=req.fingerprint(), state='accepted',
            events=[dict(state='accepted', at=self.clock())])
        self.shared.replace(v, raw, tag, after)
        a, ar, at = self.shared.read()
        if a != after or ar != encoded(after) or at == tag: fail('v3_shared_readback_UNKNOWN')
        self._point('after_shared_accept')
        self._fresh(c, eligibility=True); self._auth(c, context)
        self.shared.transition(req, 'executing', now=self.clock())
        final, fr, ft = self.shared.read()
        expected = deepcopy(after); expected['revision'] += 1
        expected['operations'][c['operation']]['state'] = 'executing'
        expected['operations'][c['operation']]['events'].append(dict(state='executing', at=self.clock()))
        if final != expected or fr != encoded(expected) or ft == at: fail('v3_shared_readback_UNKNOWN')
        self._shared_observation = token(fr, ft)
        self._claim_ticket = self._shared(c, executing=True)

    @mutation
    def consume(self, c, context, *, expected_revision):
        v, raw, tag, after = self._begin(c, expected_revision)
        if self._qualification_ticket != c['fingerprint']: fail('v3_reconstructed_read_only')
        self._fresh(c, eligibility=True); self._auth(c, context)
        claim = self._shared(c, executing=True)
        if self._claim_ticket != claim: fail('v3_unknown_shared_claim')
        r = after['operations'].get(c['operation'])
        if not r or r['request'] != c or r['phase'] != 'qualified': fail('v3_consume_read_only')
        ledger, observation = observe_ledger(self.readers.ledger)
        if ledger != c['before'] or observation != r['qualification']['before_observation']: fail('v3_before_changed')
        r.update(phase='pending', claim=claim, pending=pending_projection(c))
        r['consume'] = dict(authority_revision=c['authority']['revision'], request_id=c['request_id'],
            fingerprint=c['fingerprint'], pending_digest=digest(r['pending']), at=self.clock())
        r['unstarted'] = dict(fingerprint=c['fingerprint'], pending_digest=digest(r['pending']), sequence=0, ledger=observation)
        after['revision'] += 1; self._qualification_ticket = None
        self._point('before_consume'); self._fresh(c, eligibility=True); self._auth(c, context)
        if self._shared(c, executing=True) != claim or observe_ledger(self.readers.ledger) != (ledger, observation): fail('v3_observation_changed')
        self._native_observation = self.store._commit(v, raw, tag, after, expected_revision)
        self._point('after_consume'); self._pending_ticket = c['fingerprint']

    @mutation
    def start(self, c, context, *, expected_revision):
        # Reuse unchanged v2 start sequencing and shared-claim checks, with the
        # explicit v3 _begin/_fresh/store and new pending projection above.
        return super().start(c, context, expected_revision=expected_revision)

    def complete_fixture(self, *args, **kwargs): fail('v3_completion_disabled')
    def dispatch(self, *args, **kwargs): fail('v3_writer_disabled')

    def inspect(self, c, context=None):
        result = dict(classification='UNKNOWN', synthetic_only=True, real_native_complete=False,
            accounting_permission=False, writer_dispatches=0, retry_permission=False)
        try:
            self.auth._read(context)
            if self.auth.owner != c['owner']: fail('v3_read_owner')
            if self.uncertain or self.store.uncertain: return result
            validate_shape(c)
            v, raw, tag = self.store.read()
            if v.get('schema') != SCHEMA: return result
            r = v['operations'].get(c['operation'])
            if not r or r['request'] != c: return result
            self._fresh(c, eligibility=False)
            shared = self.shared.read()
            observed, observation = observe_ledger(self.readers.ledger)
            if r['phase'] != 'qualified' and self._shared(c) == r['claim']:
                full = planned_after(c['before'], c['plan'])
                if (r['phase'] == 'pending' and self._pending_ticket == c['fingerprint'] and observed == c['before']
                        and observation == r['unstarted']['ledger']): result['classification'] = 'NOT_EXECUTED'
                elif observed != c['before'] and observed != full: result['classification'] = 'PARTIAL'
            if self.store.read() != (v, raw, tag) or self.shared.read() != shared or observe_ledger(self.readers.ledger) != (observed, observation):
                result['classification'] = 'UNKNOWN'
        except (StateError, KeyError, ValueError, TypeError, AttributeError, OSError):
            result['classification'] = 'UNKNOWN'
        return result
