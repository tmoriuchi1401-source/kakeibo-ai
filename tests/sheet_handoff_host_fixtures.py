"""Actual existing RSA/session/CSRF host plus existing #146 image composition."""
from copy import deepcopy
from uuid import uuid4

from cryptography.fernet import Fernet

from app.iphone_draft_journal import LocalJournal
from app.iphone_persistent_drafts import PersistentJournal
from app.iphone_sheet_handoff_host import make_isolated_sheet_handoff_application, PREFIX
from app.iphone_sheet_reader import RawReader, issue_contract, original_formula
from app.unified_receipt_shadow import ShadowConfig
from staging_shadow.iphone_sheet_handoff import SheetDraftHandoff
from draft_runtime_fixtures import set_value
from iphone_r1_fixtures import Rig as ExistingRig
from test_iphone_sheet_reader import fixture, text
from test_legacy_contract_poc import mem
from unified_shadow_fixtures import ORIGIN


class HostRig:
    def __init__(self, monkeypatch, tmp_path):
        self.old = ExistingRig(monkeypatch, tmp_path); self.h = self.old.h
        self.raw, sources = fixture()
        for cid, c in self.old.native.items():
            cells = next(r['values'] for r in self.raw['sheets'][0]['data'][0]['rowData'][1:]
                         if r['values'][0]['userEnteredValue']['stringValue'] == cid)
            s = next(s for s in sources if s['case_id'] == cid)
            s.update(source_id=c.source_id, sha256=c.source['sha256'], source_version=c.source['drive_revision_id'])
            kind = cells[1]['userEnteredValue']['stringValue']
            cells[2] = {'userEnteredValue': {'formulaValue': original_formula(cid, s, kind)}}
            cells[15:17] = [text(s['sha256']), text(s['source_id'])]
            for f, col in dict(date=4, merchant=5, amount=6, category=7, memo=12).items():
                v = c.rig.f.fields[f]; cells[col]['userEnteredValue'] = {'numberValue' if type(v) is int else 'stringValue': v}
            if kind == '医療': cells[8] = text(c.rig.f.fields['payment'])
            # Trusted independent item review exists before this fixture's Sheet
            # import. The adapter never invents these individual categories.
            if kind == '一般':
                d = self.old.ui._draft(cid)[0]
                for item in d['value']['items']: item['state'] = 'entered'
                from app.unified_receipt_review import digest
                d['input_digest'] = digest(d['value']); set_value(c.readers.draft, d)
        self.sources = {s['source_id']: s for s in sources}
        self.contract = issue_contract(self.raw, sources)
        self.reader = RawReader(self.contract, lambda:deepcopy(self.raw), lambda:deepcopy(self.sources))
        batch = self.reader.read(); self.io = mem({}); self.key = Fernet.generate_key()
        self.journal = PersistentJournal(self.io, self.key, owner=self.old.ui.owner,
            contract_digest=batch.contract_digest, generation=1, initial_snapshot=batch.snapshot_digest)
        local = LocalJournal(tmp_path/'handoff-seed.json', batch.contract_digest)
        _, tag = local.initialize(); local.save(batch, tag, str(uuid4()))
        state, tag = local.read_versioned(); self.io.raw = self.journal.migration_payload(state, tag)
        self.adapter = SheetDraftHandoff(self.reader, self.journal, self.old.ui, enabled=True, isolated_synthetic=True)
        self.h.app = make_isolated_sheet_handoff_application(self.h.web.make_application, self.h.configured, self.adapter,
            config=ShadowConfig(True, 'isolated_synthetic'), cookies={'login':self.h.web.LOGIN_COOKIE,'ticket':self.h.web.COOKIE})
        self.h.app.testing = True; self.h.client = self.h.app.test_client()
        self.before = {cid:self.old.frozen(cid) for cid in self.old.native}
        def forbidden(*args, **kwargs): raise AssertionError('financial_port_reached')
        from app.receipt_confirmation import ReceiptConfirmation
        from staging_shadow.synthetic_draft_writer import SyntheticDraftConfirmation
        monkeypatch.setattr(ReceiptConfirmation, '_write_accounting_plan', forbidden)
        monkeypatch.setattr(SyntheticDraftConfirmation, '__init__', forbidden)
        for c in self.old.native.values():
            for port in (c.store.io, c.shared.io, c.readers.ledger, c.readers.authority):
                monkeypatch.setattr(port, 'replace_versioned', forbidden)

    def post(self, suffix, value, **headers):
        return self.h.client.post(PREFIX+suffix, json=value, base_url=ORIGIN,
            headers={'Origin':ORIGIN,'X-CSRF-Token':self.h.ticket.csrf,**headers})

    def prepare(self, cid, action):
        response = self.post('/prepare/'+cid+'/'+action, {})
        assert response.status_code == 200, response.json
        return response.json

    def send(self, proof, confirmed=False):
        return self.post('/request', dict(proof=proof['proof'], confirmed=confirmed,
            case_id=proof['case_id'], sheet_snapshot=proof['sheet_snapshot']))
