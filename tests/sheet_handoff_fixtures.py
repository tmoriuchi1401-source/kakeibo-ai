"""Existing v3 image fixtures and eight-case raw cells, synthetic memory only."""
from copy import deepcopy
from dataclasses import asdict
from uuid import uuid4

from cryptography.fernet import Fernet

from app.iphone_draft_journal import LocalJournal
from app.iphone_persistent_drafts import PersistentJournal
from app.iphone_sheet_reader import RawReader, issue_contract, original_formula
from app.unified_receipt_review import digest
from staging_shadow.iphone_sheet_handoff import SheetDraftHandoff
from test_iphone_r1_adapter import ui_fixture, Gate
from test_iphone_sheet_reader import fixture, text
from test_legacy_contract_poc import mem


class Rig:
    def __init__(self, kind, monkeypatch, tmp_path):
        self.h, self.c, self.make_ui = ui_fixture(kind, monkeypatch)
        self.ui = self.make_ui(); self.cid = self.h.f.case_id; self.gate = Gate()
        self.raw, source_list = fixture()
        cells = self.cells()
        source = next(s for s in source_list if s['case_id'] == self.cid)
        source.update(source_id=self.c.source_id, sha256=self.c.source['sha256'],
                      source_version=self.c.source['drive_revision_id'])
        cells[2] = {'userEnteredValue': {'formulaValue': original_formula(self.cid, source, cells[1]['userEnteredValue']['stringValue'])}}
        cells[15:17] = [text(source['sha256']), text(source['source_id'])]
        for f, col in dict(date=4, merchant=5, amount=6, category=7, memo=12).items():
            self.set(f, deepcopy(self.h.f.fields[f]))
        if kind == 'medical': self.set('payment', self.h.f.fields['payment'])
        self.sources = {s['source_id']: s for s in source_list}
        self.contract = issue_contract(self.raw, source_list)
        self.calls = dict(sheet=0, sources=0)
        self.reader = RawReader(self.contract, self.read_raw, self.read_sources)
        batch = self.reader.read()
        self.key = Fernet.generate_key(); self.io = mem({})
        self.journal = self.make_journal(batch)
        local = LocalJournal(tmp_path/'sheet-seed.json', batch.contract_digest)
        _, tag = local.initialize(); local.save(batch, tag, str(uuid4()))
        legacy, tag = local.read_versioned()
        self.io.raw = self.journal.migration_payload(legacy, tag)
        self.seed_batch = batch
        self.adapter = self.rebuild()
        self.before_financial = self.financial()
        # Fail before any real or memory financial mutation. Setup above is the
        # inherited explicit fixture provisioning, outside the new entrance.
        from app.receipt_confirmation import ReceiptConfirmation
        from staging_shadow.receipt_draft_contract import IsolatedDraftContract
        from staging_shadow.synthetic_draft_writer import SyntheticDraftConfirmation
        def forbidden(*args, **kwargs):
            raise AssertionError('financial_writer_or_authority_must_not_be_called')
        monkeypatch.setattr(ReceiptConfirmation, '_write_accounting_plan', forbidden)
        monkeypatch.setattr(IsolatedDraftContract, 'accept_fixture', forbidden)
        monkeypatch.setattr(SyntheticDraftConfirmation, '__init__', forbidden)
        for port in (self.c.store.io, self.c.shared.io, self.c.readers.ledger, self.c.readers.authority):
            monkeypatch.setattr(port, 'replace_versioned', forbidden)

    def make_journal(self, batch):
        return PersistentJournal(self.io, self.key, owner=self.ui.owner,
            contract_digest=batch.contract_digest, generation=1, initial_snapshot=batch.snapshot_digest)

    def rebuild(self):
        return SheetDraftHandoff(self.reader, self.make_journal(self.seed_batch), self.make_ui(),
            enabled=True, isolated_synthetic=True)

    def cells(self):
        return next(r['values'] for r in self.raw['sheets'][0]['data'][0]['rowData'][1:]
                    if r['values'][0]['userEnteredValue']['stringValue'] == self.cid)

    def set(self, field, value):
        col = dict(date=4, merchant=5, amount=6, category=7, payment=8, memo=12, status=3)[field]
        self.cells()[col]['userEnteredValue'] = {'numberValue' if type(value) in (int, float) else 'stringValue': value}

    def read_raw(self):
        self.calls['sheet'] += 1
        return deepcopy(self.raw)

    def read_sources(self):
        self.calls['sources'] += 1
        return deepcopy(self.sources)

    def financial(self):
        return [p.read_versioned() for p in (self.c.store.io, self.c.shared.io, self.c.readers.ledger, self.c.readers.authority)]

    def proof(self, action='import_draft'):
        return self.adapter.prepare(self.cid, action, self.gate)

    def send(self, proof, confirmed=False, adapter=None):
        return (adapter or self.adapter).post(dict(proof=proof['proof'], confirmed=confirmed,
            case_id=proof['case_id'], sheet_snapshot=proof['sheet_snapshot']), self.gate)

    def imported(self):
        p = self.proof(); assert self.send(p)['status'] == 'DRAFT_IMPORTED'
        return p
