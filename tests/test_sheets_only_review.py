from copy import deepcopy

import pytest

from sheets_only_fixtures import Rig, no_clients, evaluate
from staging_shadow.sheets_only_review import SLOT
from staging_shadow.sheets_only_surface import physical


@pytest.fixture(autouse=True)
def clients(monkeypatch):
    no_clients(monkeypatch)


@pytest.fixture
def r(monkeypatch,tmp_path):
    rig = Rig(monkeypatch,tmp_path)
    yield rig
    rig.assert_safe()


@pytest.mark.parametrize('cid',['SYN-G101','SYN-M101'])
def test_sheet_input_compare_separate_request_and_approval_unestablished(r,cid):
    r.set(cid,'memo','合成の手入力を保持')
    assert r.worker.poll() == []
    r.request(cid,'比較 1')
    assert '受付待ち' in r.sheet.view_text(cid)
    compared = r.worker.poll()[-1]
    assert compared['status'] == 'COMPARED'
    text = r.sheet.view_text(cid)
    assert cid in text and '比較版' in text and '原本版' in text and '合成の手入力を保持' in text
    r.request(cid,r.token(cid))
    assert '受付待ち' in r.sheet.view_text(cid)
    result = r.worker.poll()[-1]
    assert result['status'] == 'APPROVAL_UNESTABLISHED'
    assert result['approval_established'] is False and result['authority_dispatches'] == result['writer_dispatches'] == 0
    assert '承認未成立・未記帳' in r.sheet.view_text(cid)
    before = r.old.journal.read_versioned(),r.draft_writes(),r.sheet.writes
    for _ in range(4): assert r.worker.poll()[-1] == result
    assert (r.old.journal.read_versioned(),r.draft_writes(),r.sheet.writes) == before


def test_projection_roundtrip_preserves_all_inputs_links_notes_and_held_cases(r):
    before = deepcopy(r.initial_core)
    r.compare()
    assert r.sheet.core() == before
    assert list(r.sheet.contract['cases']) == ['SYN-G101','SYN-G102','SYN-G103','SYN-H301','SYN-M101','SYN-M102','SYN-P201','SYN-P202']
    assert r.sheet.raw['sheets'][0]['properties']['gridProperties']['frozenColumnCount'] == 1
    assert r.sheet.raw['sheets'][0]['data'][0]['rowData'][0]['values'][1]['userEnteredValue']['stringValue'] == '操作依頼'


@pytest.mark.parametrize('attack',['input','other_case_input','source','native_source','draft','display','dropdown','foreign_token','old_token'])
def test_stale_snapshot_source_and_case_mix_rejected(r,attack):
    from draft_runtime_fixtures import set_value
    from app.receipt_confirmation_contract import load
    r.compare(); token = r.token()
    if attack=='input': r.set('SYN-G101','memo','別入力')
    elif attack=='other_case_input': r.set('SYN-M101','memo','別案件')
    elif attack=='source':
        port=r.old.old.native['SYN-G101'].readers.source_bytes;port.raw=b'replaced'
    elif attack=='native_source':
        c=r.old.old.native['SYN-G101'];v=load(c.readers.source.raw);v['source']['drive_revision_id']='replacement';set_value(c.readers.source,v)
    elif attack=='draft':
        c=r.old.old.native['SYN-G101'];v=load(c.readers.draft.raw);v['revision']+=1;set_value(c.readers.draft,v)
    elif attack=='display': r.sheet.row('SYN-G101')[2]={'userEnteredValue':{'stringValue':'確認済み'}}
    elif attack=='dropdown': r.sheet.row('SYN-G101')[1]['dataValidation']['strict']=False
    elif attack=='foreign_token':
        r.compare('SYN-M101');token=r.token('SYN-M101')
    elif attack=='old_token': r.compare(generation=2)
    r.request('SYN-G101',token)
    result=r.worker.poll()
    assert any(v['status']=='CONFLICT' for v in result),result
    assert '競合' in r.sheet.view_text('SYN-G101')


@pytest.mark.parametrize('cid',['SYN-G103','SYN-H301','SYN-G102','SYN-M102','SYN-P201','SYN-P202'])
def test_hold_pdf_legacy_never_enters_native_import_or_confirmation(r,cid):
    before=r.draft_writes()
    r.request(cid,'比較 1');assert r.worker.poll()[-1]['status']=='HELD'
    assert '保留' in r.sheet.view_text(cid)
    r.request(cid,'確定 deadbeef');assert r.worker.poll()[-1]['status']=='CONFLICT'
    assert r.draft_writes()==before


def test_partial_medical_import_is_manual_and_kept_without_comparison_or_ai(r):
    r.set('SYN-M101','amount','');r.set('SYN-M101','memo','医療手入力途中')
    r.request('SYN-M101','比較 1');result=r.worker.poll()[-1]
    assert result['status']=='HELD'
    value=r.old.old.ui._draft('SYN-M101')[0]['value']
    assert value['fields']['amount']=='' and value['fields']['memo']=='医療手入力途中'
    r.set('SYN-M101','amount',135)
    assert r.compare('SYN-M101',2)['status']=='COMPARED'


def test_general_categories_not_distributed_and_nonentered_items_held(r):
    from draft_runtime_fixtures import set_value
    c=r.old.old.native['SYN-G101'];before=r.old.old.ui._draft('SYN-G101')[0]
    r.set('SYN-G101','category','交通費｜電車')
    r.request('SYN-G101','比較 1');assert r.worker.poll()[-1]['status']=='CONFLICT'
    assert r.old.old.ui._draft('SYN-G101')[0]==before


@pytest.mark.parametrize('mutation',['sort','duplicate','id','kind','hash','revision','note','protection','header','delete','insert'])
def test_raw_reader_asserts_not_weakened_for_new_columns(r,mutation):
    sheet=r.sheet.raw['sheets'][0];rows=sheet['data'][0]['rowData']
    r.request('SYN-G101','比較 1')
    if mutation=='sort': rows[1:]=list(reversed(rows[1:]))
    elif mutation=='duplicate': rows[2]=deepcopy(rows[1])
    elif mutation in {'id','kind','hash'}:
        col={'id':0,'kind':1,'hash':15}[mutation];rows[1]['values'][physical(col)]['userEnteredValue']={'stringValue':'foreign'}
    elif mutation=='revision': next(iter(r.old.sources.values()))['source_version']='changed'
    elif mutation=='note':rows[1]['values'][physical(12)]['note']='changed'
    elif mutation=='protection':sheet['protectedRanges']=[]
    elif mutation=='header':rows[0]['values'][1]={'userEnteredValue':{'stringValue':'changed'}}
    elif mutation=='delete':rows.pop()
    elif mutation=='insert':rows.append(deepcopy(rows[-1]))
    before=r.draft_writes();r.worker.poll()
    assert r.draft_writes()==before
    assert all(v['approval_established'] is False for v in r.worker.poll())


def test_events_dropped_duplicated_out_of_order_do_not_drive_processing(r):
    r.request('SYN-G101','比較 1')
    # No event is delivered. Only the next level-triggered fresh poll matters.
    first=r.worker.poll()[-1];writes=r.draft_writes()
    for _ in range(10): assert r.worker.poll()[-1]==first
    assert r.draft_writes()==writes
    r.request('SYN-G101','比較 9');assert r.worker.poll()[-1]['status']=='CONFLICT'
    r.compare(generation=2)
    r.request('SYN-G101','比較 1');assert r.worker.poll()[-1]['status']=='CONFLICT'
    assert r.draft_writes()==writes


@pytest.mark.parametrize('stage',['before_draft','after_draft','before_result','after_result'])
def test_unknown_kept_across_polls_and_worker_reconstruction_without_retry(r,monkeypatch,stage):
    c=r.old.old.native['SYN-G101']
    native=c.readers.draft.replace_versioned
    save=r.old.journal.save
    if stage in {'before_draft','after_draft'}:
        def fail(*args,**kwargs):
            if stage=='after_draft':native(*args,**kwargs)
            raise RuntimeError('synthetic draft response lost')
        monkeypatch.setattr(c.readers.draft,'replace_versioned',fail)
    else:
        def fail(*args,**kwargs):
            requests=kwargs.get('details',{}).get(SLOT,{}).get('requests',{})
            if requests and any(v['status']=='COMPARED' for v in requests.values()):
                if stage=='after_result':save(*args,**kwargs)
                raise RuntimeError('synthetic journal response lost')
            return save(*args,**kwargs)
        monkeypatch.setattr(r.old.journal,'save',fail)
    r.request('SYN-G101','比較 1');r.worker.poll()
    writes=r.draft_writes()
    r.worker=r.rebuild()
    for _ in range(4):
        result=r.worker.poll()
        assert result[-1]['status']==('COMPARED' if stage=='after_result' else 'UNKNOWN')
    assert r.draft_writes()==writes


def test_lost_projection_response_is_read_back_without_reexecution(r):
    def fail(point):
        if point=='after_publish':raise RuntimeError('lost projection response')
    r.sheet.fault=fail;r.request('SYN-G101','比較 1')
    result=r.worker.poll()[-1];assert result['status']=='COMPARED'
    before=r.draft_writes(),r.sheet.writes,r.old.journal.read_versioned()
    r.sheet.fault=None;assert r.worker.poll()[-1]==result
    assert (r.draft_writes(),r.sheet.writes,r.old.journal.read_versioned())==before


def test_owner_email_acl_status_and_cells_are_never_approval_evidence(r):
    r.compare();r.request('SYN-G101',r.token())
    result=r.worker.poll()[-1]
    assert result['status']=='APPROVAL_UNESTABLISHED'
    assert r.old.old.ui._requests('SYN-G101')==[]
    assert not result['approval_established']


@pytest.mark.parametrize('count',[2,16])
def test_independent_pollers_cas_never_import_twice(r,count):
    from concurrent.futures import ThreadPoolExecutor
    r.request('SYN-G101','比較 1');before=r.draft_writes()
    workers=[r.rebuild() for _ in range(count)]
    with ThreadPoolExecutor(count) as pool:
        outcomes=list(pool.map(lambda w:w.poll(),workers))
    assert r.draft_writes()-before==1
    _,ledger,_=r.worker._state()
    assert len(ledger['requests'])==1
    assert next(iter(ledger['requests'].values()))['status']=='COMPARED'
    for worker in workers: assert worker.poll()[-1]['status']=='COMPARED'


def test_concurrent_sheet_edit_after_intent_is_unknown_no_import(r,monkeypatch):
    capture=r.adapter._capture;calls=[]
    def changed(*args):
        calls.append(1)
        if len(calls)==2:r.set('SYN-G101','memo','同時編集は失わない')
        return capture(*args)
    monkeypatch.setattr(r.adapter,'_capture',changed)
    r.request('SYN-G101','比較 1');before=r.draft_writes()
    assert r.worker.poll()[-1]['status']=='UNKNOWN'
    r.worker=r.rebuild()
    assert r.worker.poll()[-1]['status']=='UNKNOWN'
    assert r.draft_writes()==before
    assert r.sheet.row('SYN-G101')[physical(12)]['userEnteredValue']['stringValue']=='同時編集は失わない'


def test_comparison_expiry_rejects_and_new_comparison_is_explicit(r):
    r.compare();r.request('SYN-G101',r.token());r.old.h.now+=600
    assert r.worker.poll()[-1]['status']=='CONFLICT'
    assert '期限切れ' in r.sheet.view_text('SYN-G101')
    r.compare(generation=2)


@pytest.mark.parametrize('mode',['inputs_changed','pending','idle','approval_held','unknown','quote'])
def test_actual_generated_standard_formula_calculation(r,mode,monkeypatch):
    if mode=='quote':r.set('SYN-G101','memo','"引用" と =HYPERLINK("https://invalid.test","式ではない")')
    r.compare()
    if mode=='inputs_changed':r.set('SYN-G101','memo','編集後')
    elif mode=='pending':r.request('SYN-G101','比較 2')
    elif mode=='idle':r.request('SYN-G101','入力中')
    elif mode=='approval_held':r.request('SYN-G101',r.token());r.worker.poll()
    elif mode=='unknown':
        def fail(*args,**kwargs):raise RuntimeError('synthetic uncertain save')
        monkeypatch.setattr(r.old.old.native['SYN-M101'].readers.draft,'replace_versioned',fail)
        r.request('SYN-M101','比較 1');r.worker.poll()
    assert evaluate(r.sheet,'SYN-G101')==r.sheet.view_text('SYN-G101')


def test_observation_connection_loss_before_acceptance_waits_and_reads_only(r,monkeypatch):
    original=r.sheet.read
    def fail():raise OSError('synthetic unavailable read')
    r.request('SYN-G101','比較 1');before=r.draft_writes(),r.old.journal.read_versioned()
    monkeypatch.setattr(r.sheet,'read',fail)
    assert r.worker.poll()[0]['status']=='WAITING'
    assert (r.draft_writes(),r.old.journal.read_versioned())==before
    monkeypatch.setattr(r.sheet,'read',original)
    assert r.worker.poll()[-1]['status']=='COMPARED'


def test_unknown_on_one_case_fences_other_case_request_without_input_loss(r,monkeypatch):
    def fail(*args,**kwargs):raise OSError('synthetic draft write unknown')
    monkeypatch.setattr(r.old.old.native['SYN-G101'].readers.draft,'replace_versioned',fail)
    r.request('SYN-G101','比較 1');assert r.worker.poll()[-1]['status']=='UNKNOWN'
    r.set('SYN-M101','memo','他案件の医療入力保持');r.request('SYN-M101','比較 1')
    before=r.draft_writes(),r.old.journal.read_versioned()
    assert r.rebuild().poll()[-1]['status']=='UNKNOWN'
    assert (r.draft_writes(),r.old.journal.read_versioned())==before
    assert 'UNKNOWN' in evaluate(r.sheet,'SYN-M101')


def test_result_inspection_after_reload_does_not_observe_or_write(r,monkeypatch):
    result=r.compare();before=r.old.journal.read_versioned(),r.draft_writes()
    worker=r.rebuild()
    def forbidden(*args,**kwargs):pytest.fail('result_read_must_not_observe_or_write')
    monkeypatch.setattr(r.adapter,'_capture',forbidden)
    monkeypatch.setattr(r.reader,'read',forbidden)
    monkeypatch.setattr(r.old.journal,'save',forbidden)
    assert worker.inspect(result['request_id'])==result
    assert (r.old.journal.read_versioned(),r.draft_writes())==before


def test_existing_unentered_general_item_is_held_without_inferred_categories(r):
    from draft_runtime_fixtures import set_value
    from app.unified_receipt_review import digest
    c=r.old.old.native['SYN-G101'];d=r.old.old.ui._draft('SYN-G101')[0]
    categories=[i['category'] for i in d['value']['items']]
    d['value']['items'][0]['state']='unentered';d['input_digest']=digest(d['value']);set_value(c.readers.draft,d)
    r.request('SYN-G101','比較 1');assert r.worker.poll()[-1]['status']=='HELD'
    saved=r.old.old.ui._draft('SYN-G101')[0]['value']
    assert [i['category'] for i in saved['items']]==categories and saved['items'][0]['state']=='unentered'


def test_edit_during_projection_never_publishes_freshness_success_or_overwrites_inputs(r):
    calls=[]
    def change(point):
        if point=='before_publish' and not calls:
            calls.append(1);r.set('SYN-G101','memo','表示中の同時編集を保持')
    r.sheet.fault=change;r.request('SYN-G101','比較 1');r.worker.poll()
    assert '競合' in evaluate(r.sheet,'SYN-G101')
    assert r.sheet.row('SYN-G101')[physical(12)]['userEnteredValue']['stringValue']=='表示中の同時編集を保持'


@pytest.mark.parametrize('mode',['unknown','confirmed'])
def test_existing_155_fence_and_saved_boundary_result_are_preserved(monkeypatch,tmp_path,mode):
    from sheet_handoff_host_fixtures import HostRig
    from staging_shadow.iphone_sheet_handoff import SheetDraftHandoff
    from staging_shadow.sheets_only_surface import SyntheticSheetSurface
    from staging_shadow.sheets_only_review import SheetsOnlyReview
    from app.iphone_sheet_reader import RawReader
    old=HostRig(monkeypatch,tmp_path);old.h.login()
    p=old.prepare('SYN-G101','import_draft')
    if mode=='unknown':
        def fail(*args,**kwargs):raise RuntimeError('synthetic old pending')
        monkeypatch.setattr(old.old.ui,'post',fail)
        assert old.send(p).status_code==503
    else:
        assert old.send(p).status_code==200
        q=old.prepare('SYN-G101','confirm_handoff');assert old.send(q,True).status_code==200
    sheet=SyntheticSheetSurface(old.raw,old.contract)
    reader=RawReader(old.contract,sheet.core,sheet.observe_sources)
    adapter=SheetDraftHandoff(reader,old.journal,old.old.ui,enabled=True,isolated_synthetic=True)
    worker=SheetsOnlyReview(adapter,sheet,enabled=True,isolated_synthetic=True)
    sheet.row('SYN-G101')[1]['userEnteredValue']={'stringValue':'比較 1'}
    before=old.journal.read_versioned(),old.old.ui._draft('SYN-G101')[0],deepcopy(old.h.db.data)
    result=worker.poll()[0]
    assert result['status']==('UNKNOWN' if mode=='unknown' else 'FINANCIAL_BOUNDARY_STOPPED')
    assert (old.journal.read_versioned(),old.old.ui._draft('SYN-G101')[0],old.h.db.data)==before
    assert 'UNKNOWN' in evaluate(sheet,'SYN-G101') if mode=='unknown' else '既存版' in evaluate(sheet,'SYN-G101')
    assert {cid:old.old.frozen(cid) for cid in old.old.native}==old.before


@pytest.mark.parametrize('attack',['disabled','environment','sheet_transport','source_transport','journal_transport'])
def test_composition_rejects_live_or_implicit_transport_before_use(r,attack,monkeypatch):
    from app.drive_run_state import StateError
    from staging_shadow.sheets_only_review import SheetsOnlyReview
    def forbidden(*args,**kwargs):pytest.fail('transport_must_not_be_called')
    if attack=='sheet_transport':monkeypatch.setattr(r.reader,'read_raw',forbidden)
    elif attack=='source_transport':monkeypatch.setattr(r.reader,'read_sources',forbidden)
    elif attack=='journal_transport':monkeypatch.setattr(r.old.io,'synthetic_memory_only',False)
    with pytest.raises(StateError):
        SheetsOnlyReview(r.adapter,r.sheet,enabled=attack!='disabled',isolated_synthetic=attack!='environment')
