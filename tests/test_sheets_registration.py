from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from threading import Barrier

import pytest

from app.drive_run_state import StateError
from app.receipt_confirmation_contract import load
from sheets_only_fixtures import no_clients, evaluate
from registration_fixtures import Rig


@pytest.fixture(autouse=True)
def clients(monkeypatch):
    no_clients(monkeypatch)


@pytest.fixture
def r(monkeypatch,tmp_path):
    r=Rig(monkeypatch,tmp_path)
    yield r
    r.assert_safe()


@pytest.mark.parametrize('cid',['SYN-G101','SYN-M101'])
def test_single_registration_no_comparison_owner_post_or_daily_session(r,cid):
    r.set(cid,'memo','登録の合成メモ')
    assert r.worker.poll()==[]
    # Existing browser session may expire. Standing policy does not depend on it.
    r.old.h.now+=601
    result=r.register(cid)
    assert result['status']=='VALIDATED_UNPOSTED',result
    assert result['standing_policy_verified'] and not result['per_transaction_approval_required']
    assert not result['accounting_permission'] and not result['financial_boundary_connected']
    assert result['writer_dispatches']==result['authority_dispatches']==result['accounting_writes']==0
    assert '登録検証済み・未記帳' in evaluate(r.sheet,cid)
    assert '記帳完了ではありません' in evaluate(r.sheet,cid)
    options=r.sheet.row(cid)[1]['dataValidation']['condition']['values']
    assert not any('比較' in v['userEnteredValue'] or '確定' in v['userEnteredValue'] for v in options)
    r.worker=r.rebuild()
    before=r.old.journal.read_versioned(),r.draft_writes(),r.sheet.writes
    for _ in range(3): assert r.worker.poll()[-1]==result
    assert (r.old.journal.read_versioned(),r.draft_writes(),r.sheet.writes)==before


@pytest.mark.parametrize('field',['active','permissions_verified','revision','acl_digest','expires_at','executor','destination','writer','workbook','owner','source','kind','max_amount'])
def test_changed_revoked_unverified_expired_or_out_of_scope_policy_denied(r,field):
    def change(v):
        if field in {'active','permissions_verified'}:v[field]=False
        elif field=='revision':v[field]+=1
        elif field=='expires_at':v[field]=r.old.h.now
        elif field=='acl_digest':v[field]='f'*64
        elif field in r.runtime:v['runtime'][field]='wrong'
        elif field=='source':v['routes']['SYN-G101']['source_id']='foreign'
        elif field=='kind':v['routes']['SYN-G101']['kind']='medical'
        elif field=='max_amount':v['routes']['SYN-G101']['max_amount']=1
    r.policy(change)
    before=r.draft_writes()
    assert r.register()['status']=='PERMISSION_UNVERIFIED'
    assert r.draft_writes()==before


def test_sync_settles_new_snapshot_and_never_registers_partial_inputs(r):
    r.request('SYN-G101','登録 1')
    assert r.worker.poll()[-1]['status']=='SYNC_WAIT'
    r.old.h.now+=3
    r.set('SYN-G101','memo','late sync')
    assert r.worker.poll()[-1]['status']=='SYNC_WAIT'
    assert r.draft_writes()==0
    r.old.h.now+=3
    assert r.worker.poll()[-1]['status']=='VALIDATED_UNPOSTED'


def test_partial_medical_manual_inputs_saved_held_then_complete(r):
    r.set('SYN-M101','amount','')
    r.set('SYN-M101','memo','手入力途中')
    assert r.register('SYN-M101')['status']=='HELD'
    assert r.old.old.ui._draft('SYN-M101')[0]['value']['fields']['memo']=='手入力途中'
    r.set('SYN-M101','amount',135)
    assert r.register('SYN-M101',2)['status']=='VALIDATED_UNPOSTED'


@pytest.mark.parametrize('cid',['SYN-G102','SYN-G103','SYN-M102','SYN-H301','SYN-P201','SYN-P202'])
def test_pdf_and_previous_holds_preserved(r,cid):
    before=r.sheet.core(),r.draft_writes()
    assert r.register(cid)['status']=='HELD'
    assert (r.sheet.core(),r.draft_writes())==before


def test_duplicate_and_old_confirmation_commands_cannot_mint_request(r):
    assert r.register()['status']=='VALIDATED_UNPOSTED'
    n=r.draft_writes()
    r.set('SYN-G101','amount',999)
    assert '入力変更' in evaluate(r.sheet,'SYN-G101')
    assert r.register(generation=2)['status']=='HELD'
    r.request('SYN-G101','確定 deadbeef')
    assert r.worker.poll()[-1]['status']=='CONFLICT'
    assert r.draft_writes()==n


@pytest.mark.parametrize('attack',['source_bytes','source_revision','native','case_id','category','other_input','permission'])
def test_changes_after_intent_fail_closed_no_retry(r,monkeypatch,attack):
    from draft_runtime_fixtures import set_value
    original=r.worker._save
    def save(*a,**k):
        result=original(*a,**k)
        if a[3]=='attempt':
            c=r.old.old.native['SYN-G101']
            if attack=='source_bytes':c.readers.source_bytes.raw=b'changed'
            elif attack=='source_revision':
                v=load(c.readers.source.raw);v['source']['drive_revision_id']='changed';set_value(c.readers.source,v)
            elif attack=='native':
                v=load(c.readers.draft.raw);v['revision']+=1;set_value(c.readers.draft,v)
            elif attack=='case_id':r.sheet.row('SYN-G101')[0]['userEnteredValue']={'stringValue':'foreign'}
            elif attack=='category':r.set('SYN-G101','category','交通費｜電車')
            elif attack=='other_input':r.set('SYN-M101','memo','changed')
            elif attack=='permission':r.policy(lambda v:v.update(active=False))
        return result
    monkeypatch.setattr(r.worker,'_save',save)
    assert r.register()['status']=='UNKNOWN'
    n=r.draft_writes(),r.old.journal.read_versioned()
    r.worker=r.rebuild()
    for _ in range(3):assert r.worker.poll()[-1]['status']=='UNKNOWN'
    assert (r.draft_writes(),r.old.journal.read_versioned())==n


@pytest.mark.parametrize('lost',['before_journal','after_journal','draft','before_publish','after_publish'])
def test_transport_loss_reconstruction_and_unknown_no_resend(r,monkeypatch,lost):
    def fail(*a,**k):raise TimeoutError('synthetic response loss')
    if lost=='draft':monkeypatch.setattr(r.old.old.native['SYN-G101'].readers.draft,'replace_versioned',fail)
    elif lost in {'before_journal','after_journal'}:
        io=r.old.journal.transport.io;old=io.replace_versioned
        def replace(*a):
            if lost=='before_journal':fail()
            old(*a);fail()
        monkeypatch.setattr(io,'replace_versioned',replace)
    else:
        def fault(point):
            if point==lost:fail()
        r.sheet.fault=fault
    result=r.register()
    if lost in {'before_publish','after_publish'}:
        assert result['status']=='VALIDATED_UNPOSTED'
    else:assert result['status']=='UNKNOWN'
    n=r.draft_writes(),r.sheet.writes
    for _ in range(4):r.worker.poll()
    assert (r.draft_writes(),r.sheet.writes)==n
    if lost in {'before_publish','after_publish'}:
        r.worker=r.rebuild()
        for _ in range(3):r.worker.poll()
        assert (r.draft_writes(),r.sheet.writes)==n
    if lost in {'after_journal','draft'}:
        r.worker=r.rebuild();assert r.worker.poll()[-1]['status']=='UNKNOWN'


@pytest.mark.parametrize('workers',[2,16])
def test_concurrent_workers_intent_terminal_immutable(r,workers):
    r.request('SYN-G101','登録 1')
    contenders=[r.rebuild() for _ in range(workers)]
    for w in contenders:assert w.poll()[-1]['status']=='SYNC_WAIT'
    r.old.h.now+=3
    barrier=Barrier(workers)
    def run(w):barrier.wait(timeout=10);return w.poll()
    with ThreadPoolExecutor(workers) as pool:list(pool.map(run,contenders))
    records=r.worker._state()[1]['requests']
    assert len(records)==1
    terminal=deepcopy(records)
    assert next(iter(records.values()))['status'] in {'VALIDATED_UNPOSTED','ATTEMPTING'}
    assert r.draft_writes()<=1
    for w in contenders:w.poll()
    assert r.worker._state()[1]['requests']==terminal


def test_native_financial_history_and_legacy_unknown_not_reinterpreted(r,monkeypatch):
    from staging_shadow.sheets_only_review import SheetsOnlyReview
    legacy=SheetsOnlyReview(r.adapter,r.sheet,enabled=True,isolated_synthetic=True)
    r.request('SYN-G101','比較 1')
    def fail(*a,**k):raise TimeoutError()
    monkeypatch.setattr(r.old.old.native['SYN-G101'].readers.draft,'replace_versioned',fail)
    assert legacy.poll()[-1]['status']=='UNKNOWN'
    r.request('SYN-G101','登録 1')
    assert r.worker.poll()[-1]['status']=='UNKNOWN'


def test_setup_no_login_rejected_and_policy_not_cell_owned(r):
    from staging_shadow.standing_permission import SyntheticStandingPermission
    from app.unified_receipt_shadow import ExistingHostSessionGate
    h=r.old.h
    gate=ExistingHostSessionGate(h.configured,{'login':h.web.LOGIN_COOKIE,'ticket':h.web.COOKIE})
    with h.app.test_request_context('/'):
        with pytest.raises(StateError):
            SyntheticStandingPermission(gate,r.policy_io,r.runtime,enabled=True,isolated_synthetic=True)
    r.sheet.row('SYN-G101')[2]={'userEnteredValue':{'stringValue':'権限検証済み・記帳完了'}}
    assert r.register()['status']=='VALIDATED_UNPOSTED'


@pytest.mark.parametrize('entry,kind', [('receipt_auto','normal'),('bank_auto','bank'),
    ('paypay_auto','paypay'),('amazon_auto','amazon'),('cash_manual','cash'),
    ('medical_manual','medical'),('receipt_manual','normal')])
def test_explicit_ingress_scopes_auto_needs_native_eligibility_and_never_issues_authority(r,entry,kind):
    from app.unified_receipt_shadow import ExistingHostSessionGate
    cid='SYN-G101'
    r.policy(lambda v:v['routes'][cid].update(entry=entry,kind=kind))
    h=r.old.h
    r.gate=ExistingHostSessionGate(h.configured,{'login':h.web.LOGIN_COOKIE,'ticket':h.web.COOKIE})
    p=r.setup()  # A new configuration requires a new setup verification.
    if entry.endswith('_auto'):
        for uncertain in [None,'unknown','ambiguous','duplicate','conflict','held']:
            with pytest.raises(StateError,match='native_eligibility'):
                p.check(cid,kind=kind,automatic=uncertain)
    evidence=p.check(cid,kind=kind,automatic='eligible' if entry.endswith('_auto') else None)
    assert evidence['route']['entry']==entry
    assert not any(k in evidence for k in ['authority','csrf','session','writer_capability'])
    # Scope support does not falsely claim that these production ingresses
    # are connected to the receipt-only Sheet adapter.
    r.permission=p;r.worker=r.rebuild()
    if entry not in {'receipt_manual','medical_manual'} or kind!='normal':
        assert r.register()['status']=='PERMISSION_UNVERIFIED'


def test_invalid_amount_outside_grant_and_general_category_never_distributed(r):
    r.set('SYN-G101','category','交通費｜電車')
    before=r.old.old.ui._draft('SYN-G101')[0]
    assert r.register()['status']=='CONFLICT'
    assert r.old.old.ui._draft('SYN-G101')[0]==before


def test_amount_limit_missing_scope_and_actual_executor_change_rejected(r):
    with pytest.raises(StateError,match='value_out_of_scope'):
        r.permission.check('SYN-G101',amount=100001)
    r.set('SYN-G101','amount',100001)
    # Existing item/total consistency rejects this before policy evaluation.
    assert r.register()['status']=='CONFLICT'
    assert r.draft_writes()==0
    with pytest.raises(StateError,match='source_not_allowed'):
        r.permission.check('unconfigured-case')
    r.permission.runtime['executor']='untrusted-worker'
    assert r.register(generation=2)['status']=='PERMISSION_UNVERIFIED'


def test_financial_writer_scope_cannot_be_switched_even_at_setup(r):
    from app.unified_receipt_shadow import ExistingHostSessionGate
    r.runtime['writer']='another-writer'
    r.policy(lambda v:v['runtime'].update(writer='another-writer'))
    h=r.old.h
    r.gate=ExistingHostSessionGate(h.configured,{'login':h.web.LOGIN_COOKIE,'ticket':h.web.COOKIE})
    r.permission=r.setup()
    with pytest.raises(StateError,match='registration_isolation_required'):
        r.rebuild()


def test_explicit_setup_change_resumes_with_new_request_denial_stays_immutable(r):
    from app.unified_receipt_shadow import ExistingHostSessionGate
    r.policy(lambda v:v.update(permissions_verified=False))
    denied=r.register()
    assert denied['status']=='PERMISSION_UNVERIFIED'
    old=deepcopy(r.worker._state()[1]['requests'][denied['request_id']])
    r.policy(lambda v:v.update(permissions_verified=True,revision=2))
    h=r.old.h
    r.gate=ExistingHostSessionGate(h.configured,{'login':h.web.LOGIN_COOKIE,'ticket':h.web.COOKIE})
    r.permission=r.setup();r.worker=r.rebuild()
    assert r.register(generation=2)['status']=='VALIDATED_UNPOSTED'
    assert r.worker._state()[1]['requests'][denied['request_id']]==old
