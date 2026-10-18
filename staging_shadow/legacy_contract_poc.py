"""2C-1 contract experiment. No authority issuer, writer, runner or live client.

Only explicitly marked memory IO is accepted. Synthetic fixture certificates
are never NativeInspection or financial permissions. The CAS implementation
is the unchanged ConditionalRequests.replace, over a proposed native envelope.
"""
from copy import copy, deepcopy
from dataclasses import asdict
import json
from uuid import UUID

from app.drive_run_state import StateError
from app.iphone_accounting_gate import AccountingProbeRequest
from app.iphone_accounting_ownership import source_valid, parse_ownership
from app.pdf_intake_registry import strong_etag
from app.receipt_confirmation import ReceiptConfirmation
from app.receipt_reimport_production import validate_store
from app.unified_receipt_native import confirmation_operation_identity
from app.unified_receipt_purposes import ACCOUNTING_POST, PurposeBoundary
from app.unified_receipt_review import digest
from app.unified_receipt_write import ConditionalRequests, MAX_BYTES, prohibited

SCHEMA = 'synthetic-legacy-native-contract-v1'
FIXTURE = 'synthetic-contract-fixture-not-issued-authority'
PHASES = {'claimed', 'writer_started', 'complete_fixture'}


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
    return json.loads(raw, object_pairs_hook=unique)


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


def freeze_candidate(service, port, case, source, authority, ownership, before, native_id, shared_id):
    """Validate fixtures with real native code; never manufacture authority."""
    if prohibited(case) or case.legacy_id in {'G103', 'H301', 'SYN-G103', 'SYN-H301'}:
        fail('held')
    item = service.items[port.review_id]
    if item['status'] != 'waiting' or item.get('decision_origin') == 'automatic':
        fail('legacy_pending_or_applied')
    port.validate_draft(case, case)
    if service.apply_confirmations(dry_run=True, keys=[port.review_id]) != 1:
        fail('native_validation')
    parsed = service._parsed(item)
    plan = service._plan(item, parsed, '', item['inputs'][5] == '重複候補と別の支出として確定')
    if (source['source_id'], source['sha256'], source['native_source_version']) != (
            item['source']['source_id'], item['source']['sha256'], item['source']['version']):
        fail('source_binding')
    metadata = service.verify_source(item['source'], item['folder_id'])
    if metadata.get('version') != source['drive_numeric_version']:
        fail('drive_observation_binding')
    if before[0] != service.db.rows:  # Narrow existing test DB, no production DB handle.
        fail('before_binding')
    c = dict(mode=FIXTURE, kind=item['kind'], case_id=case.case_id, review_id=port.review_id,
             operation=confirmation_operation_identity(item), request_id=native_id, shared_request_id=shared_id,
             owner=ownership['owner'], source=deepcopy(source), inputs=dict(old=deepcopy(item),
             confirmed=deepcopy(item['inputs']), parsed=parsed.model_dump(),
             categories=[list(p) for p in service.db.categories()]), plan=plan,
             before=deepcopy(before), authority=deepcopy(authority), ownership=deepcopy(ownership))
    c['fingerprint'] = digest(c)
    return validate_candidate(c)


class NativeContractPoC(ConditionalRequests):
    """CAS envelope, preserving the v1 payload. No unconditional save fallback."""
    def __init__(self, io, manifest_digest, spreadsheet_id, *, enabled=False):
        memory_only(io)
        if enabled is not True:
            fail('disabled')
        super().__init__(io, 'synthetic-legacy-native-contract')
        self.manifest_digest, self.spreadsheet_id = manifest_digest, spreadsheet_id

    def read(self):
        raw, tag = self.io.read_versioned()
        try:
            v = load(raw)
            if (not strong_etag(tag) or set(v) != {'schema', 'revision', 'legacy', 'operations'}
                    or v['schema'] != SCHEMA or type(v['revision']) is not int or v['revision'] < 0
                    or type(v['operations']) is not dict):
                raise ValueError()
            validate_store(v['legacy'], self.manifest_digest, self.spreadsheet_id)
            ids = set()
            for op, r in v['operations'].items():
                c = validate_candidate(r['candidate'])
                if (set(r) != {'candidate', 'phase', 'claim', 'unstarted', 'start', 'completion'}
                        or c['operation'] != op or r['phase'] not in PHASES or c['request_id'] in ids
                        or set(r['unstarted']) != {'candidate', 'sequence', 'ledger'}
                        or r['unstarted']['candidate'] != c['fingerprint'] or r['unstarted']['sequence'] != 0
                        or r['claim']['request_id'] != c['shared_request_id']
                        or r['claim']['operation'] != op):
                    raise ValueError()
                ids.add(c['request_id'])
                expected_start = dict(candidate=c['fingerprint'], claim=digest(r['claim']), sequence=1)
                if ((r['phase'] == 'claimed' and (r['start'] is not None or r['completion'] is not None))
                        or r['phase'] != 'claimed' and r['start'] != expected_start):
                    raise ValueError()
                if r['phase'] == 'writer_started' and r['completion'] is not None:
                    raise ValueError()
                for token in [r['unstarted']['ledger']] + ([r['completion']['observation']]
                        if r['phase'] == 'complete_fixture' else []):
                    if (set(token) != {'tag', 'raw_digest'} or not strong_etag(token['tag'])
                            or len(token['raw_digest']) != 64):
                        raise ValueError()
                if r['phase'] == 'complete_fixture':
                    proof = r['completion']
                    if (set(proof) != {'mode', 'candidate', 'start', 'ledger', 'observation'}
                            or proof['mode'] != FIXTURE or proof['candidate'] != c['fingerprint']
                            or proof['start'] != digest(expected_start)
                            or proof['ledger'] != digest(planned_after(c['before'], c['plan']))):
                        raise ValueError()
            return v, raw, tag
        except (ValueError, TypeError, KeyError, AttributeError):
            fail('state_UNKNOWN')

    @staticmethod
    def shared_binding(shared, c):
        state, _, _ = shared.read()
        r = state['operations'].get(c['operation'])
        if not r or r['state'] not in {'accepted', 'executing', 'awaiting_readback', 'complete'}:
            fail('shared_claim_required')
        req = shared.parse_request(r['request'])
        if (not isinstance(req, AccountingProbeRequest) or req.request_id != c['shared_request_id']
                or req.ownership != c['ownership'] or req.owner != c['owner']
                or req.native_preflight_digest != c['fingerprint'] or r['events'][0]['state'] != 'accepted'):
            fail('shared_binding')
        # The accepted event remains stable as shared document revisions advance.
        return dict(operation=c['operation'], request_id=req.request_id, fingerprint=r['fingerprint'],
                    accepted_at=r['events'][0]['at'])

    def claim_fixture(self, candidate, shared, auth, context, ledger_io, *, now):
        c = validate_candidate(candidate)
        if not isinstance(auth, PurposeBoundary) or auth.owner != c['owner']:
            fail('auth_required')
        auth._post(context, ACCOUNTING_POST, c['request_id'], digest(c['inputs']), 0)
        p = auth.verify_post(context, ACCOUNTING_POST, c['request_id'], digest(c['inputs']), 0)
        if digest(asdict(p)) != c['authority']['authenticated_action_digest']:
            fail('authenticated_action_binding')
        if not c['authority']['issued_at'] <= now < c['authority']['expires_at']:
            fail('expired')
        claim = self.shared_binding(shared, c)
        if shared.read()[0]['operations'][c['operation']]['state'] not in {'accepted', 'executing'}:
            fail('shared_readback_only')
        ledger, token = observe_ledger(ledger_io)
        if ledger != c['before']:
            fail('before_changed')
        v, raw, tag = self.read()
        for r in v['operations'].values():
            old = r['candidate']
            if old['request_id'] == c['request_id'] or old['source']['source_id'] == c['source']['source_id']:
                fail('request_or_source_owned')
        old_items = v['legacy'].get('confirmation_items', {})
        if any(i.get('source', {}).get('source_id') == c['source']['source_id']
               and i.get('status') in {'pending', 'applied'} for i in old_items.values()):
            fail('legacy_pending_or_applied')
        after = deepcopy(v)
        after['operations'][c['operation']] = dict(candidate=c, phase='claimed', claim=claim,
            unstarted=dict(candidate=c['fingerprint'], sequence=0, ledger=token), start=None, completion=None)
        after['revision'] += 1
        self.replace(v, raw, tag, after)

    def advance_fixture(self, c, shared, phase, current, auth, context, ledger_io, *, now):
        """Persist test-only start/completion evidence. No writer call exists."""
        validate_candidate(c)
        v, raw, tag = self.read()
        after = deepcopy(v)
        r = after['operations'].get(c['operation'])
        if not r or r['candidate'] != c or self.shared_binding(shared, c) != r['claim']:
            fail('binding')
        if current != dict(source=c['source'], inputs=c['inputs'], authority=c['authority']):
            fail('current_binding')
        ledger, token = observe_ledger(ledger_io)
        if phase == 'writer_started' and r['phase'] == 'claimed':
            if shared.read()[0]['operations'][c['operation']]['state'] not in {'accepted', 'executing'}:
                fail('shared_readback_only')
            if not isinstance(auth, PurposeBoundary) or auth.owner != c['owner']:
                fail('auth_required')
            auth._post(context, ACCOUNTING_POST, c['request_id'], digest(c['inputs']), 0)
            p = auth.verify_post(context, ACCOUNTING_POST, c['request_id'], digest(c['inputs']), 0)
            if digest(asdict(p)) != c['authority']['authenticated_action_digest']:
                fail('authenticated_action_binding')
            if not c['authority']['issued_at'] <= now < c['authority']['expires_at']:
                fail('expired')
            if ledger != c['before'] or token != r['unstarted']['ledger']:
                fail('before_changed')
            r['start'] = dict(candidate=c['fingerprint'], claim=digest(r['claim']), sequence=1)
        elif phase == 'complete_fixture' and r['phase'] == 'writer_started':
            if ledger != planned_after(c['before'], c['plan']):
                fail('completion_readback')
            r['completion'] = dict(mode=FIXTURE, candidate=c['fingerprint'], start=digest(r['start']),
                                   ledger=digest(ledger), observation=token)
        else:
            fail('readback_only')
        r['phase'] = phase
        after['revision'] += 1
        self.replace(v, raw, tag, after)


def observe_ledger(io):
    memory_only(io)
    raw, tag = io.read_versioned()
    obs = load(raw)
    if (not strong_etag(tag) or set(obs) != {'complete', 'snapshot'} or obs['complete'] is not True
            or io.read_versioned() != (raw, tag)):
        fail('ledger_UNKNOWN')
    return snapshot(obs['snapshot']), dict(tag=tag, raw_digest=digest(raw.hex()))


def classify_fixture(journal, shared, ledger_io, c, current, service):
    """Read-only design classification; never marks shared COMPLETE or retries."""
    memory_only(ledger_io)
    result = dict(synthetic_only=True, real_native_complete=False, accounting_permission=False,
                  classification='UNKNOWN', writer_dispatches=0)
    try:
        validate_candidate(c)
        before, _, _ = journal.read()
        shared_before = shared.read()[0]
        r = before['operations'].get(c['operation'])
        if (not r or r['candidate'] != c or journal.shared_binding(shared, c) != r['claim']
                or current != dict(source=c['source'], inputs=c['inputs'], authority=c['authority'])):
            return result
        if shared_before['operations'][c['operation']]['state'] == 'complete' and r['phase'] != 'complete_fixture':
            return result
        raw, tag = ledger_io.read_versioned()
        obs = load(raw)
        if not strong_etag(tag) or set(obs) != {'complete', 'snapshot'} or obs['complete'] is not True:
            return result
        observed = snapshot(obs['snapshot'])
        full = planned_after(c['before'], c['plan'])
        token = dict(tag=tag, raw_digest=digest(raw.hex()))
        if (observed == c['before'] and r['phase'] == 'claimed' and r['start'] is None
                and r['unstarted']['ledger'] == token):
            result['classification'] = 'NOT_EXECUTED'
        elif (observed == full and r['phase'] == 'complete_fixture'
                and r['completion']['observation'] == token):
            # Existing native _complete is a necessary check, never sufficient.
            class View:
                def get(self, region):
                    return deepcopy(observed[0][region.split('!')[0].strip("'")])
            read_service = copy(service)
            read_service.db = View()
            if ReceiptConfirmation._complete(read_service, c['plan']):
                result['classification'] = 'COMPLETE'
        elif observed != c['before'] and observed != full:
            result['classification'] = 'PARTIAL'
        # Three independent stores: double read, not a cross-store transaction.
        if (journal.read()[0] != before or shared.read()[0] != shared_before
                or ledger_io.read_versioned() != (raw, tag)):
            result['classification'] = 'UNKNOWN'
    except (StateError, ValueError, KeyError, TypeError, AttributeError):
        result['classification'] = 'UNKNOWN'
    return result
