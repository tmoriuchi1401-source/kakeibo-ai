"""Isolated native receipt v2 contract. No live factory or writer dispatch.

Default production v1 is unchanged. All mutation paths require explicit
isolation, memory-only IO and trusted synthetic fixtures. No real authority
is issued. ConditionalRequests.replace is the only CAS implementation.
"""
from copy import copy, deepcopy
from dataclasses import asdict
import json
from math import isfinite
from types import SimpleNamespace
from uuid import UUID

from .drive_run_state import StateError
from .iphone_accounting_gate import AccountingProbeRequest
from .iphone_accounting_ownership import source_valid, parse_ownership, hashed, relation
from .pdf_intake_registry import strong_etag
from .receipt_confirmation import ReceiptConfirmation, review_id
from .receipt_reimport_production import ReimportStore, validate_store, digest as native_digest
from .unified_receipt_native import confirmation_operation_identity
from .unified_receipt_purposes import PurposeBoundary, VerifiedPost, ACCOUNTING_POST
from .unified_receipt_review import digest
from .unified_receipt_write import ConditionalRequests, MAX_BYTES, prohibited, OLD_P1_SOURCE, OLD_REFERENCES

SCHEMA = 'receipt-reimport-v2'
FIXTURE = 'synthetic-contract-fixture-not-issued-authority'
PROVIDER = 'isolated-receipt-native-v2-fixture'


def fail(code):
    raise StateError('legacy_contract_' + code)


def load(raw):
    def unique(pairs):
        value = {}
        for k, v in pairs:
            if k in value:
                raise ValueError('duplicate')
            value[k] = v
        return value
    if type(raw) is not bytes or len(raw) > MAX_BYTES:
        fail('invalid_bytes')
    def invalid_constant(_):
        raise ValueError('non-finite')
    return json.loads(raw, object_pairs_hook=unique, parse_constant=invalid_constant)


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':')).encode()


def memory_only(io):
    if getattr(io, 'synthetic_memory_only', False) is not True:
        fail('memory_required')


def snapshot(value):
    if (type(value) is not list or len(value) != 2 or type(value[0]) is not dict
            or set(value[0]) != set(value[1]) or not {'レシート', '支出明細', '取込データ'} <= set(value[0])):
        fail('ledger_incomplete')
    for part in value:
        for rows in part.values():
            if type(rows) is not list or any(type(r) is not list or not r for r in rows):
                fail('ledger_incomplete')
            if any(not isinstance(r[0], str) or not r[0] or any(
                    type(cell) not in (str, int, float, bool, type(None))
                    or type(cell) is float and not isfinite(cell) for cell in r) for r in rows):
                fail('ledger_cell_invalid')
            ids = [str(r[0]) for r in rows]
            if len(ids) != len(set(ids)):
                fail('ledger_duplicate')
    for title in value[0]:
        if [r[0] for r in value[0][title]] != [r[0] for r in value[1][title]]:
            fail('ledger_projection_identity')
    return deepcopy(value)


def planned_after(before, plan):
    result = snapshot(before)
    keys = set()
    for title, wanted in plan:
        if title not in {'レシート', '支出明細', '取込データ'} or (title, wanted[0]) in keys:
            fail('plan_invalid')
        keys.add((title, wanted[0]))
        for part in result:
            indices = [i for i, r in enumerate(part[title]) if r[0] == wanted[0]]
            if indices:
                part[title][indices[0]] = deepcopy(wanted)
            else:
                part[title].append(deepcopy(wanted))
    return result


def validate_candidate(c):
    try:
        if set(c) != {'mode', 'kind', 'case_id', 'review_id', 'operation', 'request_id', 'shared_request_id',
                      'owner', 'source', 'inputs', 'plan', 'before', 'authority', 'ownership', 'fingerprint'}:
            raise ValueError()
        if c['mode'] != FIXTURE or c['kind'] not in {'normal', 'medical'}:
            raise ValueError()
        for key in ('request_id', 'shared_request_id'):
            if str(UUID(c[key])) != c[key]:
                raise ValueError()
        source_valid(c['source'])
        if c['operation'] != confirmation_operation_identity({'source': dict(
                source_id=c['source']['source_id'], sha256=c['source']['sha256'],
                version=c['source']['native_source_version'])}):
            raise ValueError()
        a = c['authority']
        if (set(a) != {'mode', 'actor', 'owner', 'purpose', 'request_id', 'input_fingerprint', 'revision',
                      'issued_at', 'expires_at', 'revoked', 'authenticated_action_digest', 'scope_digest'}
                or a['mode'] != FIXTURE or a['actor'] != a['owner'] or a['owner'] != c['owner']
                or a['purpose'] != ACCOUNTING_POST or a['request_id'] != c['request_id']
                or a['input_fingerprint'] != digest(c['inputs']) or a['scope_digest'] != digest([
                    c['operation'], c['source'], c['plan'], c['before']])
                or type(a['revision']) is not int or a['revision'] < 1
                or type(a['issued_at']) is not int or a['expires_at'] - a['issued_at'] != 600
                or a['revoked'] is not False or len(a['authenticated_action_digest']) != 64):
            raise ValueError()
        o = c['ownership']
        if parse_ownership(o).scope != 'whole':
            raise ValueError()
        if (o['operation_identity'] != c['operation'] or o['source'] != c['source']
                or o['owner'] != c['owner'] or o['input_snapshot'] != digest(c['inputs'])
                or o['authority_revision'] != a['revision']):
            raise ValueError()
        if c['fingerprint'] != digest({k: v for k, v in c.items() if k != 'fingerprint'}):
            raise ValueError()
        planned_after(c['before'], c['plan'])
        return deepcopy(c)
    except (KeyError, TypeError, ValueError, AttributeError):
        fail('candidate_invalid')


def validate_request(c):
    """Strict native v2 subset; old PoC's weaker reader is not authority."""
    validate_candidate(c)
    try:
        i, a = c['inputs'], c['authority']
        if set(i) != {'old', 'confirmed', 'parsed', 'categories'}:
            raise ValueError()
        old = i['old']
        if (old['kind'] != c['kind'] or old['status'] != 'waiting'
                or old.get('require_reconfirm') or old.get('decision_origin') == 'automatic'
                or c['review_id'] != review_id(old['kind'], old['source'])
                or old['inputs'] != i['confirmed'] or len(i['confirmed']) != 8
                or old['source']['source_id'] != c['source']['source_id']
                or old['source']['sha256'] != c['source']['sha256']
                or old['source']['version'] != c['source']['native_source_version']
                or c['ownership']['case_id'] != c['case_id']
                or c['case_id'] in {'G103', 'H301', 'SYN-G103', 'SYN-H301'}
                or c['source']['source_id'] == OLD_P1_SOURCE or c['review_id'] in OLD_REFERENCES
                or not hashed(a['authenticated_action_digest']) or not hashed(a['scope_digest'])
                or type(a['expires_at']) is not int or type(a['revoked']) is not bool):
            raise ValueError()
        from .models import ReceiptResult
        from .receipt_validation import validate_receipt_result
        parsed = ReceiptResult.model_validate(i['parsed'])
        if not validate_receipt_result(parsed, [tuple(p) for p in i['categories']])[0]:
            raise ValueError()
        if c['kind'] == 'medical':
            shadow = object.__new__(ReceiptConfirmation)
            shadow.db = SimpleNamespace(categories=lambda: [tuple(p) for p in i['categories']])
            if shadow._parsed(old).model_dump() != i['parsed']:
                raise ValueError()
        if i['confirmed'][5] not in ({'候補明細で確定', '重複候補と別の支出として確定'}
                if c['kind'] == 'normal' else {'医療費を確定', '重複候補と別の支出として確定'}):
            raise ValueError()
        ids = [row[0] for _, row in c['plan']]
        if ids != c['ownership']['ledger_targets'] or len(ids) != len(set(ids)):
            raise ValueError()
        if ([t for t, _ in c['plan']][0] != 'レシート' or c['plan'][-1][0] != '取込データ'
                or c['plan'][0][1][0] != 'R-'+c['source']['source_id']
                or c['plan'][-1][1][0] != 'receipt:'+c['source']['source_id']):
            raise ValueError()
        return deepcopy(c)
    except (KeyError, ValueError, TypeError, AttributeError):
        fail('v2_request_invalid')


def pending_item(c):
    item = deepcopy(c['inputs']['old'])
    item.update(status='pending', plan=deepcopy(c['plan']), confirmation_hash=native_digest(item['inputs']))
    if item['kind'] == 'medical':
        item['decision_origin'] = 'human'
    return item


def token(raw, tag):
    if type(raw) is not bytes or not strong_etag(tag):
        fail('observation_UNKNOWN')
    return dict(raw_digest=digest(raw.hex()), tag=tag)


def validate_token(t):
    if set(t) != {'raw_digest', 'tag'} or not hashed(t['raw_digest']) or not strong_etag(t['tag']):
        fail('observation_UNKNOWN')


def observe_ledger(io):
    memory_only(io)
    raw, tag = io.read_versioned()
    value = load(raw)
    if (set(value) != {'complete', 'snapshot'} or value['complete'] is not True
            or io.read_versioned() != (raw, tag)):
        fail('ledger_UNKNOWN')
    return snapshot(value['snapshot']), token(raw, tag)


def verify_attestation(p, c, at):
    if not isinstance(p, VerifiedPost):
        fail('attestation_required')
    a = c['authority']
    auth = PurposeBoundary(c['owner'], lambda *args: p, lambda *args: None, lambda: [], clock=lambda: at)
    auth._post(None, ACCOUNTING_POST, c['request_id'], digest(c['inputs']), p.expected_revision)
    if (digest(asdict(p)) != a['authenticated_action_digest'] or p.created_at != a['issued_at']
            or p.expires_at != a['expires_at'] or p.expected_revision != 0):
        fail('attestation_binding')


def validate_operation(op, r):
    if set(r) != {'request', 'phase', 'qualification', 'claim', 'consume', 'pending', 'unstarted', 'start', 'completion'}:
        fail('v2_operation_invalid')
    c = validate_request(r['request'])
    if op != c['operation'] or r['phase'] not in {'qualified', 'pending', 'writer_started', 'complete_fixture'}:
        fail('v2_operation_invalid')
    q = r['qualification']
    if set(q) != {'provider', 'attestation', 'at', 'before_observation'} or q['provider'] != PROVIDER:
        fail('v2_qualification_invalid')
    validate_token(q['before_observation'])
    verify_attestation(VerifiedPost(**q['attestation']), c, q['at'])
    if r['phase'] == 'qualified':
        if any(r[k] is not None for k in ('claim', 'consume', 'pending', 'unstarted', 'start', 'completion')):
            fail('v2_qualification_invalid')
        return c
    claim = r['claim']
    if (set(claim) != {'operation', 'request_id', 'fingerprint', 'accepted_at'}
            or claim['operation'] != op or claim['request_id'] != c['shared_request_id']
            or not hashed(claim['fingerprint']) or type(claim['accepted_at']) is not int):
        fail('v2_claim_invalid')
    if r['pending'] != pending_item(c):
        fail('v2_pending_invalid')
    consumed = r['consume']
    if (set(consumed) != {'authority_revision', 'request_id', 'fingerprint', 'pending_digest', 'at'}
            or consumed != dict(authority_revision=c['authority']['revision'], request_id=c['request_id'],
                fingerprint=c['fingerprint'], pending_digest=digest(r['pending']), at=consumed['at'])
            or type(consumed['at']) is not int or type(consumed['authority_revision']) is not int
            or not c['authority']['issued_at'] <= consumed['at'] < c['authority']['expires_at']):
        fail('v2_consume_invalid')
    u = r['unstarted']
    if (set(u) != {'fingerprint', 'pending_digest', 'sequence', 'ledger'} or type(u['sequence']) is not int or u['sequence'] != 0
            or u['fingerprint'] != c['fingerprint'] or u['pending_digest'] != digest(r['pending'])):
        fail('v2_unstarted_invalid')
    validate_token(u['ledger'])
    if u['ledger'] != q['before_observation']:
        fail('v2_before_observation_changed')
    if r['phase'] == 'pending':
        if r['start'] is not None or r['completion'] is not None:
            fail('v2_start_invalid')
        return c
    start = r['start']
    if (set(start) != {'fingerprint', 'claim_digest', 'consume_digest', 'pending_digest', 'sequence', 'at'}
            or start != dict(fingerprint=c['fingerprint'], claim_digest=digest(claim), consume_digest=digest(consumed),
                pending_digest=digest(r['pending']), sequence=1, at=start['at'])
            or type(start['sequence']) is not int or type(start['at']) is not int
            or not consumed['at'] <= start['at'] < c['authority']['expires_at']):
        fail('v2_start_invalid')
    if r['phase'] == 'writer_started':
        if r['completion'] is not None:
            fail('v2_completion_invalid')
        return c
    done = r['completion']
    if (set(done) != {'mode', 'fingerprint', 'start_digest', 'pending_digest', 'snapshot_digest', 'observation', 'at'}
            or done['mode'] != PROVIDER or done['fingerprint'] != c['fingerprint']
            or done['start_digest'] != digest(start) or done['pending_digest'] != digest(r['pending'])
            or done['snapshot_digest'] != digest(planned_after(c['before'], c['plan']))
            or type(done['at']) is not int or done['at'] < start['at']):
        fail('v2_completion_invalid')
    validate_token(done['observation'])
    return c


class IsolatedReimportStore(ReimportStore, ConditionalRequests):
    """Opt-in v2/native facade using the existing CAS engine; v1 is read-only."""
    def __init__(self, io, expected_digest, spreadsheet_id, *, enabled=False, isolated_synthetic=False):
        if enabled is not True or isolated_synthetic is not True:
            fail('v2_isolation_required')
        memory_only(io)
        ConditionalRequests.__init__(self, io, 'isolated-native-reimport-v2')
        self.expected_digest, self.spreadsheet_id = expected_digest, spreadsheet_id
        self.uncertain = False

    def validate(self, v):
        if v.get('schema') == 'receipt-reimport-v1':
            validate_store(v, self.expected_digest, self.spreadsheet_id)
            return v
        if (set(v) != {'schema', 'revision', 'legacy', 'operations'} or v['schema'] != SCHEMA
                or type(v['revision']) is not int or v['revision'] < 0 or type(v['operations']) is not dict):
            fail('v2_schema_UNKNOWN')
        validate_store(v['legacy'], self.expected_digest, self.spreadsheet_id)
        ids, shared_ids, sources = set(), set(), set()
        for op, r in v['operations'].items():
            c = validate_operation(op, r)
            if (c['request_id'] in ids or c['shared_request_id'] in shared_ids
                    or c['source']['source_id'] in sources):
                fail('v2_duplicate_request_or_source')
            ids.add(c['request_id']); shared_ids.add(c['shared_request_id']); sources.add(c['source']['source_id'])
            old = v['legacy'].get('confirmation_items', {}).get(c['review_id'])
            if digest(old) != digest(c['inputs']['old']) or old['status'] != 'waiting':
                fail('v2_legacy_binding')
        return v

    def read(self):
        try:
            raw, tag = self.io.read_versioned()
            token(raw, tag)
            v = self.validate(load(raw))
            if self.io.read_versioned() != (raw, tag):
                fail('v2_read_changed')
            return v, raw, tag
        except (ValueError, TypeError, KeyError, AttributeError) as error:
            raise StateError('legacy_contract_v2_read_UNKNOWN') from error

    @property
    def value(self):
        v = self.read()[0]
        return deepcopy(v['legacy'] if v.get('schema') == SCHEMA else v)

    def save(self, value):
        fail('v2_explicit_native_transition_required')

    def replace(self, *args):
        fail('v2_explicit_native_transition_required')

    def _commit(self, before, raw, tag, after, expected_revision):
        if self.uncertain:
            fail('v2_UNKNOWN_read_only')
        if before.get('schema') != SCHEMA:
            fail('v1_read_only_no_migration')
        if type(expected_revision) is not int or before['revision'] != expected_revision:
            fail('v2_expected_revision')
        token(raw, tag)
        if digest(self.validate(load(raw))) != digest(before):
            fail('v2_before_bytes_binding')
        if digest(before['legacy']) != digest(after['legacy']):
            fail('v2_legacy_immutable')
        self.validate(after)
        if not set(before['operations']) <= set(after['operations']):
            fail('v2_fence_removal_forbidden')
        allowed = {'qualified': 'pending', 'pending': 'writer_started', 'writer_started': 'complete_fixture'}
        changed = 0
        for key, new in after['operations'].items():
            old = before['operations'].get(key)
            if old == new:
                continue
            changed += 1
            if old is None:
                if new['phase'] != 'qualified': fail('v2_initial_phase')
                continue
            if allowed.get(old['phase']) != new['phase']:
                fail('v2_transition_forbidden')
            for field in ('request', 'qualification', 'claim', 'consume', 'pending', 'unstarted', 'start', 'completion'):
                if old[field] is not None and old[field] != new[field]:
                    fail('v2_consumed_evidence_immutable')
        if changed != 1:
            fail('v2_exact_operation_required')
        try:
            ConditionalRequests.replace(self, before, raw, tag, after)
            actual, actual_raw, actual_tag = self.read()
            if (actual != after or actual_tag == tag or not strong_etag(actual_tag)
                    or actual_raw != json.dumps(after, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode()):
                fail('v2_full_readback_unknown')
            return token(actual_raw, actual_tag)
        except Exception:
            self.uncertain = True  # No follow-up write to record uncertainty.
            fail('v2_CAS_UNKNOWN_read_only')


class IsolatedConfirmationContract:
    """Trusted memory composition; qualification, consume and evidence only."""
    def __init__(self, store, shared, auth, readers, *, clock, enabled=False, isolated_synthetic=False, checkpoint=None):
        if (enabled is not True or isolated_synthetic is not True or type(store) is not IsolatedReimportStore
                or type(shared) is not ConditionalRequests or not shared.synthetic_accounting
                or not isinstance(auth, PurposeBoundary) or getattr(readers, 'synthetic_memory_only', False) is not True):
            fail('v2_isolation_required')
        memory_only(shared.io)
        self.store, self.shared, self.auth, self.readers = store, shared, auth, readers
        memory_only(readers.ledger)
        self.clock, self.checkpoint = clock, checkpoint
        self._qualification_ticket = self._pending_ticket = self._start_ticket = None

    def _point(self, name):
        if self.checkpoint is not None:
            self.checkpoint(name)

    def _auth(self, c, context):
        if self.auth.owner != c['owner']:
            fail('v2_owner')
        self.auth._post(context, ACCOUNTING_POST, c['request_id'], digest(c['inputs']), 0)
        p = self.auth.verify_post(context, ACCOUNTING_POST, c['request_id'], digest(c['inputs']), 0)
        verify_attestation(p, c, self.clock())
        return p

    def _fresh(self, c, *, eligibility):
        current = self.readers.current()
        if digest(current) != digest(dict(source=c['source'], inputs=c['inputs'], authority=c['authority'])):
            fail('v2_current_changed')
        if self.readers.ownership().value() != c['ownership']:
            fail('v2_ownership_changed')
        if eligibility:
            service, port, case = self.readers.native()
            if prohibited(case) or case.legacy_id in {'G103', 'H301', 'SYN-G103', 'SYN-H301'}:
                fail('v2_held')
            if case.case_id != c['case_id'] or digest(service.items[c['review_id']]) != digest(c['inputs']['old']):
                fail('v2_native_changed')
            port.validate_draft(case, case)
            parsed = service._parsed(service.items[c['review_id']])
            # Native dry-run can save an error/reconfirmation item. Use its real
            # validator on a read-only store copy so v1 remains byte-for-byte idle.
            shadow = copy(service)
            def no_save(_): fail('v2_native_read_only_validation')
            shadow.store = SimpleNamespace(value=deepcopy(service.store.value), save=no_save)
            if (parsed.model_dump() != c['inputs']['parsed'] or [list(p) for p in service.db.categories()] != c['inputs']['categories']
                    or shadow.apply_confirmations(dry_run=True, keys=[c['review_id']]) != 1):
                fail('v2_native_validator')
            # Plan timestamps are frozen in the trusted snapshot. Compare the
            # native plan excluding only fresh timestamps for newly created rows.
            plan = service._plan(service.items[c['review_id']], parsed, '',
                c['inputs']['confirmed'][5] == '重複候補と別の支出として確定')
            wanted = deepcopy(c['plan'])
            for actual, expected in zip(plan, wanted):
                if any(row[0] == expected[1][0] for row in c['before'][0][actual[0]]):
                    continue
                if actual[0] == 'レシート': actual[1][7] = expected[1][7]
                if actual[0] == '取込データ': actual[1][1] = expected[1][1]
            if plan != wanted:
                fail('v2_plan_changed')
        return current

    def _shared(self, c, *, executing=False):
        v, _, _ = self.shared.read()
        r = v['operations'].get(c['operation'])
        allowed = {'accepted', 'executing'} if executing else {'accepted', 'executing', 'awaiting_readback', 'complete'}
        if not r or r['state'] not in allowed:
            fail('v2_shared_claim')
        req = self.shared.parse_request(r['request'])
        if (not isinstance(req, AccountingProbeRequest) or req.owner != c['owner']
                or req.ownership != c['ownership'] or req.request_id != c['shared_request_id']
                or req.native_preflight_digest != c['fingerprint'] or r['events'][0]['state'] != 'accepted'):
            fail('v2_shared_binding')
        # Check every other financial scope using the same #133 ownership fence.
        for op, other in v['operations'].items():
            if op == c['operation']: continue
            previous = self.shared.parse_request(other['request'])
            if getattr(previous, 'purpose', ACCOUNTING_POST) != ACCOUNTING_POST:
                continue
            if not isinstance(previous, AccountingProbeRequest) or relation(previous.ownership, c['ownership']) != 'independent':
                fail('v2_shared_scope_conflict')
        return dict(operation=c['operation'], request_id=req.request_id, fingerprint=r['fingerprint'], accepted_at=r['events'][0]['at'])

    def _begin(self, c, expected_revision):
        if self.store.uncertain:
            fail('v2_UNKNOWN_read_only')
        validate_request(c)
        v, raw, tag = self.store.read()
        if v.get('schema') != SCHEMA:
            fail('v1_read_only_no_migration')
        if type(expected_revision) is not int or v['revision'] != expected_revision:
            fail('v2_expected_revision')
        return v, raw, tag, deepcopy(v)

    def accept_fixture(self, c, context, *, expected_revision):
        """Validate issuance QUALIFICATIONS; accept a fixture, never mint a grant."""
        v, raw, tag, after = self._begin(c, expected_revision)
        self._fresh(c, eligibility=True)
        p = self._auth(c, context)
        ledger, observation = observe_ledger(self.readers.ledger)
        if digest(ledger) != digest(c['before']) or observation != self.readers.before_observation:
            fail('v2_before_changed')
        old_items = v['legacy'].get('confirmation_items', {})
        if old_items.get(c['review_id']) != c['inputs']['old'] or any(
            x.get('source', {}).get('source_id') == c['source']['source_id']
            and x.get('status') in {'pending', 'applied'} for x in old_items.values()):
            fail('v2_legacy_pending_or_applied')
        for r in v['operations'].values():
            old = r['request']
            if (old['request_id'] == c['request_id'] or old['source']['source_id'] == c['source']['source_id']
                    or old['shared_request_id'] == c['shared_request_id']):
                fail('v2_request_or_source_owned')
        after['operations'][c['operation']] = dict(request=deepcopy(c), phase='qualified',
            qualification=dict(provider=PROVIDER, attestation=asdict(p), at=self.clock(), before_observation=observation),
            claim=None, consume=None, pending=None, unstarted=None, start=None, completion=None)
        after['revision'] += 1
        self._point('before_qualification')
        self._fresh(c, eligibility=True); self._auth(c, context)
        if observe_ledger(self.readers.ledger) != (ledger, observation):
            fail('v2_observation_changed')
        self.store._commit(v, raw, tag, after, expected_revision)
        self._point('after_qualification')
        self._qualification_ticket = c['fingerprint']

    def consume(self, c, context, *, expected_revision):
        v, raw, tag, after = self._begin(c, expected_revision)
        if self._qualification_ticket != c['fingerprint']:
            fail('v2_reconstructed_read_only')
        self._fresh(c, eligibility=True); self._auth(c, context)
        claim = self._shared(c, executing=True)
        r = after['operations'].get(c['operation'])
        if not r or r['request'] != c or r['phase'] != 'qualified':
            fail('v2_consume_read_only')
        ledger, observation = observe_ledger(self.readers.ledger)
        if digest(ledger) != digest(c['before']) or observation != r['qualification']['before_observation']:
            fail('v2_before_changed')
        r.update(phase='pending', claim=claim, pending=pending_item(c))
        r['consume'] = dict(authority_revision=c['authority']['revision'], request_id=c['request_id'],
            fingerprint=c['fingerprint'], pending_digest=digest(r['pending']), at=self.clock())
        r['unstarted'] = dict(fingerprint=c['fingerprint'], pending_digest=digest(r['pending']), sequence=0, ledger=observation)
        after['revision'] += 1
        self._qualification_ticket = None
        self._point('before_consume')
        self._fresh(c, eligibility=True); self._auth(c, context)
        if self._shared(c, executing=True) != claim or observe_ledger(self.readers.ledger) != (ledger, observation):
            fail('v2_observation_changed')
        self.store._commit(v, raw, tag, after, expected_revision)
        self._point('after_consume')
        self._pending_ticket = c['fingerprint']

    def start(self, c, context, *, expected_revision):
        v, raw, tag, after = self._begin(c, expected_revision)
        if self._pending_ticket != c['fingerprint']:
            fail('v2_reconstructed_read_only')
        self._fresh(c, eligibility=True); self._auth(c, context)
        claim = self._shared(c, executing=True)
        r = after['operations'].get(c['operation'])
        if not r or r['request'] != c or r['phase'] != 'pending' or r['claim'] != claim:
            fail('v2_start_read_only')
        ledger, observation = observe_ledger(self.readers.ledger)
        if digest(ledger) != digest(c['before']) or observation != r['unstarted']['ledger']:
            fail('v2_before_changed')
        r['start'] = dict(fingerprint=c['fingerprint'], claim_digest=digest(claim), consume_digest=digest(r['consume']),
            pending_digest=digest(r['pending']), sequence=1, at=self.clock())
        r['phase'] = 'writer_started'; after['revision'] += 1
        self._pending_ticket = None
        self._point('before_start')
        self._fresh(c, eligibility=True); self._auth(c, context)
        if self._shared(c, executing=True) != claim or observe_ledger(self.readers.ledger) != (ledger, observation):
            fail('v2_observation_changed')
        self.store._commit(v, raw, tag, after, expected_revision)
        self._point('after_start')
        self._start_ticket = c['fingerprint']
        return dict(synthetic_only=True, writer_dispatches=0, accounting_permission=False)

    def complete_fixture(self, c, certificate, *, expected_revision):
        v, raw, tag, after = self._begin(c, expected_revision)
        if self._start_ticket != c['fingerprint']:
            fail('v2_reconstructed_read_only')
        self._fresh(c, eligibility=False)
        r = after['operations'].get(c['operation'])
        if not r or r['request'] != c or r['phase'] != 'writer_started' or self._shared(c) != r['claim']:
            fail('v2_completion_read_only')
        observed, observation = observe_ledger(self.readers.ledger)
        if digest(observed) != digest(planned_after(c['before'], c['plan'])):
            fail('v2_completion_ledger')
        expected = dict(mode=PROVIDER, fingerprint=c['fingerprint'], start_digest=digest(r['start']),
            pending_digest=digest(r['pending']), snapshot_digest=digest(observed), observation=observation, at=self.clock())
        if certificate != expected:
            fail('v2_completion_fixture_required')
        r.update(phase='complete_fixture', completion=deepcopy(certificate)); after['revision'] += 1
        self._start_ticket = None
        self._point('before_completion')
        self._fresh(c, eligibility=False)
        if self._shared(c) != r['claim'] or observe_ledger(self.readers.ledger) != (observed, observation):
            fail('v2_observation_changed')
        self.store._commit(v, raw, tag, after, expected_revision)
        self._point('after_completion')

    def inspect(self, c):
        result = dict(classification='UNKNOWN', synthetic_only=True, real_native_complete=False,
            accounting_permission=False, writer_dispatches=0)
        if self.store.uncertain:
            return result
        try:
            validate_request(c)
            v, raw, tag = self.store.read()
            shared_before = self.shared.read()
            if v.get('schema') != SCHEMA:
                return result
            r = v['operations'].get(c['operation'])
            if not r or r['request'] != c:
                return result
            self._fresh(c, eligibility=False)
            observed, observation = observe_ledger(self.readers.ledger)
            if r['phase'] == 'qualified':
                # No native consume/start exists. Never a retry authorization.
                return result
            if self._shared(c) != r['claim']:
                return result
            full = planned_after(c['before'], c['plan'])
            if (r['phase'] == 'pending' and self._pending_ticket == c['fingerprint']
                    and r['start'] is None and digest(observed) == digest(c['before'])
                    and observation == r['unstarted']['ledger']):
                result['classification'] = 'NOT_EXECUTED'
            elif (digest(observed) != digest(full) and digest(observed) != digest(c['before'])):
                result['classification'] = 'PARTIAL'
            elif (r['phase'] == 'complete_fixture' and digest(observed) == digest(full)
                    and r['completion']['observation'] == observation):
                class View:
                    def get(self, region):
                        return deepcopy(observed[0][region.split('!')[0].strip("'")])
                shadow = object.__new__(ReceiptConfirmation); shadow.db = View()
                if shadow._complete(c['plan']):
                    result['classification'] = 'COMPLETE'
            # A reconstructed/uncertain pending or any start remains UNKNOWN.
            if (self.store.read() != (v, raw, tag) or self.shared.read() != shared_before
                    or observe_ledger(self.readers.ledger) != (observed, observation)):
                result['classification'] = 'UNKNOWN'
            self._fresh(c, eligibility=False)
        except (StateError, KeyError, ValueError, TypeError, AttributeError):
            result['classification'] = 'UNKNOWN'
        return result

    def dispatch(self, *args, **kwargs):
        fail('v2_writer_disabled')


