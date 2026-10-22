"""Same eight-case raw fixture, image/native readers, journal and draft CAS."""
from copy import deepcopy

from app.iphone_sheet_reader import RawReader
from staging_shadow.iphone_sheet_handoff import SheetDraftHandoff
from staging_shadow.sheets_only_surface import SyntheticSheetSurface, physical
from staging_shadow.sheets_only_review import SheetsOnlyReview
from sheet_handoff_host_fixtures import HostRig


class Rig:
    def __init__(self, monkeypatch, tmp_path):
        self.old = HostRig(monkeypatch, tmp_path)
        self.sheet = SyntheticSheetSurface(self.old.raw, self.old.contract)
        self.old.sources = self.sheet.sources
        self.reader = RawReader(self.old.contract, self.sheet.core, self.sheet.observe_sources)
        self.adapter = SheetDraftHandoff(self.reader, self.old.journal, self.old.old.ui,
                                        enabled=True, isolated_synthetic=True)
        self.worker = self.rebuild()
        self.draft_writes = lambda:sum(c.readers.draft.writes for c in self.old.old.native.values())
        def forbidden(*args, **kwargs): raise AssertionError('owner_gate_or_authority_called')
        # No forged owner gate: the #155 authenticated POST must stay unused.
        monkeypatch.setattr(self.adapter, 'prepare', forbidden)
        monkeypatch.setattr(self.adapter, 'post', forbidden)
        monkeypatch.setattr(self.old.old.ui, 'post', forbidden)
        monkeypatch.setattr(self.old.old.ui, '_session', forbidden)
        from staging_shadow.receipt_draft_contract import IsolatedDraftContract
        monkeypatch.setattr(IsolatedDraftContract, 'accept_fixture', forbidden)
        self.initial_core = self.sheet.core()
        self.initial_financial = self.financial()
        self.initial_auth = deepcopy(self.old.h.db.data)

    def rebuild(self):
        return SheetsOnlyReview(self.adapter, self.sheet, enabled=True, isolated_synthetic=True)

    def set(self, cid, field, value):
        col = dict(date=4, merchant=5, amount=6, category=7, payment=8, memo=12, state=3)[field]
        self.sheet.row(cid)[physical(col)]['userEnteredValue'] = {'numberValue' if type(value) in (int,float) else 'stringValue':value}

    def request(self, cid, command):
        self.sheet.row(cid)[1]['userEnteredValue'] = {'stringValue': command}

    def compare(self, cid='SYN-G101', generation=1):
        self.request(cid, '比較 '+str(generation))
        result = self.worker.poll()
        assert result[-1]['status'] == 'COMPARED', result
        return result[-1]

    def token(self, cid='SYN-G101'):
        values = self.sheet.row(cid)[1]['dataValidation']['condition']['values']
        return next(v['userEnteredValue'] for v in values if v['userEnteredValue'].startswith('確定 '))

    def financial(self):
        return {cid:self.old.old.frozen(cid) for cid in self.old.old.native}

    def assert_safe(self):
        assert self.financial() == self.initial_financial
        assert all(c.readers.ledger.writes == 0 for c in self.old.old.native.values())
        assert self.old.h.db.data == self.initial_auth  # no browser login/session issued


def no_clients(monkeypatch):
    from googleapiclient import discovery
    from google.cloud import firestore
    def forbidden(*args, **kwargs): raise AssertionError('live_client_forbidden')
    monkeypatch.setattr(discovery, 'build', forbidden)
    monkeypatch.setattr(firestore, 'Client', forbidden)


def evaluate(sheet, cid):
    """Independent IF/AND/EXACT interpreter, not the fixture display oracle."""
    import re
    import formulas
    from app.iphone_sheet_reader import raw_value
    f=sheet.row(cid)[2]['userEnteredValue']['formulaValue']
    function=formulas.Parser().ast(f)[1].compile()
    values=[]
    for name in function.inputs:
        match=re.fullmatch(r'([A-S])([1-9][0-9]*)',name)
        assert match,name
        cells=sheet.raw['sheets'][0]['data'][0]['rowData'][int(match[2])-1]['values']
        values.append(raw_value(cells[ord(match[1])-65]))
    result=function(*values)
    return str(result.item() if hasattr(result,'item') else result)
