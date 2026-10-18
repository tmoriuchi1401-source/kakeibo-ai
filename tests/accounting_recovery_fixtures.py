"""One Step A/B native suite, one financial backend and one evidence view per run."""
from copy import deepcopy
from hashlib import sha256
from types import SimpleNamespace
from uuid import uuid4

from app.iphone_accounting_gate import SyntheticAccountingGate
from app.unified_receipt_write import ConditionalRequests
from app.pdf_intake_runner import ledger_snapshot
from app.unified_receipt_review import digest
from app.sheets import HEADERS
from app.pdf_intake_registry import strong_etag
from staging_shadow.accounting_recovery_model import EvidenceModel, SCHEMA, encoded
from accounting_gate_fixtures import NOW
from test_unified_receipt_write import Memory


def future_aliases(s,plans):
    """Server-bound aliases to EXACT existing operations, no native grants."""
    from staging_shadow.native_accounting_sandbox import NativeAccountingSandbox, NativeInvocation
    for entry in sorted(set(e for e,k in plans)-{'future_one_tab'}):
        route=s.router.routes[entry,'u1'];key='alias-'+entry
        route.native.frame['future_one_tab',key]=deepcopy(route.native.frame[entry,'u1'])
        s.router.routes['future_one_tab',key]=route
        s.adapters['future_one_tab',key]=s.adapters[entry,'u1']
        s.invocations.append(NativeInvocation('future_one_tab',key,(),{}))
        plans['future_one_tab',key]=deepcopy(plans[entry,'u1'])
    old=s.sandbox
    s.sandbox=NativeAccountingSandbox(s.gate,s.invocations,enabled=True,isolated_synthetic=True)
    extraction=s.sandbox.module('receipt_text_extraction');privacy=s.sandbox.module('receipt_privacy_gate')
    privacy._extract_receipt_text=lambda *a,**kw:extraction._ReceiptTextExtraction('extracted','pdf_text',s._text.get())
    old.close()


def plans_for(s):
    result={}
    baseline=ledger_snapshot(s.pdf.db)
    from app.bank_income import INCOME_SHEET, INCOME_HEADERS
    for part in baseline:part[INCOME_SHEET]=[INCOME_HEADERS]+deepcopy(s.shared_income)
    for (entry,key),route in s.router.routes.items():
        namespace=route.native.frame[entry,key]['namespace']
        profile=namespace
        if entry in {'normal_auto','buyback_income'}:
            # Existing materializer in PLAN-ONLY mode, never the native writer.
            from app.receipt_plan.planning import PlanningDB
            from app.receipt_pipeline import ReceiptPipeline, INCOME_SHEET, INCOME_HEADERS
            class Capture(PlanningDB):
                def __init__(self):super().__init__(s.automatic[entry].db.categories());self.income=[]
                def sheet_titles(self):return [INCOME_SHEET]
                def get(self,region):return [INCOME_HEADERS] if 'A1:' in region else deepcopy(self.income)
                def append_raw(self,title,rows):
                    assert title==INCOME_SHEET
                    self.income.extend(deepcopy(rows));self.plan.extend((title,deepcopy(r)) for r in rows)
            db=Capture();source=route.native.frame[entry,key]['source']['source_id']
            ReceiptPipeline(db,None,clock=lambda:'2026-10-11 12:00:00')._materialize_result(s.auto_results[entry],source,'',[])
            rows=db.plan;profile='unsupported_auto'
        elif namespace=='receipt_confirmation':
            legacy=s.medical if key=='medical' else s.legacy
            item=next(iter(legacy.service.items.values()))
            rows=legacy.service._plan(item,legacy.service._parsed(item),'',False)
        elif namespace=='pdf_intake':
            p=s.pdf.port;r=s.pdf.runner
            ready=r.post_normal(p.key,p.uid,apply=False) if entry=='pdf_auto' else r.post_manual(p.key,p.uid,p.snapshot_reader(),apply=False)
            rows=[(o['table'],row) for o in ready['plan']['operations'] for row in o['rows']]
        elif namespace=='pdf_unit_processing':
            plan=s.grouped.port._plan(s.grouped.accounting.case)
            rows=[(o['table'],row) for o in plan.expected['operations'] for row in o['rows']]
        else:
            from app.receipt_plan import posting,unit_posting
            from test_p14_plan_only import RID
            native=posting if entry=='fixed_canary' else unit_posting
            rows=native.fresh_plan(s.canaries[entry].readers,RID)['rows']
        tables=list(dict.fromkeys(t for t,_ in rows))
        expected=dict(operations=[dict(table=t,rows=[deepcopy(row) for title,row in rows if title==t]) for t in tables])
        view=deepcopy(baseline)
        for title in tables:
            if title not in view[0]:
                from app.bank_income import INCOME_HEADERS
                for part in view:part[title]=[INCOME_HEADERS]+deepcopy(s.shared_income)
        result[entry,key]=(expected,view,profile)
    return result


class PendingOverlay:
    synthetic_memory_only=True
    def __init__(self,base,model):self.base,self.model=base,model
    def read(self):
        value=self.base.read()
        try:
            for r in self.model.read()[0]['records'].values():
                if r['native_state'] not in {'waiting','absent','unused','unsupported'} or r['writer_starts']:
                    value['records'].append(deepcopy(r['ownership']))
        except Exception:value['complete']=False
        return value


class Harness:
    def __init__(self,s,plans):
        self.s=s;self.base_pending=s.pending
        self.boundary_calls=[]
        self.server=Memory();self.server.synthetic_memory_only=True
        self.requests=ConditionalRequests(self.server,'synthetic-shared-financial-claims',synthetic_accounting=True)
        self.evidence=Memory();self.evidence.synthetic_memory_only=True
        baseline=deepcopy(next(iter(plans.values()))[1])
        self.evidence.raw=encoded(dict(schema=SCHEMA,generation=0,records={},ledger=baseline))
        self.plans=plans
        self.model=EvidenceModel(self.evidence,self.requests,s.auth,self.bindings,
            lambda e,k:deepcopy(plans[e,k]),enabled=True)
        self.pending=PendingOverlay(s.pending,self.model)
        for route in s.router.routes.values():route.reader.pending=self.pending
        self.gate=self.new_gate()
        s.sandbox.gate=self.gate

    def new_gate(self):
        return SyntheticAccountingGate(ConditionalRequests(self.server,self.requests.store_id,synthetic_accounting=True),
            self.s.router,self.s.auth,self.s.preflight,self.observe_boundary,enabled=True,isolated_synthetic=True,
            clock=lambda:NOW,checkpoint=self.model.checkpoint)

    def observe_boundary(self,entry,key,op):
        self.boundary_calls.append((entry,key,op))
        return self.model.observe_boundary(entry,key,op)

    def bindings(self,entry,key):
        route=self.s.router.routes[entry,key]
        raw,tag=self.s.adapters[entry,key].read_versioned()
        if type(raw) is not bytes or not strong_etag(tag):raise ValueError('native strong tag required')
        return dict(source=route.source.read(),bytes_hash=sha256(route.source.raw).hexdigest(),
            partition=route.partition.read(),native=route.native.read(entry,key),native_state=digest(raw.hex()),native_tag=tag,
            ledger=self.s.ledger.read(),pending=self.base_pending.read())

    def call(self,entry='pdf_manual',key='u1',rid=None,revision=None,context=None):
        if revision is None:revision=self.requests.read()[0]['revision']
        return self.s.sandbox.call(entry,key,rid or str(uuid4()),revision,context or {})

    def close(self):
        for route in self.s.router.routes.values():route.reader.pending=self.base_pending
        self.s.sandbox.gate=self.s.gate

    def restart(self):
        # Fresh coordinator/model objects; the same fake remote IO survives.
        self.model=EvidenceModel(self.evidence,self.requests,self.s.auth,self.bindings,
            lambda e,k:deepcopy(self.plans[e,k]),enabled=True)
        self.pending.model=self.model
        self.gate=self.new_gate();self.s.sandbox.gate=self.gate
