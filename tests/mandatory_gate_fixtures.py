"""Wholly invented, disposable memory composition; no service account or AI."""
from copy import deepcopy
from dataclasses import asdict
from types import SimpleNamespace
from uuid import uuid4

from app.bank_income import INCOME_SHEET, INCOME_HEADERS
from app.iphone_accounting_ownership import native_identity
from app.receipt_confirmation_contract import encoded, token, FIXTURE
from app.receipt_confirmation_draft_request import DraftReaders, prepare_inputs
from app.receipt_reimport_production import target_snapshot
from app.sheets import HEADERS
from app.unified_receipt_review import digest
from staging_shadow.auto_native_evidence import observation_token
from staging_shadow.mandatory_receipt_gate import MandatoryReceiptComposition, RetainedHistoryReader
from staging_shadow.receipt_draft_contract import IsolatedDraftStore, IsolatedDraftContract
from auto_native_fixtures import Harness, NOW
from draft_runtime_fixtures import harness as draft_harness, RAW as DRAFT_RAW, rehash, set_value
from accounting_gate_fixtures import reader_for


def canonical(before):
    rows = deepcopy(before)
    for part in rows:
        for title in part:
            part[title] = part[title][1:]
    return dict(complete=True, snapshot=rows)


def with_headers(rows):
    rows = [{t:deepcopy(p[t]) for t in ('レシート','支出明細','取込データ',INCOME_SHEET)} for p in rows]
    for part in rows:
        for title in part:
            part[title].insert(0, deepcopy(INCOME_HEADERS if title == INCOME_SHEET else HEADERS[title]))
    return rows


class Rig:
    def __init__(self):
        self.h = Harness(connect_existing_writer=True)
        self.history = dict(complete=True, records=[])
        self.backends = self.h.financial, self.h.ledger_io, self.h.native_io
        self.backends[1].raw = encoded(canonical(self.h.baseline))
        self.instances = []
        self.new(fresh=True)

    def new(self, *, fresh=False):
        h = self.h
        c = MandatoryReceiptComposition(shared_io=self.backends[0], ledger_io=self.backends[1],
            auto_native_io=self.backends[2], auth=h.auth, reader=h.reader, preflight=h.preflight,
            candidate_reader=h.candidate, binding_reader=h.bindings,
            history_reader=RetainedHistoryReader(lambda:deepcopy(self.history),legacy_ports=()),
            enabled=True, isolated_synthetic=True, allow_new_attempts=fresh, clock=lambda:NOW)
        self.instances.append(c)
        h.ledger_io, h.journal, h.shared = c.auto_ledger, c.journal, c.shared
        h.instances = [i.adapter for i in self.instances]
        if len(self.instances)==1:
            h.before_observation = observation_token(*c.auto_ledger.read_versioned())
        self.c = c
        return c

    def call(self, entry='normal_auto', *, c=None, ids=None, revision=None):
        c = c or self.c
        ids = ids or (str(uuid4()), str(uuid4()))
        revision = c.shared.read()[0]['revision'] if revision is None else revision
        return c.make_receipt_pipeline(entry, 'u1').process_bytes(self.h.source.raw,
            'application/pdf' if self.h.source_value['source_kind'] == 'pdf' else 'image/png',
            native_id=ids[0], shared_id=ids[1], revision=revision)

    def frozen(self):
        return [p.read_versioned() for p in self.backends]

    def dispatches(self):
        return sum(len(c.writer.dispatches) for c in self.instances)


def joint_r1(r, monkeypatch, kind='normal'):
    # Create a NEW empty-target fixture before any v2/v3 root is pinned.
    # Never rewrite a retained native document/pending to enable processing.
    import iphone_draft_request_design_examples as examples
    import draft_runtime_fixtures as runtime
    original_factory, original_example = examples.native_fixture, examples.Example
    def empty_native(k):
        service, store, db, metadata, key = original_factory(k)
        sid = service.items[key]['source']['source_id']
        db.rows['レシート'] = [row for row in db.rows['レシート'] if row[0] != 'R-'+sid]
        service.items[key]['before'] = target_snapshot(service.tables(), sid)
        if sid in store.value.get('records', {}):
            store.value['records'][sid]['before'] = deepcopy(service.items[key]['before'])
        return service, store, db, metadata, key
    class EmptyExample(original_example):
        def __init__(self, k, m):super().__init__(k, m, existing_expense=False)
    with monkeypatch.context() as m:
        m.setattr(examples, 'native_fixture', empty_native)
        m.setattr(runtime, 'Example', EmptyExample)
        d = draft_harness(kind, m)
    # Test provisioning, before starting any financial attempt.
    before = deepcopy(d.c['before'])
    for part in before:part[INCOME_SHEET] = []
    r.backends[1].raw = encoded(dict(complete=True, snapshot=before))
    h = r.h; old_sid = h.source_value['source_id']; source = deepcopy(d.c['source'])
    h.source_value = source
    h.source.frame = dict({k:v for k,v in source.items() if k!='native_source_version'}, generation=1)
    h.source.raw = DRAFT_RAW
    h.partition.frame = dict(source=source, generation=1, revision=1, state='not_applicable', complete=True, units=[])
    def replace(v):
        if isinstance(v, str):return v.replace(old_sid, source['source_id'])
        if isinstance(v, list):return [replace(x) for x in v]
        if isinstance(v, dict):return {k:replace(x) for k,x in v.items()}
        return v
    h.golden = replace(h.golden)
    from app.receipt_pipeline import ReceiptPipeline
    from app.models import ReceiptResult
    from app.utils import canonical_hash
    from auto_native_fixtures import IncomeReadView
    parsed = ReceiptResult.model_validate(h.golden['buyback_income']['inputs']['parsed'])
    income, _ = ReceiptPipeline._buyback_income(SimpleNamespace(db=IncomeReadView(with_headers(before))),
        'receipt:'+source['source_id'], parsed, canonical_hash(parsed.model_dump()))
    h.golden['buyback_income']['plan']['operations'][1]['rows'] = [income]
    for entry in ('normal_auto', 'buyback_income'):
        n = h.native.frame[entry, 'u1']; n['source'] = source
        n['ledger_targets'] = replace(n['ledger_targets'])
        if entry == 'buyback_income':n['ledger_targets'][1] = income[0]
        n['operation_identity'] = native_identity(n, source, None)
    h.baseline = with_headers(before)
    h.frozen = {e:h.reader.observe(e,'u1').value() for e in ('normal_auto','buyback_income')}
    h.before_observation = observation_token(*r.c.auto_ledger.read_versioned())
    c = r.c
    native_port = c.r1_native_port(d.native)
    d.readers = DraftReaders(**dict(d.ports, ledger=c.ledger), ownership=lambda:d.own.reader.observe('legacy_confirmation','u1'))
    inputs, baseline, plan = prepare_inputs(d.legacy, kind, d.f.case_id, d.c['owner'], token(*native_port.read_versioned()),
        '2026-10-19 00:00:00', d.readers)
    d.own = reader_for('legacy_confirmation', source, DRAFT_RAW, [], dict(namespace='receipt_confirmation',scope='whole',
        unit_id=None,receipt_index=None,input_snapshot=digest(inputs),authority_revision=7,
        ledger_targets=[row[0] for _,row in plan],case_id=d.f.case_id))
    ownership = d.own.reader.observe('legacy_confirmation','u1').value()
    authority = deepcopy(d.c['authority']);authority.update(input_fingerprint=digest(inputs),
        scope_digest=digest([ownership['operation_identity'],source,plan,baseline]))
    # Preserve exact existing authenticated-action shape from PurposeBoundary.
    from app.unified_receipt_purposes import VerifiedPost
    from draft_runtime_fixtures import NOW as DRAFT_NOW
    p=VerifiedPost(d.c['owner'],'ACCOUNTING_POST',d.c['request_id'],digest(inputs),0,DRAFT_NOW,DRAFT_NOW+600,
        csrf_valid=True,session_active=True)
    authority['authenticated_action_digest']=digest(asdict(p))
    d.c.update(inputs=inputs,plan=plan,before=baseline,authority=authority,ownership=ownership)
    rehash(d.c);set_value(d.ports['authority'],authority)
    d.store=IsolatedDraftStore(native_port,d.manifest,d.spid,parent_digest=d.pin,enabled=True,isolated_synthetic=True)
    d.contract=IsolatedDraftContract(d.store,c.shared,d.auth,d.readers,clock=lambda:DRAFT_NOW,
        fresh_process=True,enabled=True,isolated_synthetic=True)
    return d
