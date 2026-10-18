from copy import deepcopy
import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.iphone_confirmation_host import PREFIX
from app.iphone_readonly_host import PREFIX as OLD_PREFIX
from iphone_confirmation_fixtures import Rig
from unified_shadow_fixtures import ORIGIN
from app.drive_run_state import StateError


@pytest.fixture(autouse=True)
def no_live_clients(monkeypatch):
    from google.cloud import firestore
    from googleapiclient import discovery
    def forbidden(*a,**kw):pytest.fail('live_client_forbidden')
    monkeypatch.setattr(firestore,'Client',forbidden)
    monkeypatch.setattr(discovery,'build',forbidden)


@pytest.mark.parametrize('cid',['SYN-G101','SYN-M101','SYN-P201'])
def test_save_redisplay_acknowledge_and_readback_are_synthetic_only(monkeypatch,tmp_path,cid):
    r=Rig(monkeypatch,tmp_path);assert r.h.login().status_code==303
    before_auth=deepcopy(r.h.db.data)
    result=r.save(cid);assert result.status_code==200,result.json
    assert result.json['result']['status']=='draft_saved'
    response,payload,csrf=r.accept(cid)
    assert response.status_code==200,response.json
    receipt=response.json['result']
    assert receipt['status']=='synthetic_owner_acknowledged' and receipt['synthetic_only']
    assert receipt['source_snapshot']['input_snapshot']==receipt['input_digest']
    assert receipt['source_snapshot']['observed_at']==r.h.now
    assert not receipt['native_authority'] and not receipt['accounting_permission']
    assert not {'operation_targets','authority','token','proof'} & set(receipt)
    assert r.h.get(response.json['redirect']).status_code==200
    writes=r.doc.writes
    replay=r.post(payload,csrf);assert replay.status_code==200 and replay.json['replay']
    assert replay.json['result']==receipt and r.doc.writes==writes
    assert r.h.db.data==before_auth
    assert r.h.get(OLD_PREFIX).status_code==200


def test_gets_never_write_or_observe_and_holds_have_no_form(monkeypatch,tmp_path):
    r=Rig(monkeypatch,tmp_path);r.h.login()
    before=r.doc.payload,r.doc.writes,deepcopy(r.h.db.data)
    for path in ['', '/cases/SYN-G101','/cases/SYN-M101','/cases/SYN-P201','/cases/SYN-G101/reconfirm',
                 '/cases/SYN-G103','/cases/SYN-H301','/results/nonexistent','/ui.js']:
        assert r.h.get(PREFIX+path).status_code==200
    assert before==(r.doc.payload,r.doc.writes,r.h.db.data)
    assert all(n['context'].source_reader.calls==0 for n in r.native.values())
    for cid in ['SYN-G103','SYN-H301']:
        assert '<form' not in r.h.get(PREFIX+'/cases/'+cid).get_data(as_text=True)


def test_partial_entries_and_notes_saved_then_exactly_redisplayed(monkeypatch,tmp_path):
    r=Rig(monkeypatch,tmp_path);r.h.login()
    payload,csrf,_=r.form();payload['edits']=r.edits(entered=False)
    payload['edits']['items'][0].update(state='held',note='未送信の明細メモ')
    payload['edits']['fields']['memo']='全体の途中メモ'
    result=r.post(payload,csrf);assert result.status_code==200,result.json
    html=r.h.get(result.json['redirect']).get_data(as_text=True)
    assert '未送信の明細メモ' in html and '全体の途中メモ' in html
    response,_,_=r.accept();assert response.status_code==409


@pytest.mark.parametrize('attack',['csrf','origin','expired','session','purpose','case','snapshot','revision','ack'])
def test_invalid_acknowledgement_is_fail_closed(monkeypatch,tmp_path,attack):
    r=Rig(monkeypatch,tmp_path);r.h.login();assert r.save().status_code==200
    payload,csrf,_=r.form(reconfirm=True);payload['acknowledged']=True
    headers={}
    if attack=='csrf':csrf='wrong'
    elif attack=='origin':headers['Origin']='https://foreign.test'
    elif attack=='expired':r.h.now+=600
    elif attack=='session':r.h.client.set_cookie(r.h.web.COOKIE,'wrong',domain='confirmation.example.test')
    elif attack=='purpose':payload['purpose']='STRUCTURE_REVIEW'
    elif attack=='case':payload['case_id']='SYN-G102'
    elif attack=='snapshot':payload['input_digest']='f'*64
    elif attack=='revision':payload['expected_revision']+=1
    else:payload['acknowledged']=False
    before=r.doc.payload,r.doc.writes,deepcopy(r.h.db.data)
    response=r.post(payload,csrf,**headers)
    assert response.status_code in {403,409,410},response.json
    if attack=='expired':
        assert response.is_json and '期限が切れました' in response.json['message']
    assert before==(r.doc.payload,r.doc.writes,r.h.db.data)


@pytest.mark.parametrize('path',['','/cases/SYN-G101','/cases/SYN-M101','/cases/SYN-P201',
    '/cases/SYN-G101/reconfirm','/results/unknown','/ui.js'])
def test_all_get_paths_authenticate_before_repository(monkeypatch,tmp_path,path):
    r=Rig(monkeypatch,tmp_path)
    monkeypatch.setattr(r.service,'read',lambda:pytest.fail('read_before_authentication'))
    assert r.h.get(PREFIX+path).status_code==403
    assert r.doc.writes==0


@pytest.mark.parametrize('attack',['other_google_user','issuer','bad_signature','audience','nonce','owner','case_owner','ticket','cross_site','null_origin'])
def test_google_owner_case_policy_and_browser_boundaries(monkeypatch,tmp_path,attack):
    r=Rig(monkeypatch,tmp_path)
    if attack in {'other_google_user','issuer','bad_signature','audience','nonce'}:
        if attack=='other_google_user':r.h.claim_changes={'sub':'999'}
        elif attack=='issuer':r.h.claim_changes={'iss':'https://foreign.test'}
        elif attack=='bad_signature':r.h.public='not a cert'
        elif attack=='audience':r.h.claim_changes={'aud':'foreign.apps.googleusercontent.com'}
        else:r.h.claim_changes={'nonce':'wrong'}
        assert r.h.login().status_code==403
        assert r.h.get(PREFIX).status_code==403
    else:
        r.h.login();payload,csrf,_=r.form();payload['edits']=r.edits()
        headers={}
        if attack=='owner':r.service.owner='f'*64
        elif attack=='case_owner':
            r.bundle['mappings']['SYN-G101']['owner']='f'*64
            r.path.write_text(json.dumps(r.bundle));r.saved.bundle_digest=__import__('app.unified_receipt_review',fromlist=['digest']).digest(r.bundle)
        elif attack=='ticket':r.h.client.set_cookie(r.h.web.COOKIE,'tampered',domain='confirmation.example.test')
        elif attack=='cross_site':headers['Sec-Fetch-Site']='cross-site'
        else:headers['Origin']='null'
        before=r.doc.payload,r.doc.writes
        assert r.post(payload,csrf,**headers).status_code in {403,409,410}
        assert before==(r.doc.payload,r.doc.writes)


@pytest.mark.parametrize('attack',['id','hash','revision','numeric_version','native_version','missing_native','bytes','mid_read'])
def test_trusted_source_changes_or_unknown_stop_acceptance(monkeypatch,tmp_path,attack):
    r=Rig(monkeypatch,tmp_path);r.h.login();assert r.save().status_code==200
    payload,csrf,_=r.form(reconfirm=True);payload['acknowledged']=True
    reader=r.native['SYN-G101']['context'].source_reader
    if attack=='id':reader.frame['source_id']='other-original'
    elif attack=='hash':reader.frame['sha256']='f'*64
    elif attack=='revision':reader.frame['drive_revision_id']='new-revision-same-hash'
    elif attack=='numeric_version':reader.frame['drive_numeric_version']='18'
    elif attack=='native_version':reader.frame['native_source_version']='new-native-same-hash'
    elif attack=='missing_native':reader.frame.pop('native_source_version')
    elif attack=='bytes':reader.raw=b'changed-original-bytes'
    else:
        original=reader.read_revision_bytes
        def changed(*args):
            value=original(*args);reader.frame['drive_numeric_version']='18';return value
        reader.read_revision_bytes=changed
    before=r.doc.payload,r.doc.writes,deepcopy(r.h.db.data)
    assert r.post(payload,csrf).status_code==409
    assert before==(r.doc.payload,r.doc.writes,r.h.db.data)


@pytest.mark.parametrize('attack',['add','delete','reorder','duplicate','foreign_id','fields_type','items_type','item_type',
    'amount_type','category_broadcast','state','immutable_amount','extra_authority'])
def test_item_and_field_contract_rejects_mixups_and_malformed_input(monkeypatch,tmp_path,attack):
    r=Rig(monkeypatch,tmp_path);r.h.login();payload,csrf,_=r.form();edits=r.edits();payload['edits']=edits
    if attack=='add':edits['items'].append(deepcopy(edits['items'][0]))
    elif attack=='delete':edits['items'].pop()
    elif attack=='reorder':edits['items'].reverse()
    elif attack=='duplicate':edits['items'][1]=deepcopy(edits['items'][0])
    elif attack=='foreign_id':edits['items'][0]['item_id']='other-source-item'
    elif attack=='fields_type':edits['fields']=list(edits['fields'])
    elif attack=='items_type':edits['items']='not items'
    elif attack=='item_type':edits['items'][0]=None
    elif attack=='amount_type':edits['fields']['amount']=True
    elif attack=='category_broadcast':edits['fields']['category']='食費｜食料品'
    elif attack=='state':edits['items'][0]['state']='automatically_confirmed'
    elif attack=='immutable_amount':edits['items'][0]['amount']='bad'
    else:payload['native_authority']=True
    before=r.doc.payload,r.doc.writes
    assert r.post(payload,csrf).status_code==409
    assert before==(r.doc.payload,r.doc.writes)


@pytest.mark.parametrize('cid',['SYN-G103','SYN-H301'])
def test_held_cases_cannot_receive_a_rebound_confirmation(monkeypatch,tmp_path,cid):
    r=Rig(monkeypatch,tmp_path);r.h.login();payload,csrf,_=r.form(reconfirm=True)
    payload.update(case_id=cid,acknowledged=True)
    assert r.post(payload,csrf).status_code==409 and r.doc.writes==0


@pytest.mark.parametrize('change',['sort','insert','delete','stale_raw_input'])
def test_sheet_layout_or_snapshot_change_stops_requests(monkeypatch,tmp_path,change):
    from app.unified_receipt_review import digest
    r=Rig(monkeypatch,tmp_path);r.h.login();payload,csrf,_=r.form();payload['edits']=r.edits()
    rows=r.bundle['raw_snapshot']['sheets'][0]['data'][0]['rowData']
    if change=='sort':rows[1:]=list(reversed(rows[1:]))
    elif change=='insert':rows.insert(2,deepcopy(rows[1]))
    elif change=='delete':rows.pop()
    else:rows[1]['values'][12]['userEnteredValue']={'stringValue':'別の未送信入力'}
    r.path.write_text(json.dumps(r.bundle));r.saved.bundle_digest=digest(r.bundle)
    before=r.doc.payload,r.doc.writes
    assert r.post(payload,csrf).status_code==409
    assert before==(r.doc.payload,r.doc.writes)


def test_input_save_invalidates_earlier_confirmation_and_keeps_old_snapshot(monkeypatch,tmp_path):
    r=Rig(monkeypatch,tmp_path);r.h.login();assert r.save().status_code==200
    old,csrf,_=r.form(reconfirm=True);old['acknowledged']=True
    payload,save_csrf,_=r.form();payload['edits']=r.edits();payload['edits']['fields']['memo']='確認後に変更した入力'
    assert r.post(payload,save_csrf).status_code==200
    assert r.post(old,csrf).status_code==409
    result,_,_=r.accept();assert result.status_code==200
    ledger=r.service.read()[2]
    assert len(ledger['history'])==2 and result.json['result']['reviewed_input']['fields']['memo']=='確認後に変更した入力'


def test_request_id_content_reuse_refused_and_new_request_duplicate_is_read_only(monkeypatch,tmp_path):
    r=Rig(monkeypatch,tmp_path);r.h.login();payload,csrf,_=r.form();payload['edits']=r.edits()
    assert r.post(payload,csrf).status_code==200
    payload['edits']['fields']['memo']='同一IDの別内容'
    assert r.post(payload,csrf).status_code==409
    assert r.accept()[0].status_code==200
    writes=r.doc.writes
    response,_,_=r.accept();assert response.status_code==200 and response.json['replay']
    assert r.doc.writes==writes


@pytest.mark.parametrize('fault',['before','after','partial','read'])
def test_unknown_outcome_has_no_retry_and_result_lookup_never_writes(monkeypatch,tmp_path,fault):
    r=Rig(monkeypatch,tmp_path);r.h.login();assert r.save().status_code==200
    payload,csrf,_=r.form(reconfirm=True);payload['acknowledged']=True
    before=r.doc.writes;r.doc.fault=fault
    response=r.post(payload,csrf);assert response.status_code in {409,503},response.json
    assert r.doc.writes<=before+1
    r.doc.fault=None
    if fault=='after':
        repeated=r.post(payload,csrf);assert repeated.status_code==200 and repeated.json['replay']
        assert r.doc.writes==before+1
        assert r.h.get(PREFIX+'/results/'+payload['request_id']).status_code==200
    elif fault=='before':
        assert '受付は確認できません' in r.h.get(PREFIX+'/results/'+payload['request_id']).get_data(as_text=True)
        assert r.doc.writes==before
    else:
        # read failure has no commit; partial corruption remains quarantined.
        expected=200 if fault=='read' else 503
        assert r.h.get(PREFIX+'/results/'+payload['request_id']).status_code==expected


def test_restart_reads_accepted_record_without_native_or_auth_mutations(monkeypatch,tmp_path):
    from app.iphone_confirmation import ConfirmationService
    from app.iphone_readonly_host import SavedDraftRepository
    from iphone_persistent_fixtures import repository
    r=Rig(monkeypatch,tmp_path);r.h.login();assert r.save().status_code==200
    response,_,_=r.accept();assert response.status_code==200
    before=r.doc.payload,r.doc.writes,deepcopy(r.h.db.data)
    fresh=repository(r.doc,r.key,r.batch)
    saved=SavedDraftRepository(fresh,r.path,r.saved.bundle_digest,r.saved.owner)
    reopened=ConfirmationService(saved,r.service.contexts,r.proof_key,clock=lambda:r.h.now)
    assert reopened.read()[2]['requests'][response.json['result']['request_id']]['result']==response.json['result']
    assert before==(r.doc.payload,r.doc.writes,r.h.db.data)


def test_default_off_and_wrong_environment_never_register_new_post(monkeypatch,tmp_path):
    from app.iphone_confirmation_host import make_isolated_confirmation_application
    from app.unified_receipt_shadow import ShadowConfig
    r=Rig(monkeypatch,tmp_path,enabled=False)
    assert r.h.get(PREFIX).status_code==404
    assert not any(rule.rule.startswith(PREFIX) for rule in r.h.app.url_map.iter_rules())
    with pytest.raises(StateError):
        make_isolated_confirmation_application(r.h.web.make_application,r.h.configured,r.service,
            config=ShadowConfig(True,'production'),cookies={'login':r.h.web.LOGIN_COOKIE,'ticket':r.h.web.COOKIE})


@pytest.mark.parametrize('workers',[2,8])
def test_multiple_instances_double_post_adopts_exactly_once(monkeypatch,tmp_path,workers):
    from app.iphone_confirmation import ConfirmationService
    from app.iphone_confirmation_host import make_isolated_confirmation_application
    from app.iphone_readonly_host import SavedDraftRepository
    from app.unified_receipt_shadow import ShadowConfig
    from iphone_persistent_fixtures import repository
    r=Rig(monkeypatch,tmp_path);r.h.login();r.save()
    payload,csrf,_=r.form(reconfirm=True);payload['acknowledged']=True
    clients=[]
    for _ in range(workers):
        journal=repository(r.doc,r.key,r.batch)
        view=SavedDraftRepository(journal,r.path,r.saved.bundle_digest,r.saved.owner)
        service=ConfirmationService(view,r.service.contexts,r.proof_key,clock=lambda:r.h.now)
        app=make_isolated_confirmation_application(r.h.web.make_application,r.h.configured,service,
            config=ShadowConfig(True,'isolated_synthetic'),cookies={'login':r.h.web.LOGIN_COOKIE,'ticket':r.h.web.COOKIE})
        app.testing=True;client=app.test_client()
        for name in [r.h.web.LOGIN_COOKIE,r.h.web.COOKIE]:
            client.set_cookie(name,r.h.client.get_cookie(name,domain='confirmation.example.test').value,domain='confirmation.example.test')
        clients.append(client)
    writes=r.doc.writes
    def post(client):
        return client.post(PREFIX+'/cases/SYN-G101/request',json=payload,base_url=ORIGIN,
            headers={'Origin':ORIGIN,'X-CSRF-Token':csrf})
    with ThreadPoolExecutor(workers) as pool:responses=list(pool.map(post,clients))
    assert all(x.status_code in {200,409} for x in responses)
    assert any(x.status_code==200 for x in responses)
    assert r.doc.writes==writes+1
    assert len([e for e in r.service.read()[2]['requests'].values() if e['result']['status']=='synthetic_owner_acknowledged'])==1


def test_concurrent_different_edits_conflict_without_overwriting_loser_input(monkeypatch,tmp_path):
    r=Rig(monkeypatch,tmp_path);r.h.login();payload,csrf,_=r.form();payload['edits']=r.edits()
    other=deepcopy(payload);other['edits']['fields']['memo']='別端末で途中入力'
    assert r.post(payload,csrf).status_code==200
    response=r.post(other,csrf)
    assert response.status_code==409 and response.json['input_preserved']
    assert other['edits']['fields']['memo']=='別端末で途中入力'
    assert r.doc.writes==1


@pytest.mark.parametrize('kind',['broken_ciphertext','broken_receipt','broken_draft','weak_etag'])
def test_corrupt_or_unavailable_journal_never_receives_new_ack(monkeypatch,tmp_path,kind):
    from app.unified_receipt_review import digest
    r=Rig(monkeypatch,tmp_path);r.h.login();r.save()
    payload,csrf,_=r.form(reconfirm=True);payload['acknowledged']=True
    if kind=='broken_ciphertext':r.doc.payload=b'damaged'
    elif kind=='weak_etag':
        original=r.journal.transport.io.read_versioned
        monkeypatch.setattr(r.journal.transport.io,'read_versioned',lambda:(original()[0],'W/"weak"'))
    else:
        state,_,_=r.journal._state()
        if kind=='broken_receipt':next(iter(state['confirmation']['requests'].values()))['result']['native_authority']=True
        else:state['confirmation']['drafts']['SYN-G101']['input_digest']='f'*64
        r.doc.payload=r.journal.transport.encode(state)
    before=r.doc.payload,r.doc.writes
    assert r.post(payload,csrf).status_code in {409,503}
    assert before==(r.doc.payload,r.doc.writes)


@pytest.mark.parametrize('attack',['missing_csrf','missing_origin','proof_tamper','request_id','revision_bool','extra_key',
    'bad_json','duplicate_json_key','wrong_media','missing_proof'])
def test_malformed_transport_is_refused_without_mutation(monkeypatch,tmp_path,attack):
    r=Rig(monkeypatch,tmp_path);r.h.login();payload,csrf,_=r.form();payload['edits']=r.edits()
    headers={'Origin':ORIGIN,'X-CSRF-Token':csrf}; kwargs={'json':payload}
    if attack=='missing_csrf':headers.pop('X-CSRF-Token')
    elif attack=='missing_origin':headers.pop('Origin')
    elif attack=='proof_tamper':payload['proof']=payload['proof'][:-4]+'AAAA'
    elif attack=='request_id':payload['request_id']='not-uuid'
    elif attack=='revision_bool':payload['expected_revision']=True
    elif attack=='extra_key':payload['operation_target']='forbidden'
    elif attack=='bad_json':kwargs=dict(data='{',content_type='application/json')
    elif attack=='duplicate_json_key':kwargs=dict(data='{"case_id":"SYN-G101","case_id":"SYN-G102"}',content_type='application/json')
    elif attack=='wrong_media':kwargs=dict(data=json.dumps(payload),content_type='text/plain')
    else:payload.pop('proof')
    before=r.doc.payload,r.doc.writes,deepcopy(r.h.db.data)
    response=r.h.client.post(PREFIX+'/cases/SYN-G101/request',base_url=ORIGIN,headers=headers,**kwargs)
    assert response.status_code in {403,409},response.get_data(as_text=True)
    assert before==(r.doc.payload,r.doc.writes,r.h.db.data)


def test_expired_old_proof_cannot_execute_after_new_login_but_result_can_be_read(monkeypatch,tmp_path):
    r=Rig(monkeypatch,tmp_path);r.h.login();r.save();response,payload,csrf=r.accept();assert response.status_code==200
    r.h.now+=600
    assert r.post(payload,csrf).status_code in {403,410}
    assert r.doc.writes==2


def test_acceptance_survives_an_actual_new_python_process(monkeypatch,tmp_path):
    import base64,subprocess,sys
    r=Rig(monkeypatch,tmp_path);r.h.login();r.save();response,_,_=r.accept();assert response.status_code==200
    fixture=tmp_path/'synthetic-restart.json'
    fixture.write_text(json.dumps(dict(payload=base64.b64encode(r.doc.payload).decode(),key=r.key.decode(),scope=r.journal.scope,
        receipt=response.json['result'])),encoding='utf8')
    code='''import base64,json,sys
from types import SimpleNamespace
from app.iphone_persistent_drafts import PersistentJournal
v=json.load(open(sys.argv[1]));s=v['scope']
io=SimpleNamespace(read_versioned=lambda:(base64.b64decode(v['payload']),'"saved-fake-backend"'))
r=PersistentJournal(io,v['key'].encode(),owner=s['owner'],contract_digest=s['contract'],generation=s['generation'],initial_snapshot=s['initial_snapshot'])
state,_,_=r._state(); actual=state['confirmation']['requests'][v['receipt']['request_id']]['result']
assert actual==v['receipt'] and actual['native_authority'] is False and actual['accounting_permission'] is False
print('synthetic-acknowledgement-process-restart-ok')
'''
    result=subprocess.run([sys.executable,'-c',code,str(fixture)],capture_output=True,text=True)
    assert result.returncode==0,result.stderr
    assert 'process-restart-ok' in result.stdout


def test_per_item_category_edit_keeps_old_value_and_other_items(monkeypatch,tmp_path):
    r=Rig(monkeypatch,tmp_path);r.h.login();payload,csrf,_=r.form();payload['edits']=r.edits()
    original=deepcopy(payload['edits']['items'])
    payload['edits']['items'][0]['category']='日用品｜洗剤'
    response=r.post(payload,csrf);assert response.status_code==200,response.json
    public=response.json['result']['reviewed_input']['items']
    assert public[0]['old']['category']==original[0]['category']
    assert public[0]['proposed']['category']=='日用品｜洗剤'
    assert public[1]['proposed']['category']==original[1]['category']
    response,_,_=r.accept();assert response.status_code==200,response.json


@pytest.mark.parametrize('attack',['native_fields','native_identity','native_record','medical_unsent','structure_contract'])
def test_current_native_validation_cannot_be_replaced_with_browser_digests(monkeypatch,tmp_path,attack):
    from dataclasses import replace
    cid='SYN-M101' if attack=='medical_unsent' else 'SYN-P201' if attack=='structure_contract' else 'SYN-G101'
    r=Rig(monkeypatch,tmp_path);r.h.login();assert r.save(cid).status_code==200
    payload,csrf,_=r.form(cid,reconfirm=True);payload['acknowledged']=True
    n=r.native[cid];context=n['context']
    if attack=='native_fields':context.case.fields[0][2]='native snapshot changed after redisplay'
    elif attack=='native_identity':context.case.identity['receipt_unit_id']='other-unit'
    elif attack=='native_record':context.record['digest']='f'*64
    elif attack=='medical_unsent':n['db'].rows['領収書確認'][0][8]='本人が別画面で入力中'
    else:context.structure_observation=replace(context.structure_observation,purpose='ACCOUNTING_POST')
    before=r.doc.payload,r.doc.writes,deepcopy(r.h.db.data)
    assert r.post(payload,csrf).status_code==409
    assert before==(r.doc.payload,r.doc.writes,r.h.db.data)


def test_pdf_displays_and_binds_the_actual_partition_without_applying_it(monkeypatch,tmp_path):
    r=Rig(monkeypatch,tmp_path);r.h.login();assert r.save('SYN-P201').status_code==200
    payload,csrf,response=r.form('SYN-P201',reconfirm=True)
    html=response.get_data(as_text=True)
    assert '確認するPDFのページ構成' in html and 'グループ 1：p1' in html and 'グループ 2：p2' in html
    payload['acknowledged']=True
    source=r.native['SYN-P201']['fixture'].state_io.raw
    response=r.post(payload,csrf);assert response.status_code==200,response.json
    proposal=response.json['result']['reviewed_input']['structure']['proposal']
    assert [g['page_numbers'] for g in proposal['groups']]==[[1],[2]]
    assert r.native['SYN-P201']['fixture'].state_io.raw==source
    assert not response.json['result']['accounting_permission']


def test_grouping_proposal_revision_change_after_redisplay_stops_ack(monkeypatch,tmp_path):
    r=Rig(monkeypatch,tmp_path);r.h.login();r.save('SYN-P201')
    payload,csrf,_=r.form('SYN-P201',reconfirm=True);payload['acknowledged']=True
    f=r.native['SYN-P201']['fixture'];state=f.grouping.store.load();record=next(iter(state['records'].values()))
    old=record['proposal'];snapshot={k:old[k] for k in ('source_file_id','source_content_hash','page_count','pages')}
    candidates=[{k:g[k] for k in ('page_numbers','confidence','reason')} for g in old['groups']]
    record['proposal']=f.native.receipt_pdf_grouping._proposal(snapshot,candidates,old['grouping_version']+1)
    record['revision']+=1;state['generation']+=1
    f.state_io.raw=f.native.pdf_grouping_authority.encoded(state)  # explicit synthetic external change
    before=r.doc.payload,r.doc.writes,f.state_io.raw
    assert r.post(payload,csrf).status_code==409
    assert before==(r.doc.payload,r.doc.writes,f.state_io.raw)


def test_ack_record_cannot_be_parsed_as_native_or_unified_authority(monkeypatch,tmp_path):
    from app.unified_receipt_write import OperationRequest
    from app.unified_receipt_purposes import PurposeRequest
    r=Rig(monkeypatch,tmp_path);r.h.login();r.save();response,_,_=r.accept();record=response.json['result']
    with pytest.raises(StateError):OperationRequest.parse(record)
    with pytest.raises(StateError):PurposeRequest.parse(record)
    def forbidden_keys(value):
        if isinstance(value,dict):
            assert not {'token','proof','sealed','operation_targets','operation_identity','credentials'} & set(value)
            for child in value.values():forbidden_keys(child)
        elif isinstance(value,list):
            for child in value:forbidden_keys(child)
    forbidden_keys(record)
