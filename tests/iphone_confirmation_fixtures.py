"""Eight invented cases, real validators, pinned Google host, fake CAS only."""
from copy import deepcopy
from dataclasses import asdict, replace
from hashlib import sha256
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from cryptography.fernet import Fernet

from app.iphone_confirmation import ConfirmationService, NativeDraftContext
from app.iphone_confirmation_host import make_isolated_confirmation_application, PREFIX
from app.iphone_draft_journal import LocalJournal
from app.iphone_item_drafts import issue
from app.iphone_readonly_host import SavedDraftRepository
from app.iphone_sheet_reader import CASE_IDS
from app.page_receipt_model import SourceRef, stable_page
from app.receipt_confirmation import ReceiptConfirmation
from app.unified_receipt_review import Case, digest
from app.unified_receipt_eligibility import current_snapshot
from app.unified_receipt_shadow import ShadowConfig
from app.unified_receipt_native import ReceiptConfirmationPort
from iphone_persistent_fixtures import AtomicDocument, repository
from iphone_readonly_fixtures import saved_bundle
from test_iphone_review_preview import from_case, structure_fixture
from test_receipt_confirmation import Store, DB, confirm
from test_unified_receipt_write import pdf_native
from unified_purpose_fixtures import OWNER
from unified_shadow_fixtures import HostRig, ORIGIN


class SourceReader:
    def __init__(self, observation, raw):
        self.frame = {k:deepcopy(v) for k,v in observation.items() if k not in {'observed_at','snapshot_digest','input_snapshot'}}
        self.raw, self.calls = raw, 0
    def read_frame(self):
        self.calls += 1; return deepcopy(self.frame)
    def read_revision_bytes(self, fid, revision):
        self.calls += 1
        assert (fid,revision) == (self.frame['source_id'],self.frame['drive_revision_id'])
        return self.raw


def contexts():
    result = {}
    for n,cid in enumerate(['SYN-G101','SYN-G102','SYN-G103'],1):
        case, _, port, _, db, runner = pdf_native(index=n,count=3)
        case.synthetic = True
        record = runner.store.load()['pages'][port.key]['units'][port.uid]['candidate']['record']
        saved = port.snapshot_reader()
        canonical = from_case(case,'normal'); canonical['inputs']['category'] = ''
        ctx = NativeDraftContext(canonical,case,None,record,saved,db.categories())
        result[cid] = dict(context=ctx,raw=b'%PDF-FAKE-UNIT-WRITER',db=db,store=runner.store,port=port)
    for cid in ['SYN-M101','SYN-M102']:
        raw = ('synthetic-manual-original:'+cid).encode()
        src = dict(source_id='synthetic-manual-'+cid, sha256=sha256(raw).hexdigest(),version='native-medical-1',mime_type='application/pdf')
        store,db = Store(),DB()
        service = ReceiptConfirmation(store,db,lambda *args:dict(version=src['version']))
        service.observe_medical(src,'synthetic-only-folder'); service.render(); confirm(db)
        service.capture_inputs(); key,item = next(iter(service.items.items()))
        page = stable_page(SourceRef(source_file_id=src['source_id'],source_content_hash=src['sha256'],page_count=1),1)
        identity = dict(source_file_id=src['source_id'],source_content_hash=src['sha256'],source_version=src['version'],
            stable_page_identity=page,page_number=1,page_numbers=[1],page_identities=[page],
            receipt_unit_id='synthetic-manual-unit-'+cid,receipt_index=1,review_id=key)
        fields = [[k,k,v] for k,v in zip(('date','merchant','amount','category','payment','action','duplicate_target','memo'),item['inputs'])]
        case = Case(key,'synthetic-medical-case-v1',identity,fields,'synthetic','medical',synthetic=True)
        canonical = from_case(case,'medical')
        if cid=='SYN-M102': canonical['inputs']['amount']=''
        port = ReceiptConfirmationPort(service,key,lambda *a:None)
        result[cid] = dict(context=NativeDraftContext(canonical,case,None,categories=db.categories(),medical_port=port),
                           raw=raw,db=db,store=store,port=port)
    for cid in ['SYN-P201','SYN-P202']:
        f,observation,row = structure_fixture()
        identity = deepcopy(row['identity']); identity.update(stable_page_identity=identity['page_identities'][0],page_number=1)
        case = Case(cid,'synthetic-structure-case-v1',identity,[], 'synthetic','grouping',synthetic=True)
        row['case_id']=case.case_id
        observation=f.observe(case.case_id,f.purpose,f.action,f.rid)
        def read_proposal(f=f):
            return deepcopy(next(iter(f.grouping.store.load()['records'].values()))['proposal'])
        result[cid] = dict(context=NativeDraftContext(row,case,None,structure_observation=observation,structure_request_id=f.rid,
                           structure_proposal=deepcopy(f.proposal),structure_validator=f.native.receipt_pdf_grouping._valid_proposal,
                           structure_reader=read_proposal),
                           raw=f.raw,fixture=f,port=f.port)
    return result


class Rig:
    def __init__(self, monkeypatch, tmp_path, *, enabled=True):
        self.native = contexts()
        self.bundle,self.batch = saved_bundle(coobserved=True,native_contexts=self.native)
        self.h = HostRig(monkeypatch,enabled=False)
        self.key=Fernet.generate_key(); self.proof_key=Fernet.generate_key(); self.doc=AtomicDocument()
        self.journal=repository(self.doc,self.key,self.batch)
        self.path=tmp_path/'display.json'; self.path.write_text(json.dumps(self.bundle),encoding='utf8')
        local=LocalJournal(tmp_path/'legacy.json',self.batch.contract_digest)
        _,tag=local.initialize();local.save(self.batch,tag,str(uuid4()));state,tag=local.read_versioned()
        details={cid:{k:deepcopy(m[k]) for k in ('source_observation','item_draft') if k in m} for cid,m in self.bundle['mappings'].items()}
        self.doc.payload=self.journal.migration_payload(state,tag,details=details)
        for cid,n in self.native.items():
            n['context'].source_reader=SourceReader(self.bundle['mappings'][cid]['source_observation'],n['raw'])
        self.saved=SavedDraftRepository(self.journal,self.path,digest(self.bundle),OWNER)
        self.service=ConfirmationService(self.saved,{c:n['context'] for c,n in self.native.items()},self.proof_key,clock=lambda:self.h.now)
        self.h.app=self.application(enabled=enabled);self.h.app.testing=True;self.h.client=self.h.app.test_client()
        def forbidden(*args,**kwargs):
            raise AssertionError('confirmation_must_never_mutate_native_or_execute')
        for n in self.native.values():
            monkeypatch.setattr(n['port'],'execute',forbidden)
            if 'db' in n:
                for method in ('append_raw','update_row_raw','set_raw_range'):
                    if hasattr(n['db'],method):monkeypatch.setattr(n['db'],method,forbidden)
                monkeypatch.setattr(n['store'],'save',forbidden)
            if 'fixture' in n:
                monkeypatch.setattr(n['fixture'].grouping,'review',forbidden)
        monkeypatch.setattr(self.h.f.port,'execute',forbidden)

    def application(self, *, enabled=True):
        return make_isolated_confirmation_application(self.h.web.make_application,self.h.configured,self.service,
            config=ShadowConfig(enabled,'isolated_synthetic'),cookies={'login':self.h.web.LOGIN_COOKIE,'ticket':self.h.web.COOKIE})

    def form(self,cid='SYN-G101',*,reconfirm=False):
        import re
        response=self.h.get(PREFIX+'/cases/'+cid+('/reconfirm' if reconfirm else ''))
        assert response.status_code==200,response.get_data(as_text=True)
        html=response.get_data(as_text=True)
        value=json.loads(re.search(r'<script id="form-context" type="application/json">(.*?)</script>',html,re.S)[1])
        csrf=re.search(r'data-csrf="([^"]+)"',html)[1]
        return value['request'],csrf,response

    def edits(self,cid='SYN-G101',*,entered=True):
        ledger=self.service.read()[2];draft=self.service.draft(cid,ledger)['value']
        return dict(fields=deepcopy(draft['fields']),items=[dict(item_id=e['item_id'],name=e['proposed']['name'],
            amount=e['proposed']['amount'],category=e['proposed']['category'],state='entered' if entered else e['state'],note=e['note'])
            for e in draft['item_draft']['entries']] if draft['item_draft'] else [])

    def post(self,payload,csrf,**kw):
        return self.h.client.post(PREFIX+'/cases/'+payload['case_id']+'/request',json=payload,base_url=ORIGIN,
                                  headers={'Origin':ORIGIN,'X-CSRF-Token':csrf,**kw})

    def save(self,cid='SYN-G101',*,entered=True):
        payload,csrf,_=self.form(cid);payload['edits']=self.edits(cid,entered=entered)
        return self.post(payload,csrf)

    def accept(self,cid='SYN-G101'):
        payload,csrf,_=self.form(cid,reconfirm=True);payload['acknowledged']=True
        return self.post(payload,csrf),payload,csrf
