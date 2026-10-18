"""Frozen wholly synthetic candidates; NEVER call an existing materializer."""
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4
from contextvars import ContextVar

from app.drive_run_state import StateError
from app.iphone_accounting_gate import SyntheticAccountingGate,NativePreflight,ENTRIES
from app.iphone_accounting_ownership import TrustedOwnershipReader,native_identity
from app.unified_receipt_purposes import PurposeBoundary,VerifiedPost,VerifiedRead
from app.unified_receipt_review import digest
from app.unified_receipt_write import ConditionalRequests
from app.page_receipt_model import SourceRef,PageUnit,stable_page
from app.pdf_intake_authority import review_identity
from app.models import ReceiptResult
from app.receipt_validation import validate_receipt_result
from app.receipt_pipeline import ReceiptPipeline
from app.receipt_plan.planning import check_duplicates
from app.bank_income import INCOME_SHEET,INCOME_HEADERS
from app.sheets import HEADERS
from staging_shadow.auto_native_evidence import (NativeEvidenceJournal,SyntheticAutoEvidenceAdapter,SCHEMA,LEDGER_SCHEMA,
    ledger_view,request,observation_token)
from staging_shadow.native_accounting_sandbox import NativeAccountingSandbox,NativeInvocation
from staging_shadow.accounting_recovery_model import SnapshotDB
from accounting_ownership_fixtures import Port,SourcePort,NativePort
from test_unified_receipt_write import Memory

OWNER=digest(['https://accounts.google.com','synthetic-owner']);NOW=1791530000
GOLDEN=json.loads((Path(__file__).parent/'fixtures/synthetic_auto_native.json').read_text('utf-8'))
RAW=b'%PDF-synthetic-auto-native-original'


def encoded(v):return json.dumps(v,sort_keys=True,separators=(',',':')).encode()


class IncomeReadView(SnapshotDB):
    def sheet_titles(self):return list(self.snapshot[0])
    def get(self,region):
        if region==INCOME_SHEET+'!A1:J1':return deepcopy(self.snapshot[0][INCOME_SHEET][:1])
        if region==INCOME_SHEET+'!A2:J':return deepcopy(self.snapshot[0][INCOME_SHEET][1:])
        return super().get(region)
    def get_raw(self,region):
        rows=super().get_raw(region)
        return rows[1:] if '!A2:' in region else rows


class Pending:
    synthetic_memory_only=True
    def __init__(self,h):self.h=h
    def read(self):
        h=self.h;v=h.base_pending.read()
        try:
            for r in h.journal.read()[0]['operations'].values():
                active=next((a._attempt.get() for a in h.instances if a._attempt.get() is not None),None)
                # Only this exact already claimed in-flight request may finish
                # its second preflight. Replay never enters a new gate context.
                if active and digest(active[2])==r['fingerprint']:continue
                v['records'].append(deepcopy(r['request']['ownership']))
        except Exception:v['complete']=False
        return v


class Harness:
    def __init__(self,sandbox=None):
        self.golden=deepcopy(GOLDEN);self.adapter=None;self.mode='complete';self.fault=None
        self.known_classification=None
        self.instances=[]
        self.text=ContextVar('synthetic_auto_text',default='')
        self.source_value=dict(source_id='synthetic-auto-native-source',sha256=sha256(RAW).hexdigest(),drive_revision_id='drive-r1',
            drive_numeric_version='17',native_source_version='native-v1',source_kind='pdf',page_count=14)
        self.source=SourcePort(dict({k:v for k,v in self.source_value.items() if k!='native_source_version'},generation=1),RAW)
        src=SourceRef(source_file_id=self.source_value['source_id'],source_content_hash=self.source_value['sha256'],page_count=14)
        units=[dict(unit_id='synthetic-unit-'+str(n),receipt_index=1,members=[dict(page=n,identity=stable_page(src,n),box=[0,0,1,1])]) for n in (1,4,14)]
        units.append(dict(unit_id='synthetic-grouped-unit',receipt_index=1,members=[dict(page=n,identity=stable_page(src,n),box=[0,0,1,1]) for n in (2,3)]))
        self.partition=Port(dict(source=self.source_value,generation=1,revision=1,state='confirmed',complete=True,units=units))
        self.ledger_port=Port(dict(generation=1,complete=True,records=[]));self.base_pending=Port(dict(generation=1,complete=True,records=[]))
        self.financial=Memory();self.financial.synthetic_memory_only=True
        self.native_io=Memory();self.native_io.synthetic_memory_only=True
        self.native_io.raw=encoded(dict(schema=SCHEMA,revision=0,operations={}))
        self.journal=NativeEvidenceJournal(self.native_io,enabled=True)
        self.ledger_io=Memory();self.ledger_io.synthetic_memory_only=True
        baseline={t:[deepcopy(HEADERS[t])] for t in ('レシート','支出明細','取込データ')};baseline[INCOME_SHEET]=[INCOME_HEADERS]
        self.baseline=[deepcopy(baseline),deepcopy(baseline)]
        self.ledger_io.raw=encoded(dict(schema=LEDGER_SCHEMA,generation=0,complete=True,snapshot=self.baseline))
        self.before_observation=observation_token(*self.ledger_io.read_versioned())
        self.pending=Pending(self);catalog={}
        for entry in ENTRIES:
            whole=entry in {'normal_auto','buyback_income','legacy_confirmation','future_one_tab'}
            unit=None if whole else next(u for u in units if u['unit_id']==
                ('synthetic-grouped-unit' if entry=='grouped_unit' else 'synthetic-unit-'+str(14 if entry=='fixed_canary' else 4 if entry=='explicit_unit' else 1)))
            namespace='receipt_confirmation' if whole else ('p14_posting' if entry=='fixed_canary' else 'receipt_unit_posting' if entry=='explicit_unit'
                else 'pdf_unit_processing' if entry=='grouped_unit' else 'pdf_intake')
            page=None
            if namespace=='pdf_intake':
                page=PageUnit(source=src,page_number=1,stable_page_identity=stable_page(src,1),automatic_classification='normal',
                    automatic_reason='synthetic',observation_complete=True,extraction_status='extracted',observation_render_hash='a'*64,
                    review_identity='0'*64,authority_revision=1).model_dump();page['review_identity']=review_identity(page)
            parsed=self.golden['buyback_income' if entry=='buyback_income' else 'normal_auto']['inputs']['parsed']
            n=dict(generation=1,owner=OWNER,source=deepcopy(self.source_value),case_id='synthetic-auto-case',purpose='ACCOUNTING_POST',
                action='buyback_income_post' if entry=='buyback_income' else 'post',native_context='synthetic-context',
                native_request_id='00000000-0000-4000-8000-000000000001',native_page=page,namespace=namespace,
                scope='whole' if whole else 'unit',unit_id=unit['unit_id'] if unit else None,
                receipt_index=1 if unit else None,input_snapshot=digest(parsed),authority_revision=1,
                ledger_targets=[('R-'+self.source_value['source_id']) if whole else 'R-'+unit['unit_id']],state='ready')
            if entry=='buyback_income':n['ledger_targets'].append(self.golden[entry]['plan']['operations'][1]['rows'][0][0])
            n['operation_identity']=native_identity(n,self.source_value,unit);catalog[entry,'u1']=n
        self.native=NativePort(catalog)
        self.reader=TrustedOwnershipReader(self.source,self.partition,self.native,self.ledger_port,self.pending)
        self.frozen={e:self.reader.observe(e,'u1').value() for e in ('normal_auto','buyback_income')}
        self.auth=PurposeBoundary(OWNER,self.verify,lambda c:VerifiedRead(**dict(dict(actor_id=OWNER,expires_at=NOW+600,session_active=True),**(c or {}))),
            lambda:[],clock=lambda:NOW)
        self.shared=ConditionalRequests(self.financial,'synthetic-shared-financial-claims',synthetic_accounting=True)
        self._rebuild()
        self.sandbox=sandbox or NativeAccountingSandbox(self.gate,[NativeInvocation(e,'u1',(),{}) for e in ENTRIES],enabled=True,isolated_synthetic=True)
        self.sandbox.gate=self.gate
        self._patch_extraction()

    def _patch_extraction(self):
        extraction=self.sandbox.module('receipt_text_extraction');privacy=self.sandbox.module('receipt_privacy_gate')
        privacy._extract_receipt_text=lambda *a,**kw:extraction._ReceiptTextExtraction('extracted','pdf_text',self.text.get())

    @staticmethod
    def verify(c,purpose,rid,binding,revision):
        return VerifiedPost(**dict(dict(actor_id=OWNER,purpose=purpose,request_id=rid,binding_digest=binding,
            expected_revision=revision,created_at=NOW,expires_at=NOW+600,csrf_valid=True,session_active=True),**(c or {})))

    def bindings(self,e,k):
        return dict(entry=e,key=k,source=self.source.read(),bytes_hash=sha256(self.source.raw).hexdigest(),
            partition=self.partition.read(),native=self.native.read(e,k),old_pending=self.base_pending.read(),ledger_scope=self.ledger_port.read(),
            privacy_provenance=self.known_classification)

    def candidate(self,e,k):
        golden=deepcopy(self.golden[e]);o=self.frozen[e]
        return dict(schema=SCHEMA,synthetic_only=True,ownership=o,kind=e,inputs=golden['inputs'],input_digest=digest(golden['inputs']),
            plan=golden['plan'],plan_digest=digest(golden['plan']),before=deepcopy(self.baseline),before_digest=digest(self.baseline),
            before_observation=self.before_observation,binding=self.bindings(e,k))

    def preflight(self,e,k,o):
        if e in {'normal_auto','buyback_income'}:
            parsed=ReceiptResult.model_validate(self.golden[e]['inputs']['parsed']);categories=self.golden[e]['inputs']['categories']
            token=self.text.set('買取\n合計 155円\n現金 155円' if e=='buyback_income' else 'レシート 商品 小計 730円 現金 730円')
            try:privacy=self.sandbox.module('receipt_privacy_gate').evaluate_receipt_privacy(RAW,'application/pdf',
                **({'known_source_classification':self.known_classification} if self.known_classification else {}))
            finally:self.text.reset(token)
            if (privacy.classification!='normal' or not privacy.gemini_allowed or not validate_receipt_result(parsed,[tuple(x) for x in categories])[0]
                    or ReceiptPipeline._kind_issues(parsed,privacy)):raise StateError('synthetic_native_validation_held')
            view=IncomeReadView(ledger_view(self.ledger_io)[0]);check_duplicates(view,o.source['source_id'],parsed)
            if e=='buyback_income':
                from app.utils import canonical_hash
                income,exists=ReceiptPipeline._buyback_income(SimpleNamespace(db=view),'receipt:'+o.source['source_id'],parsed,canonical_hash(parsed.model_dump()))
                if exists or income!=self.golden[e]['plan']['operations'][1]['rows'][0]:raise StateError('synthetic_income_plan_mismatch')
            checked=digest([parsed.model_dump(),privacy.model_dump(),self.golden[e]])
        else:checked=digest(['synthetic_legacy_read_projection_only',o.value()])
        return NativePreflight(digest(o.value()),checked,digest(self.bindings(e,k)),'"synthetic-native-read-only"')

    def _rebuild(self):
        old=self.adapter
        self.adapter=SyntheticAutoEvidenceAdapter(self.journal,self.shared,self.auth,self.candidate,self.bindings,self.ledger_io,enabled=True)
        self.instances=[self.adapter]
        if old:self.adapter.probe_calls=old.probe_calls;self.adapter.boundary_calls=old.boundary_calls
        self.adapter.fault=self.on_point
        self.gate=self.new_gate()

    def new_gate(self):
        return SyntheticAccountingGate(ConditionalRequests(self.financial,self.shared.store_id,synthetic_accounting=True),self.reader,self.auth,
            self.preflight,self.adapter.observe_boundary,enabled=True,isolated_synthetic=True,clock=lambda:NOW,checkpoint=self.adapter.checkpoint)

    def project(self,partial):
        current=next(a._attempt.get() for a in self.instances if a._attempt.get() is not None);req=current[2]
        raw,tag=self.ledger_io.read_versioned();value=json.loads(raw)
        rows=[(o['table'],r) for o in req['plan']['operations'] for r in o['rows']]
        if partial:rows=rows[:1]
        for title,row in rows:
            for part in value['snapshot']:
                part[title]=[r for r in part[title] if not r or r[0]!=row[0]]+[deepcopy(row)]
        value['generation']+=1;self.ledger_io.replace_versioned(raw,tag,encoded(value))

    def on_point(self,p):
        if p=='after_probe' and self.mode!='no_ledger':self.project(True)
        if p=='after_all_ledger' and self.mode=='complete':self.project(False)
        if self.fault:self.fault(p)

    def call(self,e='normal_auto',native_id=None,shared_id=None,revision=None,context=None,gate=None):
        native_id=native_id or str(uuid4());shared_id=shared_id or str(uuid4())
        revision=self.shared.read()[0]['revision'] if revision is None else revision
        if e not in {'normal_auto','buyback_income'}:
            return gate.submit(e,'u1',shared_id,revision,context or {}) if gate else self.sandbox.call(e,'u1',shared_id,revision,context or {})
        with self.adapter.attempt(e,'u1',native_id,shared_id,revision,context) as replay:
            if replay is not None:return replay
            return gate.submit(e,'u1',shared_id,revision,context or {}) if gate else self.sandbox.call(e,'u1',shared_id,revision,context or {})

    def restart(self):
        self.journal=NativeEvidenceJournal(self.native_io,enabled=True);self._rebuild();self.fault=None
        self.sandbox.gate=self.gate
