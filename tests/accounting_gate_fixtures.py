"""Existing native preflights + private intercepted entry functions, no writer.

Native fixtures initialize fake state before measurement. Every financial
probe uses ONE shared ConditionalRequests backend, never each fixture bridge.
"""
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import asdict
from hashlib import sha256
import importlib
import json
from threading import Lock
from types import SimpleNamespace
from uuid import uuid4

from app.drive_run_state import StateError
from app.iphone_accounting_ownership import TrustedOwnershipReader, native_identity
from app.iphone_accounting_gate import SyntheticAccountingGate
from app.unified_receipt_write import ConditionalRequests, UnifiedWriteBridge
from app.unified_receipt_purposes import PurposeBoundary, VerifiedPost, VerifiedRead
from app.unified_receipt_review import digest, Case
from app.unified_receipt_native import ReceiptConfirmationPort, GroupedPlan, GroupedUnitPort
from staging_shadow.accounting_native_preflight import NativeReadAdapter, NativeValidation, verify_registered_pdf_partition
from staging_shadow.native_accounting_sandbox import NativeAccountingSandbox, NativeInvocation
from accounting_ownership_fixtures import OwnershipFixture, SourcePort, Port, NativePort
from unified_purpose_fixtures import native_modules
from test_pdf_intake_authority import OWNER
from test_unified_receipt_write import Memory,FakeNative

NOW = 1791530000


def encoded(value): return json.dumps(value,sort_keys=True,separators=(',',':')).encode()


def state_read(read):
    def read_versioned():
        value = read()
        return encoded(value), '"'+digest(value)+'"'
    return read_versioned


def reader_for(entry, source, raw, units, native, *, revision=1):
    source_port = SourcePort(dict({k:v for k,v in source.items() if k!='native_source_version'},generation=1),raw)
    partition = Port(dict(source=deepcopy(source),generation=1,revision=revision,state='confirmed' if units else 'not_applicable',
                          complete=True,units=deepcopy(units)))
    n = dict(generation=1,owner=OWNER,source=deepcopy(source),case_id='synthetic-new-case',purpose='ACCOUNTING_POST',
             action='post',native_context='',native_request_id='',native_page=None,state='ready')
    n.update(native)
    unit = next((u for u in units if u['unit_id']==n['unit_id']),None)
    n['operation_identity'] = native_identity(n,source,unit)
    catalog = NativePort({(entry,'u1'):n})
    ledger, pending = Port(dict(generation=1,complete=True,records=[])), Port(dict(generation=1,complete=True,records=[]))
    return SimpleNamespace(source=source_port,partition=partition,native=catalog,ledger=ledger,pending=pending,
        reader=TrustedOwnershipReader(source_port,partition,catalog,ledger,pending))


class Router:
    def __init__(self): self.routes = {}
    def observe(self,entry,key): return self.routes[entry,key].reader.observe(entry,key)
    @property
    def synthetic_memory_only(self):return all(route.reader.synthetic_memory_only for route in self.routes.values())


class SharedLedgerPort(Port):
    def __init__(self,rows):
        super().__init__(dict(generation=1,complete=True,records=[]));self.rows=rows
        # Exact protected legacy review scaffolds from fixture initialization,
        # not posted accounting authority and never inferred by amount/date.
        self.review_rows={title:{digest(row) for row in values} for title,values in rows.items()}
    def read(self):
        result=super().read()
        if any(digest(row) not in self.review_rows[title] for title,values in self.rows.items() for row in values):
            result['complete']=False  # New/changed native rows need explicit ownership/read-back mapping.
        return dict(result,native_rows_digest=digest(self.rows))


class SharedPendingPort(Port):
    def __init__(self,owners,checks):
        super().__init__(dict(generation=1,complete=True,records=[]));self.owners,self.checks=owners,checks
    def read(self):
        value=super().read()
        for key,check in self.checks.items():
            try:
                if check():value['records'].append(deepcopy(self.owners[key]))
            except Exception:value['complete']=False
        return value


class Probe:
    def __init__(self): self.calls=[];self.lock=Lock();self.mode=None
    def __call__(self,entry,key,operation):
        with self.lock: self.calls.append((entry,key,operation))
        if self.mode in {'lost','partial'}: raise TimeoutError('synthetic boundary response unknown')


class Suite:
    def __init__(self,server=None,*,sandbox=True,grouped_accounting=True):
        self.server=server or Memory(); self.probe=Probe(); self.router=Router(); self.adapters={};self.invocations=[]
        assert type(self.server) is Memory
        self.server.synthetic_memory_only=True
        self.after_preflight=None;self.native_reads=0
        self.grouped_accounting=grouped_accounting
        self.shared_ledger={t:[] for t in ('レシート','支出明細','取込データ')}
        self.shared_income=[]
        self.pdf=OwnershipFixture();p=self.pdf.port;r=self.pdf.runner;c=self.pdf.c
        for title in self.shared_ledger:self.pdf.db.data[title]=self.shared_ledger[title]
        self.native_states=[]
        # Shared, existing PDF manual/future source and actual native validator.
        for entry in ('pdf_manual','future_one_tab'):
            self.router.routes[entry,'u1']=self.pdf
            self.pdf.native.frame[entry,'u1']['input_snapshot']=digest(p.snapshot_reader())
            def validate(o,p=p,r=r,c=c):
                verify_registered_pdf_partition(o,r.store.load(),p.key,p.uid)
                result=p.inspect(c,'post')
                ready=r.post_manual(p.key,p.uid,p.snapshot_reader(),apply=False)
                return NativeValidation(result.operation_identity,digest(p.snapshot_reader()),result.authority_revision,
                    digest(ready),(ready['plan']['operations'][0]['rows'][0][0],))
            self.adapters[entry,'u1']=NativeReadAdapter(r.store.io.read_versioned,validate,expected_source=self.pdf.source_value,
                expected_partition=self.pdf.partition.frame,expected_case_id=self.pdf.native.frame[entry,'u1']['case_id'])
            args=(r,p.key,p.uid,p.snapshot_reader()) if entry=='pdf_manual' else ()
            kwargs={'apply':True} if entry=='pdf_manual' else {}
            self.invocations.append(NativeInvocation(entry,'u1',args,kwargs))
        self.native_states.append(lambda: [r.store.load(),self.pdf.db.data])
        self._legacy_and_auto()
        self._medical()
        self._pdf_auto()
        self._grouped()
        self._canaries()
        owners={key:route.reader.observe(*key).value() for key,route in self.router.routes.items()}
        def pdf_pending():
            pages=self.pdf.runner.store.load()['pages']
            known=pages[self.pdf.port.key]['units']
            for page in pages.values():
                if any(uid!=self.pdf.port.uid and u['intent'] is not None for uid,u in page['units'].items()):
                    raise StateError('synthetic_unmapped_native_pending')
            return known[self.pdf.port.uid]['intent'] is not None
        checks={('pdf_manual','u1'):pdf_pending,
            ('legacy_confirmation','u1'):lambda:any(i['status']=='pending' for i in self.legacy.service.items.values()),
            ('legacy_confirmation','medical'):lambda:any(i['status']=='pending' for i in self.medical.service.items.values()),
            ('grouped_unit','u1'):lambda:bool(self.grouped.store.load()['records']),
            ('normal_auto','u1'):lambda:bool(self.automatic['normal_auto'].db._import_ids),
            ('buyback_income','u1'):lambda:bool(self.automatic['buyback_income'].db._import_ids)}
        for entry,data in self.canaries.items():
            def canary_pending(data=data,entry=entry):
                from app.receipt_plan import posting,unit_posting
                value,_=data.store.read()
                (posting if entry=='fixed_canary' else unit_posting).validate_state(value)
                return value['state']!='unused'
            checks[entry,'u1']=canary_pending
        self.ledger=SharedLedgerPort(self.shared_ledger);self.pending=SharedPendingPort(owners,checks)
        for route in self.router.routes.values():
            route.reader.ledger=self.ledger;route.reader.pending=self.pending
        # The PDF aliases are used by fault-injection tests as well.
        self.pdf.ledger=self.ledger;self.pdf.pending=self.pending
        self.auth=PurposeBoundary(OWNER,self.verify,lambda context:VerifiedRead(**dict(
            dict(actor_id=OWNER,expires_at=NOW+600,session_active=True),**(context or {}))),lambda:[],clock=lambda:NOW)
        self.requests=ConditionalRequests(self.server,'synthetic-shared-financial-claims',synthetic_accounting=True)
        self.gate=SyntheticAccountingGate(self.requests,self.router,self.auth,self.preflight,self.probe,
            enabled=True,isolated_synthetic=True,clock=lambda:NOW)
        self.sandbox=NativeAccountingSandbox(self.gate,self.invocations,enabled=True,isolated_synthetic=True) if sandbox else None
        self._text=ContextVar('synthetic_local_extraction_text',default='')
        if self.sandbox:
            extraction=self.sandbox.module('receipt_text_extraction')
            privacy=self.sandbox.module('receipt_privacy_gate')
            # Input-reader replacement only: the original privacy classifier and
            # validation rules run on deterministic, wholly synthetic OCR text.
            privacy._extract_receipt_text=lambda *a,**kw:extraction._ReceiptTextExtraction('extracted','pdf_text',self._text.get())

    @staticmethod
    def verify(context,purpose,rid,binding,revision):
        fields=dict(actor_id=OWNER,purpose=purpose,request_id=rid,binding_digest=binding,
            expected_revision=revision,created_at=NOW,expires_at=NOW+600,csrf_valid=True,session_active=True)
        fields.update(context or {})
        return VerifiedPost(**fields)

    def preflight(self,entry,key,o):
        self.native_reads+=1
        result=self.adapters[entry,key](o)
        if self.after_preflight: self.after_preflight(self,entry,key)
        return result

    def call(self,entry='pdf_manual',key='u1',rid=None,revision=None,context=None):
        if revision is None: revision=self.requests.read()[0]['revision']
        return self.sandbox.call(entry,key,rid or str(uuid4()),revision,context or {})

    def snapshot_native(self): return deepcopy([read() for read in self.native_states])

    def _legacy_and_auto(self):
        from app.receipt_confirmation import ReceiptConfirmation,TITLE
        from app.receipt_plan.planning import PlanningDB
        from app.receipt_pipeline import ReceiptPipeline
        from app.receipt_reimport_production import target_snapshot
        from app.models import ReceiptResult
        from app.unified_receipt_eligibility import current_snapshot
        from test_receipt_confirmation import Store,DB
        from test_receipt_pipeline import _buyback_result,FakeDB
        pdf=self.pdf;state=pdf.runner.store.load()['pages'][pdf.port.key]['units'][pdf.port.uid]
        auto_parsed=ReceiptResult.model_validate(state['candidate']['record']['legacy']['parsed'])
        parsed=ReceiptResult.model_validate(dict(auto_parsed.model_dump(),date='2024-01-02'))
        store,db=Store(),DB()
        for title in self.shared_ledger:db.rows[title]=self.shared_ledger[title]
        db.categories=lambda:[('食費','食料品'),('日用品','洗剤')]
        legacy_raw=b'synthetic-general-legacy-source'
        legacy_source=dict(source_id='synthetic-general-legacy-source',sha256=sha256(legacy_raw).hexdigest(),drive_revision_id='legacy-drive-r1',
            drive_numeric_version='2',native_source_version='legacy-native-v1',source_kind='image',page_count=1)
        src=dict(source_id=legacy_source['source_id'],sha256=legacy_source['sha256'],version=legacy_source['native_source_version'],mime_type='image/png')
        preview=PlanningDB(db.categories())
        ReceiptPipeline(preview,None,clock=lambda:'2026-10-11 12:00:00')._materialize_result(parsed,src['source_id'],'',['synthetic_review_required'])
        for title,row in preview.plan: db.rows[title].append(deepcopy(row))
        store.value['manifest']=dict(sources=[src],folder_id='synthetic-folder')
        store.value['records'][src['source_id']]=dict(phase='complete',parsed=parsed.model_dump(),before=target_snapshot(
            dict(receipt_rows=db.rows['レシート'],expense_rows=[],import_rows=db.rows['取込データ'],review_rows=[]),src['source_id']))
        service=ReceiptConfirmation(store,db,lambda *_:dict(version=src['version']))
        service.prepare_general();service.render();db.rows[TITLE][0][12]='候補明細で確定';service.capture_inputs()
        rid=next(iter(service.items));item=service.items[rid]
        case=Case(rid,'receipt-confirmation-v1',dict(source_file_id=src['source_id'],source_content_hash=src['sha256'],
            source_version=src['version'],review_id=rid),[],TITLE,'normal',authority_status='waiting')
        markers=('date','merchant','amount','category','payment','action','duplicate_target','memo')
        case.fields=[[k,k,v] for k,v in zip(markers,item['inputs'])]
        native=ReceiptConfirmationPort(service,rid,lambda *_:None)
        self.legacy=SimpleNamespace(service=service,store=store,db=db,case=case,port=native)
        entry='legacy_confirmation'
        own=reader_for(entry,legacy_source,legacy_raw,[],dict(namespace='receipt_confirmation',scope='whole',unit_id=None,
            receipt_index=None,input_snapshot=digest(current_snapshot(case)),authority_revision=1,ledger_targets=['R-'+src['source_id']]))
        own.native.frame[entry,'u1']['case_id']=case.case_id
        self.router.routes[entry,'u1']=own
        def legacy(o,entry=entry):
            if service.items[rid]['status']!='waiting': raise StateError('synthetic_native_pending_held')
            result=native.validate_draft(case,case)
            shadow=deepcopy(service)
            if shadow.apply_confirmations(dry_run=True,keys=[rid])!=1:
                raise StateError('synthetic_native_legacy_full_validation_held')
            return NativeValidation(native_identity(own.native.frame[entry,'u1'],legacy_source,None),
                digest(current_snapshot(case)),1,digest(result),('R-'+src['source_id'],))
        self.adapters[entry,'u1']=NativeReadAdapter(state_read(lambda:[store.value,db.rows]),legacy,expected_source=legacy_source,
            expected_partition=own.partition.frame,expected_case_id=case.case_id)
        self.invocations.append(NativeInvocation(entry,'u1',(service,),{'keys':[rid]}))
        # Future financial front door for exactly the same native operation.
        own.native.frame['future_one_tab','legacy']=deepcopy(own.native.frame[entry,'u1'])
        self.router.routes['future_one_tab','legacy']=own
        self.adapters['future_one_tab','legacy']=self.adapters[entry,'u1']
        self.invocations.append(NativeInvocation('future_one_tab','legacy',(),{}))
        self.native_states.append(lambda:[store.value,db.rows])
        self.auto_text={'normal_auto':'レシート 商品 小計 730円 現金 730円',
                        'buyback_income':'買取\n合計 155円\n現金 155円'}
        self.auto_results={'normal_auto':auto_parsed,'buyback_income':_buyback_result()}
        self.automatic={}
        auto_source_id=pdf.c.source_id
        for entry in ('normal_auto','buyback_income'):
            auto_db=FakeDB()
            auto_db.income_rows=self.shared_income
            auto_db.import_ids=lambda db=auto_db:set(db._import_ids)|{r[0] for r in self.shared_ledger['取込データ']}
            auto_db.receipt_ids=lambda db=auto_db:set(db._receipt_ids)|{r[0] for r in self.shared_ledger['レシート']}
            auto_db.expense_index=lambda:{r[0]:i for i,r in enumerate(self.shared_ledger['支出明細'],2)}
            auto_db.categories=(lambda:[('食費','食品')]) if entry=='buyback_income' else db.categories
            pipeline=ReceiptPipeline(auto_db,None)
            self.automatic[entry]=SimpleNamespace(db=auto_db,pipeline=pipeline)
            self.router.routes[entry,'u1']=pdf
            n=pdf.native.frame[entry,'u1'];n['input_snapshot']=digest(self.auto_results[entry].model_dump())
            if entry=='buyback_income':
                from app.bank_income import income_id
                n['ledger_targets'].append(income_id('receipt:'+auto_source_id))
            def automatic(o,entry=entry,auto_db=auto_db,pipeline=pipeline):
                from app.receipt_validation import validate_receipt_result
                privacy=self.sandbox.module('receipt_privacy_gate')
                token=self._text.set(self.auto_text[entry])
                try: gate=privacy.evaluate_receipt_privacy(pdf.source.raw,'application/pdf',
                    known_source_classification=pipeline._source_privacy.get(pdf.c.source_id))
                finally:self._text.reset(token)
                result=self.auto_results[entry]
                categories=[('食費','食品')] if entry=='buyback_income' else db.categories()
                if (gate.classification!='normal' or not gate.gemini_allowed
                        or not validate_receipt_result(result,categories)[0]
                        or ReceiptPipeline._kind_issues(result,gate)):
                    raise StateError('synthetic_native_privacy_or_validation_held')
                if 'receipt:'+auto_source_id in auto_db.import_ids():
                    raise StateError('synthetic_native_already_imported')
                income=None
                if result.transaction_kind=='buyback':
                    from app.utils import canonical_hash
                    income=pipeline._buyback_income('receipt:'+auto_source_id,result,canonical_hash(result.model_dump()))
                # Same legacy namespace and independent native input, never a
                # fake Unit or a grant from the owner acknowledgement.
                return NativeValidation(native_identity(pdf.native.frame[entry,'u1'],pdf.source_value,None),
                    digest(result.model_dump()),1,digest([result.model_dump(),gate.model_dump(),income]),
                    ('R-'+auto_source_id,)+( (income[0][0],) if income is not None else () ))
            read=state_read(lambda auto_db=auto_db,pipeline=pipeline,entry=entry:dict(
                import_ids=sorted(auto_db.import_ids()),receipts=sorted(auto_db.receipt_ids()),expenses=auto_db.expense_index(),
                income=auto_db.income_rows,source_privacy=pipeline._source_privacy,parsed=self.auto_results[entry].model_dump()))
            self.adapters[entry,'u1']=NativeReadAdapter(read,automatic,expected_source=pdf.source_value,
                expected_partition=pdf.partition.frame,expected_case_id=pdf.native.frame[entry,'u1']['case_id'])
            self.native_states.append(lambda auto_db=auto_db,pipeline=pipeline:dict(import_ids=sorted(auto_db.import_ids()),
                income=auto_db.income_rows,appends=auto_db.append_calls,source_privacy=pipeline._source_privacy))
            self.invocations.append(NativeInvocation(entry,'u1',(object(),pdf.source.raw,'application/pdf',auto_source_id),{}))

    def _medical(self):
        from app.receipt_confirmation import ReceiptConfirmation
        from app.unified_receipt_eligibility import current_snapshot
        from test_receipt_confirmation import Store,DB,confirm
        raw=b'synthetic-owner-manually-entered-medical-source'
        source=dict(source_id='synthetic-manual-medical-source',sha256=sha256(raw).hexdigest(),drive_revision_id='medical-drive-r1',
            drive_numeric_version='3',native_source_version='medical-native-v1',source_kind='image',page_count=1)
        src=dict(source_id=source['source_id'],sha256=source['sha256'],version=source['native_source_version'],mime_type='image/png')
        store,db=Store(),DB();service=ReceiptConfirmation(store,db,lambda *_:dict(version=src['version']))
        for title in self.shared_ledger:db.rows[title]=self.shared_ledger[title]
        service.observe_medical(src,'synthetic-medical-folder');service.render();confirm(db);service.capture_inputs()
        rid=next(iter(service.items));item=service.items[rid]
        case=Case(rid,'receipt-confirmation-v1',dict(source_file_id=src['source_id'],source_content_hash=src['sha256'],
            source_version=src['version'],review_id=rid),[],'領収書確認','medical',authority_status='waiting')
        markers=('date','merchant','amount','category','payment','action','duplicate_target','memo')
        case.fields=[[k,k,v] for k,v in zip(markers,item['inputs'])]
        port=ReceiptConfirmationPort(service,rid,lambda *_:None)
        # reader_for's catalog key is explicitly remapped at initialization;
        # existing source identities/ledger are never rewritten during requests.
        own=reader_for('legacy_confirmation',source,raw,[],dict(namespace='receipt_confirmation',scope='whole',unit_id=None,
            receipt_index=None,input_snapshot=digest(current_snapshot(case)),authority_revision=1,ledger_targets=['R-'+src['source_id']]))
        own.native.frame['legacy_confirmation','medical']=own.native.frame.pop(('legacy_confirmation','u1'))
        own.native.frame['legacy_confirmation','medical']['case_id']=case.case_id
        self.router.routes['legacy_confirmation','medical']=own
        def validate(o):
            checked=port.validate_draft(case,case);shadow=deepcopy(service)
            if shadow.apply_confirmations(dry_run=True,keys=[rid])!=1:raise StateError('synthetic_native_medical_held')
            return NativeValidation(native_identity(own.native.frame['legacy_confirmation','medical'],source,None),
                digest(current_snapshot(case)),1,digest([checked['status'],service._parsed(service.items[rid]).model_dump(),current_snapshot(case)]),('R-'+src['source_id'],))
        self.adapters['legacy_confirmation','medical']=NativeReadAdapter(state_read(lambda:[store.value,db.rows]),validate,expected_source=source,
            expected_partition=own.partition.frame,expected_case_id=case.case_id)
        self.invocations.append(NativeInvocation('legacy_confirmation','medical',(service,),dict(keys=[rid])))
        self.native_states.append(lambda:[store.value,db.rows]);self.medical=SimpleNamespace(service=service,store=store,db=db,own=own)

    def _pdf_auto(self):
        from app.receipt_item_review import fields
        runner=self.pdf.runner;key=self.pdf.port.key;uid=self.pdf.port.uid;db=self.pdf.db
        page=runner.store.load()['pages'][key]['page']
        unit=runner.store.load()['pages'][key]['units'][uid]
        source=self.pdf.source_value;units=self.pdf.partition.frame['units']
        record=unit['candidate']['record'];snap=digest(fields(record))
        own=self.pdf;own.native.frame['pdf_auto','u1']['input_snapshot']=snap
        self.router.routes['pdf_auto','u1']=own
        def validate(o):
            verify_registered_pdf_partition(o,runner.store.load(),key,uid)
            current=runner.store.load()['pages'][key]['units'][uid]
            if current['intent'] is not None: raise StateError('synthetic_native_pending_held')
            result=runner.post_normal(key,uid,apply=False)
            if result.get('status')!='ready_to_write':raise StateError('synthetic_native_auto_held')
            return NativeValidation(native_identity(own.native.frame['pdf_auto','u1'],source,units[0]),
                digest(fields(current['candidate']['record'])),page['authority_revision'],digest(result),
                (result['plan']['operations'][0]['rows'][0][0],))
        self.adapters['pdf_auto','u1']=NativeReadAdapter(runner.store.io.read_versioned,validate,expected_source=source,
            expected_partition=own.partition.frame,expected_case_id=own.native.frame['pdf_auto','u1']['case_id'])
        self.invocations.append(NativeInvocation('pdf_auto','u1',(runner,key,uid),{'apply':True}))
        self.native_states.append(lambda:[runner.store.load(),db.data]);self.auto_pdf=SimpleNamespace(runner=runner,key=key,uid=uid,db=db,own=own)

    def _grouped(self):
        # Existing native schema/route and grouped materializer plan validator.
        # The fixture initializes an independently confirmed grouping snapshot;
        # the accounting probe does not confirm or update it.
        n=native_modules();processing=importlib.import_module('app._unified_pinned.pdf_unit_processing')
        from app.page_receipt_model import SourceRef,stable_page
        from app.pdf_intake_authority import review_identity
        source=deepcopy(self.pdf.source_value);source['page_count']=2
        page=deepcopy(self.pdf.runner.store.load()['pages'][self.pdf.port.key]['page'])
        page['source']['page_count']=2
        page['stable_page_identity']=stable_page(SourceRef(**page['source']),1)
        page['review_identity']=review_identity(page)
        grouping_io=Memory();group_binding=digest(['synthetic-grouping-state'])
        grouping_io.raw=n.pdf_grouping_authority.encoded(n.pdf_grouping_authority.empty_state(group_binding))
        observations=n.receipt_pdf_units.PdfObservations(source['source_id'],source['sha256'],tuple(
            n.receipt_pdf_units.PageObservation(source['source_id'],source['sha256'],number,digest(['synthetic-render',number]),
                'extracted','normal','normal',effective_render_scale=1.,observation_complete=True,render_attempts=1) for number in (1,2)))
        grouping=n.pdf_grouping_authority.DurablePdfGrouping(n.pdf_grouping_authority.DriveGroupingStore(grouping_io,group_binding),
            lambda *_:observations,proposer=n.receipt_pdf_grouping.AdjacentPageGrouping(lambda o:[n.receipt_pdf_grouping.PageEvidence() for _ in o.pages]),
            clock=lambda:'2026-10-09T08:00:00+00:00')
        proposal=grouping.display(source['source_id'])['proposal']
        grouping.review(dict(request_id=str(uuid4()),source_file_id=source['source_id'],source_content_hash=source['sha256'],
            proposal_digest=proposal['proposal_digest'],grouping_revision=proposal['grouping_version'],operation='edit',partition=[[1,2]]))
        proposal=grouping.display(source['source_id'])['proposal']
        grouping.review(dict(request_id=str(uuid4()),source_file_id=source['source_id'],source_content_hash=source['sha256'],
            proposal_digest=proposal['proposal_digest'],grouping_revision=proposal['grouping_version'],operation='confirm',partition=None))
        confirmed=grouping.store.load()['records'][digest(source['source_id'])]['confirmation']
        spec=processing.unit_spec(source['source_id'],source['sha256'],2,[1,2],['normal']*2,['normal']*2,revision=proposal['grouping_version'],
            proposal_digest=proposal['proposal_digest'],confirmation_digest=confirmed['confirmation_digest'],kind_digests=['']*2)
        units=[dict(unit_id=spec['unit_id'],receipt_index=1,members=[dict(page=number,
            identity=stable_page(SourceRef(**page['source']),number),box=[0,0,1,1]) for number in (1,2)])]
        from app.pdf_intake_runner import ledger_snapshot,exact_readback
        from app.receipt_plan.planning import check_duplicates
        from app.receipt_plan.planning import PlanningDB
        from app.receipt_pipeline import ReceiptPipeline
        from app.models import ReceiptResult
        from test_pdf_intake_runner import DB
        db=DB()
        for title in self.shared_ledger:db.data[title]=self.shared_ledger[title]
        categories=db.categories();parsed=self.auto_results['normal_auto'].model_dump()
        planning=PlanningDB(categories)
        ReceiptPipeline(planning,None,clock=lambda:'2026-10-11 12:00:00')._materialize_result(
            ReceiptResult.model_validate(parsed),spec['unit_id'],'',[])
        operations=[dict(table=t,rows=[row for title,row in planning.plan if title==t]) for t in ('レシート','支出明細','取込データ')]
        expected=dict(operations=operations,digest=digest(operations))
        from app.unified_receipt_eligibility import current_snapshot
        case=deepcopy(self.pdf.c)
        case.identity.update(receipt_unit_id=spec['unit_id'],page_numbers=[1,2],page_identities=spec['member_page_identities'],
            stable_page_identity=spec['member_page_identities'][0],owner_actor_id=OWNER)
        case.fields=[['date','日付',parsed['date']],['merchant','店舗',parsed['merchant']],
            ['items','全明細',json.dumps(parsed['items'],ensure_ascii=False,sort_keys=True)],['unit','Unit',spec['unit_id']]]
        grouped=GroupedPlan(spec,digest(current_snapshot(case)),digest(['synthetic-writer-reference']),parsed,
            spec['unit_id'],'','2026-10-11 12:00:00',categories,expected,digest(ledger_snapshot(db)))
        transport_module=importlib.import_module('app._unified_pinned.conditional_drive_state_v2')
        class Transport(transport_module.ConditionalDriveStateTransportV2):
            def __init__(self): self.io=Memory()
            def read_versioned(self): return self.io.read_versioned()
            def replace_versioned(self,*args): return self.io.replace_versioned(*args)
        transport=Transport();binding=digest(['synthetic-grouped-processing-state'])
        transport.io.raw=processing.encoded(processing.empty_state(binding))
        store=processing.DriveUnitProcessingStore(transport,binding,preflight=lambda:None)
        def verify_current(current):
            values=grouping.confirmed_units(source['source_id'])
            record=grouping.store.load()['records'][digest(source['source_id'])]
            return (current==spec and len(values)==1 and list(values[0].page_numbers)==spec['page_numbers']
                and values[0].grouping_version==spec['grouping_revision']
                and values[0].proposal_digest==spec['proposal_digest']
                and values[0].source_content_hash==spec['source_content_hash']
                and set(values[0].page_classifications)=={'normal'}
                and record['confirmation'] is not None
                and record['confirmation']['confirmation_digest']==spec['confirmation_digest'])
        # Independent synthetic ACCOUNTING evidence, not the STRUCTURE
        # confirmation/revision above. Use the existing native eligibility gate.
        accounting=FakeNative(case,target=digest(['pdf-unit-processing',spec['unit_id']]))
        accounting.expiry=NOW+600
        if not self.grouped_accounting:accounting.e['permissions']['posting']='denied'
        port=GroupedUnitPort(store,db,lambda _:grouped,verify_current,lambda *_:accounting.inspect(case,'post'))
        inspector=UnifiedWriteBridge(adapters={(case.kind,'post'):port},clock=lambda:NOW)
        own=reader_for('grouped_unit',source,self.pdf.source.raw,units,dict(namespace='pdf_unit_processing',scope='unit',
            unit_id=spec['unit_id'],receipt_index=1,input_snapshot=grouped.input_digest,authority_revision=accounting.revision,
            ledger_targets=['R-'+grouped.ledger_source_id]),revision=spec['grouping_revision'])
        self.router.routes['grouped_unit','u1']=own
        def validate(o):
            processing._spec(spec)
            rows={x['table']:deepcopy(x['rows']) for x in expected['operations']}
            processing._route(spec,'receipt',rows,grouped.writer_reference)
            if store.load()['records']:raise StateError('synthetic_native_grouped_pending')
            inspected,_=inspector._inspection(case,'post')
            if not inspected.owner_confirmed or case.identity['owner_actor_id']!=o.owner:
                raise StateError('synthetic_native_independent_accounting_authority_required')
            checked=port._plan(case)
            if digest(ledger_snapshot(db))!=grouped.before_ledger_digest or exact_readback(db,expected)!='not_written':
                raise StateError('synthetic_native_grouped_ledger_changed')
            check_duplicates(db,spec['unit_id'],ReceiptResult.model_validate(parsed))
            return NativeValidation(inspected.operation_identity,checked.input_digest,inspected.authority_revision,
                digest(asdict(checked)),(checked.expected['operations'][0]['rows'][0][0],))
        own.native.frame['grouped_unit','u1']['case_id']=case.case_id
        self.adapters['grouped_unit','u1']=NativeReadAdapter(state_read(lambda:[store.load(),grouping.store.load(),db.data,
            asdict(accounting.inspect(case,'post'))]),validate,expected_source=source,
            expected_partition=own.partition.frame,expected_case_id=case.case_id)
        self.invocations.append(NativeInvocation('grouped_unit','u1',(port,{}),{}))
        self.native_states.append(lambda:[store.load(),grouping.store.load(),db.data]);self.grouped=SimpleNamespace(
            port=port,store=store,transport=transport,spec=spec,own=own,grouping=grouping,accounting=accounting,inspector=inspector)

    def _canaries(self):
        # Fresh, fully synthetic native fixture initialization with real bytes
        # hash and owner. Default historical regression fixtures are unchanged.
        from test_p14_plan_only import Readers,RID,Queue
        from test_receipt_unit_posting import Readers as UnitReaders,Store as UnitStore
        from test_p14_posting import Store,Writer,Projection,ATTEMPT
        from test_receipt_unit_plan import CONTEXT
        from app.receipt_plan import runner,unit_runner,posting,unit_posting
        raw=b'%PDF-new-synthetic-fixed-and-unit-fixture'
        source_hash=sha256(raw).hexdigest()
        fixed=Readers(source_hash=source_hash,actor_owner=OWNER);unit=UnitReaders(source_hash=source_hash,actor_owner=OWNER)
        from app.sheets import HEADERS
        for rd in (fixed,unit):
            original=rd.sheet_rows
            def shared_rows(region,*,render='UNFORMATTED_VALUE',original=original):
                for title in self.shared_ledger:
                    if region.startswith("'"+title+"'!"):
                        return deepcopy([HEADERS[title]]+self.shared_ledger[title])
                return original(region,render=render)
            rd.sheet_rows=shared_rows
        fixed.queue=Queue();runner.execute(fixed,fixed.queue,RID)
        unit.queue=Queue();unit_runner.execute(unit,unit.queue,RID,CONTEXT)
        fixed_store=Store(posting.approval(runner.prepare(fixed,RID),RID,OWNER,
            approval_reference='synthetic-fixed-only',approved_at=NOW-1,expires_at=NOW+600))
        unit_store=UnitStore(unit_posting.approval(unit_runner.prepare(unit,RID),RID,OWNER,CONTEXT,
            approval_reference='synthetic-unit-only',approved_at=NOW-1,expires_at=NOW+600))
        source=dict(source_id=fixed.snapshot['identity']['source_file_id'],sha256=source_hash,drive_revision_id='canary-drive-r1',
            drive_numeric_version='17',native_source_version='canary-native-v1',source_kind='pdf',page_count=14)
        units=[dict(unit_id=rd.snapshot['identity']['receipt_unit_id'],receipt_index=1,members=[dict(
            page=rd.snapshot['identity']['page_number'],identity=rd.snapshot['identity']['stable_page_identity'],box=[0,0,1,1])]) for rd in (unit,fixed)]
        self.canaries={}
        for entry,rd,store,native,context_id in [('fixed_canary',fixed,fixed_store,posting,'synthetic-fixed-native-context'),
                                               ('explicit_unit',unit,unit_store,unit_posting,CONTEXT)]:
            writer,projection=Writer(rd),Projection();uid=rd.snapshot['identity']['receipt_unit_id']
            own=reader_for(entry,source,raw,units,dict(namespace='p14_posting' if entry=='fixed_canary' else 'receipt_unit_posting',native_context=context_id,native_request_id=RID,
                scope='unit',unit_id=uid,receipt_index=1,input_snapshot=digest(rd.snapshot),authority_revision=2,ledger_targets=['R-'+uid]))
            def validate(o,rd=rd,store=store,native=native,context_id=context_id,own=own,entry=entry):
                value,_=store.read();native.validate_state(value)
                if value['state']!='unused' or not value['grant']['approved_at']<=NOW<value['grant']['expires_at']:
                    raise StateError('synthetic_native_canary_claimed_or_expired')
                current=native.fresh_plan(rd,RID)
                if native is unit_posting:
                    native.bound(value['grant'],current,RID,OWNER,context_id);native.plan_receipt(rd,current,RID,context_id)
                else:native.bound(value['grant'],current,RID,OWNER)
                if posting.outcome(rd,current,value['grant'])[0]!='not_written':raise StateError('synthetic_native_canary_ledger_changed')
                return NativeValidation(native_identity(own.native.frame[entry,'u1'],source,next(u for u in units if u['unit_id']==rd.snapshot['identity']['receipt_unit_id'])),
                    digest(rd.snapshot),2,digest(current),tuple(row[0] for title,row in current['rows'] if title=='レシート'))
            read=state_read(lambda rd=rd,store=store:[store.read(),rd.journal,self.shared_ledger,rd.queue.data])
            self.adapters[entry,'u1']=NativeReadAdapter(read,validate,expected_source=source,
                expected_partition=own.partition.frame,expected_case_id=own.native.frame[entry,'u1']['case_id']);self.router.routes[entry,'u1']=own
            args=(rd,store,writer,projection,RID)+( (context_id,) if entry=='explicit_unit' else () )
            self.invocations.append(NativeInvocation(entry,'u1',args,dict(attempt_id=ATTEMPT,now=NOW,operation='apply')))
            self.native_states.append(lambda rd=rd,store=store,writer=writer:[store.read(),rd.tables,rd.queue.data,writer.calls])
            self.canaries[entry]=SimpleNamespace(readers=rd,store=store,writer=writer,own=own)
