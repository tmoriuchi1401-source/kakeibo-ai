"""Explicit synthetic Sheets -> existing R1 draft handoff; stop before finance.

RawReader, encrypted PersistentJournal, R1 draft CAS/session and native planning
are reused. No writer, native qualification, authority issuance or live client.
The journal's optional confirmation slot belongs exclusively to this composition;
it must not share that slot with the older PDF-Unit acknowledgement service.
"""
from copy import deepcopy
from dataclasses import asdict
from uuid import NAMESPACE_URL, uuid5

from cryptography.fernet import InvalidToken

from app.drive_run_state import StateError
from app.iphone_native_dry_run import field_inputs
from app.iphone_item_drafts import image_issue, image_inspect
from app.iphone_persistent_drafts import PersistentJournal
from app.iphone_sheet_reader import RawReader, raw_value
from app.receipt_confirmation_contract import encoded, load, memory_only
from app.unified_receipt_review import digest
from .iphone_confirmation_ui import OneTabConfirmation, DraftCAS

SCHEMA = 'synthetic-sheet-draft-handoff-v1'
ACTIONS = {'import_draft', 'confirm_handoff'}
TERMINAL = {'DRAFT_IMPORTED', 'FINANCIAL_BOUNDARY_STOPPED'}


def deny(reason):
    raise StateError('sheet_handoff_' + reason)


class SheetDraftHandoff:
    def __init__(self, reader, journal, confirmation, *, enabled=False, isolated_synthetic=False):
        if (enabled is not True or isolated_synthetic is not True or type(reader) is not RawReader
                or type(journal) is not PersistentJournal or type(confirmation) is not OneTabConfirmation
                or journal.scope['owner'] != confirmation.owner
                or journal.binding != digest(reader.contract)):
            deny('isolation_required')
        # The whole new path, including its draft journal, is memory-only.
        memory_only(journal.transport.io)
        self.reader, self.journal, self.confirmation = reader, journal, confirmation
        self.owner = confirmation.owner
        self.contract_digest = digest(reader.contract)
        self.uncertain = False

    def _save(self, *args, **kwargs):
        try:
            return self.journal.save(*args, **kwargs)
        except Exception:
            self.uncertain = True
            raise

    def _state(self):
        state, _, tag = self.journal._state()
        value = deepcopy(state.get('confirmation', dict(schema=SCHEMA, requests={})))
        try:
            if (set(value) != {'schema', 'requests'} or value['schema'] != SCHEMA
                    or type(value['requests']) is not dict or len(value['requests']) > 64):
                deny('journal_quarantined')
            for rid, r in value['requests'].items():
                if (set(r) != {'case_id', 'action', 'fingerprint', 'sheet_snapshot', 'value', 'source', 'comparison', 'status', 'result', 'attempt_revision'}
                        or rid != str(uuid5(NAMESPACE_URL, r['fingerprint']))
                        or r['action'] not in ACTIONS or r['status'] not in TERMINAL | {'ATTEMPTING'}
                        or r['case_id'] not in self.reader.contract['cases']
                        or type(r['fingerprint']) is not str or len(r['fingerprint']) != 64
                        or r['status'] == 'ATTEMPTING' and r['result'] is not None):
                    deny('journal_quarantined')
                attempt = state['legacy']['requests'].get(str(uuid5(NAMESPACE_URL, rid+':attempt')))
                if not attempt or attempt['revision'] != r['attempt_revision']:
                    deny('journal_quarantined')
                if r['status'] in TERMINAL and r['status'] != (
                        'DRAFT_IMPORTED' if r['action'] == 'import_draft' else 'FINANCIAL_BOUNDARY_STOPPED'):
                    deny('journal_quarantined')
                if r['status'] in TERMINAL:
                    expected = self._result(rid, r)
                    if r['result'] != expected:
                        deny('journal_quarantined')
            return state, value, tag
        except (KeyError, TypeError, ValueError):
            deny('journal_quarantined')

    @staticmethod
    def _result(rid, record):
        return dict(request_id=rid, case_id=record['case_id'], status=record['status'],
            sheet_snapshot=record['sheet_snapshot'], input_digest=digest(record['value']),
            source=deepcopy(record['source']), comparison_digest=digest(record['comparison']),
            reviewed_input=deepcopy(record['value']), synthetic_only=True,
            native_authority=False, accounting_permission=False, writer_dispatches=0)

    def _auth(self, gate, action, rid, binding):
        session = self.confirmation._session(gate)
        checked = gate.post({}, 'ACCOUNTING_POST' if action == 'confirm_handoff' else 'METADATA_REVIEW', rid, binding, 0)
        if checked.actor_id != self.owner:
            deny('owner_required')
        return session

    def _capture(self, cid, action):
        if digest(self.reader.contract) != self.contract_digest:
            deny('contract_changed')
        batch = self.reader.read()
        draft = next((d for d in batch.drafts.values() if d['case_id'] == cid), None)
        ui = self.confirmation
        if (draft is None or draft['held'] or raw_value(draft['cells'][3]) == '保留'
                or draft['kind'] == 'PDF' or cid not in ui.cases or cid in ui.held):
            deny('held_or_unmapped')
        c = ui.cases[cid]
        if {'normal': '一般', 'medical': '医療'}.get(c.kind) != draft['kind']:
            deny('kind_changed')
        if ui._requests(cid) or DraftCAS(c.readers.authority).read()[0]:
            deny('native_attempt_UNKNOWN_read_only')
        d, _, tag = ui._draft(cid)
        values, observations = c.readers.observe()  # hash + revision-specific bytes + double read
        source = values[1]['source']; identity = draft['identity']
        native = c.store.read()[0]['legacy']['confirmation_items'][c.review_id]
        if (source != c.source or source['source_kind'] != 'image' or source['page_count'] != 1
                or identity != dict(source_id=source['source_id'], sha256=source['sha256'],
                    source_version=source['drive_revision_id'], version_scheme='drive_revision_id')
                or (native['source']['source_id'], native['source']['sha256'], native['source']['version']) !=
                    (source['source_id'], source['sha256'], source['native_source_version'])
                or native['kind'] != c.kind or values[0] != d):
            deny('independent_source_or_native_identity_changed')
        value = deepcopy(d['value']); fields = field_inputs(draft)
        if type(fields.get('amount')) is float and fields['amount'] == int(fields['amount']):
            fields['amount'] = int(fields['amount'])  # Sheets integer-valued number cell, never text coercion
        if c.kind == 'normal':
            # A receipt-wide category must never overwrite individual categories.
            baseline = self.reader.contract['cases'][cid]['cells']
            if fields['category'] != raw_value(baseline[7]):
                deny('receipt_category_requires_item_review')
            fields.pop('category')
        value['fields'].update(fields)
        image_inspect(value, image_issue(c.store.value, c.kind, c.source_id), values[2]['pairs'], c.kind)
        snapshot = None
        if action == 'confirm_handoff':
            if value != d['value']:
                deny('sheet_and_saved_draft_conflict')
            snapshot = ui._prepare(cid)  # existing exact v3/native validator and read-only plan only
        if self.reader.read() != batch or c.readers.observe() != (values, observations):
            deny('concurrent_snapshot')
        return batch, dict(draft=d, tag=tag, value=value, source=source,
                           observations=observations, snapshot=snapshot)

    @staticmethod
    def _unresolved(ledger):
        return any(r['status'] == 'ATTEMPTING' for r in ledger['requests'].values())

    def prepare(self, cid, action, gate):
        if self.uncertain:
            deny('attempt_UNKNOWN_read_only')
        if action not in ACTIONS:
            deny('action_invalid')
        session = self._auth(gate, action, 'prepare', digest([cid, action]))
        state, ledger, tag = self._state()
        if self._unresolved(ledger):
            deny('attempt_UNKNOWN_read_only')
        if any(r['case_id'] == cid and r['status'] == 'FINANCIAL_BOUNDARY_STOPPED'
               for r in ledger['requests'].values()):
            deny('duplicate_confirmation_read_only')
        batch, frame = self._capture(cid, action)
        imported = sorted((r for r in ledger['requests'].values()
                    if r['case_id'] == cid and r['status'] == 'DRAFT_IMPORTED'), key=lambda r:r['attempt_revision'])
        if action == 'confirm_handoff':
            if not imported or imported[-1]['sheet_snapshot'] != batch.snapshot_digest:
                deny('fresh_import_required')
        else:
            if imported and imported[-1]['sheet_snapshot'] == batch.snapshot_digest and frame['value'] == frame['draft']['value']:
                deny('already_imported_read_only')
        inner = self.confirmation.proof(cid, 'save_draft', gate)[0] if action == 'import_draft' else None
        if self._state()[2] != tag:
            deny('journal_conflict')
        claims = dict(schema=SCHEMA, case_id=cid, action=action, session=session,
            journal_tag=tag, journal_revision=state['legacy']['revision'],
            scope=deepcopy(self.journal.scope), batch=asdict(batch), frame=frame, inner_proof=inner)
        rid = str(uuid5(NAMESPACE_URL, digest(claims)))
        return dict(request_id=rid, case_id=cid, action=action, kind=self.confirmation.cases[cid].kind,
                    source=deepcopy(frame['source']), comparison=deepcopy(frame['snapshot']),
                    proof=self.confirmation.cipher.encrypt(encoded(claims)).decode(),
                    review=deepcopy(frame['value']), sheet_snapshot=batch.snapshot_digest,
                    accounting_permission=False, writer_dispatches=0)

    def post(self, payload, gate):
        if self.uncertain:
            deny('attempt_UNKNOWN_read_only')
        if type(payload) is not dict or set(payload) != {'proof', 'confirmed', 'case_id', 'sheet_snapshot'} or type(payload['confirmed']) is not bool:
            deny('request_invalid')
        try:
            if type(payload['proof']) is not str or len(payload['proof']) > 600000:
                deny('proof_invalid')
            claims = load(self.confirmation.cipher.decrypt(payload['proof'].encode()))
            if (set(claims) != {'schema', 'case_id', 'action', 'session', 'journal_tag', 'journal_revision',
                    'scope', 'batch', 'frame', 'inner_proof'} or claims['schema'] != SCHEMA
                    or claims['action'] not in ACTIONS or claims['scope'] != self.journal.scope):
                deny('proof_invalid')
        except (InvalidToken, TypeError, ValueError, KeyError):
            deny('proof_invalid')
        fingerprint = digest(claims); rid = str(uuid5(NAMESPACE_URL, fingerprint))
        cid, action = claims['case_id'], claims['action']
        if payload['case_id'] != cid or payload['sheet_snapshot'] != claims['batch']['snapshot_digest']:
            deny('displayed_case_or_snapshot_changed')
        if self._auth(gate, action, rid, fingerprint) != claims['session']:
            deny('session_changed_or_expired')
        if payload['confirmed'] is not (action == 'confirm_handoff'):
            deny('explicit_confirmation_required')
        state, ledger, tag = self._state()
        if rid in ledger['requests'] or self._unresolved(ledger):
            deny('attempt_read_only_no_resend')
        if (tag != claims['journal_tag'] or state['legacy']['revision'] != claims['journal_revision']):
            deny('journal_conflict')
        batch, frame = self._capture(cid, action)
        if asdict(batch) != claims['batch'] or frame != claims['frame']:
            deny('stale_sheet_or_native_snapshot')
        record = dict(case_id=cid, action=action, fingerprint=fingerprint,
            sheet_snapshot=batch.snapshot_digest, value=deepcopy(frame['value']),
            source=deepcopy(frame['source']), comparison=deepcopy(frame['snapshot']), status='ATTEMPTING', result=None,
            attempt_revision=state['legacy']['revision']+1)
        pending = deepcopy(ledger); pending['requests'][rid] = record
        # Durable intent BEFORE the separate existing draft CAS. A lost response
        # leaves an unresolved attempt; another instance never dispatches it.
        attempt = self._save(batch, tag, str(uuid5(NAMESPACE_URL, rid+':attempt')), confirmation=pending)
        if attempt['status'] != 'draft_saved':
            # PersistentJournal can resolve an exact replay read-only. That is
            # a receipt, never a new lease to enter the next operation.
            deny('attempt_read_only_no_resend')
        if self._capture(cid, action) != (batch, frame):
            deny('attempt_UNKNOWN_snapshot_changed')
        if action == 'import_draft':
            try:
                saved_draft = self.confirmation.post(dict(proof=claims['inner_proof'], value=frame['value'], confirmed=False), gate)
            except Exception:
                self.uncertain = True
                raise
            observed = self.confirmation._draft(cid)[0]
            if (saved_draft.get('newly_saved') is not True or observed['value'] != frame['value']
                    or observed['revision'] != frame['draft']['revision']+1):
                deny('draft_UNKNOWN')
            _, observations = self.confirmation.cases[cid].readers.observe()
            if observations[1:] != frame['observations'][1:]:
                deny('attempt_UNKNOWN_original_or_master_changed')
        # confirm_handoff intentionally has NO call to post/qualify/dispatch.
        _, current, current_tag = self._state()
        if current != pending or self.reader.read() != batch:
            deny('attempt_UNKNOWN_readback')
        after = deepcopy(current)
        saved = after['requests'][rid]
        saved['status'] = 'DRAFT_IMPORTED' if action == 'import_draft' else 'FINANCIAL_BOUNDARY_STOPPED'
        saved['result'] = self._result(rid, saved)
        self._save(batch, current_tag, str(uuid5(NAMESPACE_URL, rid+':result')), confirmation=after)
        return self.inspect(rid, gate)

    def inspect(self, rid, gate):
        self.confirmation._session(gate)
        # Read-only even after process restart, source changes or response loss.
        _, ledger, _ = self._state()
        record = ledger['requests'].get(rid)
        if not record or record['status'] == 'ATTEMPTING':
            return dict(request_id=rid, status='UNKNOWN', synthetic_only=True,
                native_authority=False, accounting_permission=False, writer_dispatches=0)
        return deepcopy(record['result'])
