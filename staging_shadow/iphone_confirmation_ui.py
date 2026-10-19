"""One-tab R1 adapter for the existing host, v3 contract and guarded writer.

Explicit injection only; memory ports only. No production factory or login.
GET prepares a read-only comparison, never a qualification or authority.
"""
from copy import deepcopy
from dataclasses import asdict
import json
from threading import Lock
from uuid import NAMESPACE_URL, uuid5

from cryptography.fernet import Fernet, InvalidToken

from app.drive_run_state import StateError
from app.iphone_item_drafts import image_issue, image_inspect
from app.receipt_confirmation_contract import FIXTURE, encoded, load, memory_only, token
from app.receipt_confirmation_draft_request import prepare_inputs, validate_draft_request_v2
from app.unified_receipt_purposes import PurposeBoundary
from app.unified_receipt_review import digest
from app.unified_receipt_write import ConditionalRequests
from .receipt_draft_contract import IsolatedDraftContract, IsolatedDraftStore
from .synthetic_draft_writer import SyntheticDraftConfirmation


LABELS = dict(draft_saved='下書き保存済み', reconfirm='確定前の再確認が必要',
    COMPLETE='合成検証での記帳成功', PARTIAL='PARTIAL：部分反映・要照合',
    UNKNOWN='UNKNOWN：結果不明・要照合', held='保留', rejected='競合または期限切れによる拒否')


def deny(code):
    raise StateError('iphone_r1_' + code)


class DraftCAS:
    """Transport adapter; delegates CAS/read-back to the existing engine."""
    def __init__(self, io):
        memory_only(io)
        self.io = io

    def read(self):
        raw, tag = self.io.read_versioned()
        token(raw, tag)
        return load(raw), raw, tag

    replace = ConditionalRequests.replace


class OneTabConfirmation:
    def __init__(self, cases, held, key, *, owner, clock, enabled=False, isolated_synthetic=False):
        if enabled is not True or isolated_synthetic is not True:
            deny('isolation_required')
        self.cases, self.held, self.owner, self.clock = cases, deepcopy(held), owner, clock
        self.cipher = Fernet(key)
        self.lock = Lock()
        if (len({id(c.shared.io) for c in cases.values()}) != 1
                or len({id(c.readers.ledger) for c in cases.values()}) != 1
                or len({c.source_id for c in cases.values()}) != len(cases)):
            deny('shared_ownership_and_ledger_required')
        for cid, c in cases.items():
            if (cid in held or type(c.store) is not IsolatedDraftStore or c.kind not in {'normal', 'medical'}
                    or type(c.shared) is not ConditionalRequests or not c.shared.synthetic_accounting):
                deny('isolation_required')
            for port in (c.store.io, c.shared.io, c.readers.draft, c.readers.source,
                         c.readers.categories, c.readers.ledger, c.readers.source_bytes, c.readers.authority):
                memory_only(port)
            self._draft(cid)

    def _draft(self, cid):
        if cid not in self.cases or cid in {'G103', 'H301', 'SYN-G103', 'SYN-H301'}:
            deny('held')
        c = self.cases[cid]
        d, raw, tag = DraftCAS(c.readers.draft).read()
        source = load(c.readers.source.read_versioned()[0])['source']
        if source != c.source:
            deny('source_changed_reconfirm')
        if (set(d) != {'domain','owner','case_id','generation','revision','input_digest','value'}
                or d['domain'] != 'DRAFT_ONLY' or d['owner'] != self.owner or d['case_id'] != cid
                or type(d['revision']) is not int or d['revision'] < 0
                or type(d['generation']) is not int or d['generation'] < 1
                or d['input_digest'] != digest(d['value'])):
            deny('draft_changed')
        baseline = image_issue(c.store.value, c.kind, c.source_id)
        image_inspect(d['value'], baseline, load(c.readers.categories.read_versioned()[0])['pairs'], c.kind)
        return d, raw, tag

    def _prepare(self, cid):
        c = self.cases[cid]
        native = c.store.read()
        inputs, before, plan = prepare_inputs(native[0]['legacy'], c.kind, cid, self.owner,
            token(native[1], native[2]), c.prepared_at, c.readers)
        fields, parsed = inputs['draft']['value']['fields'], inputs['proposed']['parsed']
        if any(parsed[k] != fields[f] for k,f in (
                ('date','date'), ('merchant','merchant'), ('total','amount'), ('payment_method','payment'))):
            deny('normalization_requires_explicit_edit')
        ownership = c.ownership(inputs, plan)
        if c.store.read() != native:
            deny('reconfirm')
        # JSON transport representation only; no field coercion/normalization.
        return load(encoded(dict(inputs=inputs, before=before, plan=plan,
                                 source=inputs['source_binding']['source'], ownership=ownership)))

    def read(self, gate):
        gate.read({})
        rows = []
        for cid, c in self.cases.items():
            d, _, tag = self._draft(cid)
            baseline = image_issue(c.store.value, c.kind, c.source_id)
            latest = self._requests(cid)
            evidence = DraftCAS(c.readers.authority).read()[0]
            result = self.inspect(latest[-1]['request_id'], gate) if latest else None
            if not latest and evidence:
                rid=evidence.get('request_id', 'unavailable') if type(evidence) is dict else 'unavailable'
                result=self.inspect(rid, gate)
            rows.append(dict(case_id=cid, kind=c.kind, draft=d, tag=tag, baseline=baseline,
                source=deepcopy(c.source),
                held=False, reason='', result=result))
        rows.extend(dict(case_id=cid, held=True, reason=value['reason'], inputs=value.get('inputs', []),
                         kind=value.get('kind', 'held'), source_id=value.get('source_id', ''))
                    for cid,value in self.held.items())
        return rows

    def _requests(self, cid):
        return [r['request'] for r in self.cases[cid].store.read()[0]['operations'].values()
                if isinstance(r, dict) and isinstance(r.get('request'), dict) and r['request']['case_id'] == cid]

    @staticmethod
    def _store(c):
        return IsolatedDraftStore(c.store.io, c.store.expected_digest, c.store.spreadsheet_id,
            parent_digest=c.store.parent_digest, enabled=True, isolated_synthetic=True)

    def _session(self, gate):
        _, ticket, record, actor = gate.current()
        if (actor.issuer != 'https://accounts.google.com' or actor.actor_id != self.owner
                or digest([actor.issuer, actor.subject]) != self.owner
                or not record['created_at'] <= self.clock() < record['expires_at']
                or record['expires_at'] - record['created_at'] != 600):
            deny('owner_session_expired')
        return dict(actor=self.owner, session=digest([record['request_id'], ticket.cookie]),
                    created_at=record['created_at'], expires_at=record['expires_at'])

    def proof(self, cid, action, gate):
        if action not in {'save_draft', 'post'}:
            deny('purpose')
        session = self._session(gate)
        d, _, tag = self._draft(cid)
        if action == 'post' and not self._requests(cid) and DraftCAS(self.cases[cid].readers.authority).read()[0]:
            deny('attempt_UNKNOWN_read_only')
        snapshot = self._prepare(cid) if action == 'post' else None
        claims = dict(schema='synthetic-one-tab-proof-v1', case_id=cid, purpose='ACCOUNTING_POST',
            action=action, draft=d, tag=tag, snapshot=snapshot, **session)
        claims['request_id'] = str(uuid5(NAMESPACE_URL, digest(claims)))
        return self.cipher.encrypt(encoded(claims)).decode(), deepcopy(claims)

    def post(self, payload, gate):
        if type(payload) is not dict or set(payload) != {'proof', 'value', 'confirmed'}:
            deny('request')
        try:
            if type(payload['proof']) is not str or len(payload['proof']) > 600000:
                deny('proof')
            claims = load(self.cipher.decrypt(payload['proof'].encode()))
        except (InvalidToken, ValueError, TypeError):
            deny('proof')
        if self._session(gate) != {k:claims[k] for k in ('actor','session','created_at','expires_at')}:
            deny('session_expired')
        cid, rid, action = claims['case_id'], claims['request_id'], claims['action']
        if claims['purpose'] != 'ACCOUNTING_POST' or action not in {'save_draft', 'post'}:
            deny('purpose')
        attested = gate.post({}, 'ACCOUNTING_POST', rid, digest(claims), 0)
        if attested.actor_id != self.owner:
            deny('owner')
        if not self.lock.acquire(blocking=False):
            deny('in_flight_UNKNOWN')
        try:
            c = self.cases[cid]
            if action == 'post':
                if payload['confirmed'] is not True or payload['value'] != claims['draft']['value']:
                    deny('explicit_snapshot_confirmation')
                old = next((r for r in self._requests(cid) if r['request_id'] == rid), None)
                if old:
                    if any(old[k] != claims['snapshot'][k] for k in ('inputs','before','plan','source','ownership')):
                        deny('request_reuse')
                    return self.inspect(rid, gate)
                if self._requests(cid):
                    # A new session/request cannot replace evidence of an
                    # already qualified source, even when its ledger is empty.
                    deny('request_reuse')
            d, raw, tag = self._draft(cid)
            if (d != claims['draft'] or tag != claims['tag']):
                # Exact save response loss/replay can be acknowledged read-only.
                if (action == 'save_draft' and d['revision'] == claims['draft']['revision']+1
                        and d['value'] == payload['value']):
                    return dict(status='draft_saved', label=LABELS['draft_saved'], request_id=rid,
                                draft_revision=d['revision'], draft_tag=tag)
                deny('reconfirm')
            if action == 'save_draft':
                if payload['confirmed'] is not False:
                    deny('save_is_nonfinancial')
                baseline = image_issue(c.store.value, c.kind, c.source_id)
                master = load(c.readers.categories.read_versioned()[0])['pairs']
                image_inspect(payload['value'], baseline, master, c.kind)
                values, observations = c.readers.observe()
                if (values[0] != d or observations[0] != token(raw, tag)
                        or values[1]['source'] != c.source or values[2]['pairs'] != master):
                    deny('save_source_or_draft_changed')
                after = dict(d, revision=d['revision']+1, value=deepcopy(payload['value']), input_digest=digest(payload['value']))
                DraftCAS(c.readers.draft).replace(d, raw, tag, after)
                observed, _, saved_tag=DraftCAS(c.readers.draft).read()
                if observed != after or saved_tag == tag:
                    deny('draft_UNKNOWN')
                return dict(status='draft_saved', label=LABELS['draft_saved'], request_id=rid,
                            draft_revision=after['revision'], draft_tag=saved_tag)
            current = self._prepare(cid)
            if current != claims['snapshot']:
                deny('reconfirm')
            # VerifiedPost is generated again for the EXACT native input, using
            # the existing host session/Origin/CSRF checks, never browser authority.
            boundary = PurposeBoundary(self.owner, gate.post, gate.read, lambda: [], enabled=True, clock=self.clock)
            p = gate.post({}, 'ACCOUNTING_POST', rid, digest(current['inputs']), 0)
            authority = dict(mode=FIXTURE, actor=self.owner, owner=self.owner, purpose='ACCOUNTING_POST',
                request_id=rid, input_fingerprint=digest(current['inputs']), revision=c.authority_revision,
                issued_at=p.created_at, expires_at=p.expires_at, revoked=False,
                authenticated_action_digest=digest(asdict(p)),
                scope_digest=digest([current['ownership']['operation_identity'], current['source'], current['plan'], current['before']]))
            request = dict(mode=FIXTURE, kind=c.kind, case_id=cid, review_id=c.review_id,
                operation=current['ownership']['operation_identity'], request_id=rid,
                shared_request_id=str(uuid5(NAMESPACE_URL, rid+':shared')), owner=self.owner,
                authority=authority, **current)
            request['fingerprint'] = digest(request)
            # Only synthetic evidence port; qualification is still the unchanged
            # v3 validator. A failed evidence CAS never reaches the writer.
            a, ar, at = DraftCAS(c.readers.authority).read()
            if a:
                # This existing port is the persistent synthetic attempt fence
                # even if qualification never reached the native store. Never
                # replace it after response loss or on a competing POST.
                deny('attempt_UNKNOWN_read_only')
            try:
                c.readers.authority.replace_versioned(ar, at, encoded(authority))
                if load(c.readers.authority.read_versioned()[0]) != authority:
                    deny('authority_UNKNOWN')
                validate_draft_request_v2(request, c.store.value, c.readers)
                contract = IsolatedDraftContract(self._store(c), c.shared, boundary, c.readers, clock=self.clock,
                    enabled=True, isolated_synthetic=True, fresh_process=True, checkpoint=getattr(c, 'checkpoint', None))
                writer = SyntheticDraftConfirmation(contract, enabled=True, isolated_synthetic=True, fresh_attempt=True,
                    checkpoint=getattr(c, 'checkpoint', None))
                writer.submit(request, expected_shared_revision=c.shared.read()[0]['revision'])
            except Exception:
                # Read-only reconstruction classifies partial writes; no recovery.
                return self.inspect(rid, gate)
            return self.inspect(rid, gate)
        finally:
            self.lock.release()

    def inspect(self, rid, gate):
        gate.read({})
        for cid,c in self.cases.items():
            request = next((r for r in self._requests(cid) if r['request_id'] == rid), None)
            if request:
                store = self._store(c)
                boundary = PurposeBoundary(self.owner, gate.post, gate.read, lambda: [], enabled=True, clock=self.clock)
                contract = IsolatedDraftContract(store, c.shared, boundary, c.readers, clock=self.clock,
                    enabled=True, isolated_synthetic=True)
                writer = SyntheticDraftConfirmation(contract, enabled=True, isolated_synthetic=True)
                result = writer.inspect(request)
                status = result['classification']
                return dict(status=status, label=LABELS[status] if status in LABELS else LABELS['UNKNOWN'],
                    request_id=rid, case_id=cid, snapshot=deepcopy(request['inputs']['draft']),
                    fingerprint=request['fingerprint'], synthetic_only=True, retry_allowed=False)
        return dict(status='UNKNOWN', label=LABELS['UNKNOWN'], request_id=rid, retry_allowed=False)
