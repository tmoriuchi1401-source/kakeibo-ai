"""Fresh synthetic resources only; never initializes/migrates real native state."""
from copy import deepcopy
from uuid import uuid4

from app.daily_corrections import DailyCorrections
from app.drive_run_state import StateBinding
from app.production_ledger import initial_ledger
from app.receipt_confirmation_contract import encoded
from staging_shadow.auto_native_evidence import load
from staging_shadow.isolated_update_adapters import native_code_identity, UpdateLedgerBoundary, _DB, _CorrectionsStore
from test_legacy_contract_poc import mem
from test_projection_refresh import initialized, row

REQUEST = 'REQ-' + 'a'*32


def memory(value):
    io = mem(value); io.supports_strong_cas = True
    return io


def imported(key='synthetic-paypay-1', source='PayPay', status='unclassified_paypay', amount=100):
    return [key, '', source, key, '2026-09-01', '合成店舗', amount, '通常払い', status, '', 'synthetic-hash', '']


class Rig:
    def __init__(self, entry='A03', *, receipt=None):
        tables = {t: [] for t in ('レシート', '支出明細', '取込データ', '銀行振込_入金明細')}
        tables['支出明細'] = [row('synthetic-existing', amount=7)]
        source = dict(money_mode=False,category_rule_auto_apply_enabled=False,
            bank_finalization=dict(approved_target='synthetic-joint-ledger',expected_head=native_code_identity()),
            categories=[['食費','食料品'], ['その他','未分類']],
            tables={'要確認': [], '店舗': [], 'カテゴリ自動分類ルール': []}, projection={})
        tables['取込データ'] = [imported()]
        if entry == 'A01':
            review = ['synthetic-paypay-1', '中', '2026-09-01', 'PayPay', '合成店舗', 100,
                      '', '', 'unclassified_paypay', '', '', '支出として計上', '', '食費', '食料品', '合成', '', '', '', '', '', '']
            source['tables']['要確認'] = [review]
        elif entry == 'A02':
            tables['取込データ'] += [imported('receipt:synthetic-r1','receipt','解析済')]
        elif entry == 'A03-bank':
            from app.bank_pdf_pipeline import DOCOMO_SMTB_SOURCE
            tables['取込データ'] = [imported('synthetic-bank-1', DOCOMO_SMTB_SOURCE, 'bank_expense', -100)]
        if receipt:
            self.receipt = receipt
            self.shared_io, self.ledger_io = receipt.backends[:2]
            if entry!='A10':
                value=load(self.ledger_io.raw)
                for part in value['snapshot']:
                    part['取込データ'].append(deepcopy(tables['取込データ'][0]))
                self.ledger_io.raw=encoded(value)  # Fresh fixture provisioning only.
            else:
                value=load(self.ledger_io.raw)
                for part in value['snapshot']:
                    if not part['支出明細']:part['支出明細'].append(row('synthetic-existing',amount=7))
                self.ledger_io.raw=encoded(value)
        else:
            self.shared_io = memory(dict(schema='unified-receipt-requests-v1',revision=0,operations={}))
            self.ledger_io = memory(dict(complete=True,snapshot=[deepcopy(tables),deepcopy(tables)]))
        for p in (self.shared_io, self.ledger_io): p.supports_strong_cas = True
        tables = load(self.ledger_io.read_versioned()[0])['snapshot'][0]
        store, _, _ = initialized(tables['支出明細'])
        source['projection'] = {k:deepcopy(store.data[k]) for k in ('catalog','index')}
        correction = {'requests': {}}
        native = dict(production=load(initial_ledger(StateBinding('production_run','synthetic-joint-ledger',
            'synthetic-state-folder','synthetic-run-ledger'))), corrections=correction)
        if entry == 'A10':
            db = _DB([deepcopy(tables),deepcopy(tables)], source)
            adapter = _CorrectionsStore(native, db)
            DailyCorrections(adapter, db).prepare(REQUEST, tables['支出明細'][0][0], {'amount':8})
            native = adapter.payload
        self.evidence_io = memory(dict(revision=0,payload=dict(synthetic_only=True,operations=[])))
        self.native_io = memory(dict(revision=0,payload=native))
        self.source_io = memory(dict(revision=0,payload=source))
        self.entry = entry; self.new()

    def new(self):
        self.boundary = UpdateLedgerBoundary(ledger_id='synthetic-joint-ledger', shared_io=self.shared_io,
            ledger_io=self.ledger_io, retained_update_ports=(), evidence_io=self.evidence_io,
            native_io=self.native_io, source_io=self.source_io, enabled=True, isolated_synthetic=True)
        self.adapter = self.boundary.compose_updates()
        return self.adapter

    def candidate(self, request_id=None):
        return self.adapter.prepare(self.entry, request_id or str(uuid4()),
            arguments=(REQUEST,) if self.entry == 'A10' else (('synthetic-bank-1',),) if self.entry=='A03-bank' else (),
            options={'head':native_code_identity(),'expected_head':native_code_identity()} if self.entry=='A03-bank' else None)

    def frozen(self):
        return tuple(p.read_versioned() for p in (self.shared_io,self.ledger_io,self.evidence_io,self.native_io,self.source_io))


def mutate(io, change):
    raw, tag = io.read_versioned(); value = load(raw); change(value)
    io.replace_versioned(raw,tag,encoded(value))
