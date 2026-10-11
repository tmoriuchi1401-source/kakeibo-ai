"""Clean synthetic audit fixtures and native fake-writer capabilities only.

No live original, credential, authority update, unified dispatcher or sender.
"""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json

import pytest

from app.drive_run_state import StateError
from app.page_receipt_model import SourceRef, stable_page
from app.unified_receipt_review import (Case,digest,pdf_cases,relate_legacy_references,
                                       LEGACY_UNMAPPED_HOLD,LEGACY_HOLD_REASON)
from app.unified_receipt_eligibility import (identity_scope,current_snapshot,evaluate,evaluate_batch,counts)
from app.unified_receipt_preview import render


def clean(name='single-pdf',index=1,kind='general_receipt_item_review',pages=None,source_name=None):
    pages=pages or [1];source_name=source_name or name
    source=SourceRef(source_file_id='synthetic-clean-'+source_name,
                     source_content_hash=digest(['synthetic-bytes',source_name]),page_count=max(pages),
                     source_kind='image' if name=='image' else 'pdf')
    page_ids=[stable_page(source,n) for n in pages]
    ident=dict(source_file_id=source.source_file_id,source_content_hash=source.source_content_hash,
               source_version='synthetic-version-1',stable_page_identity=page_ids[0],
               page_number=pages[0],page_numbers=pages,page_identities=page_ids,
               receipt_unit_id='page-receipt-v1:'+digest(['synthetic-explicit-unit',source_name,index]),
               receipt_index=index,review_purpose=kind)
    fields=[['date','支払日','2026-10-11'],['merchant','店舗・施設','Synthetic manual clinic' if kind=='medical' else 'Synthetic shop'],
            ['amount','実支払額',730],['category','カテゴリ','医療・保険｜病院' if kind=='medical' else '食費｜食料品'],
            ['action','操作','医療費を確定' if kind=='medical' else '記帳する']]
    return Case('synthetic-'+source_name+'-'+str(index),'synthetic-clean-review-v1',ident,fields,'synthetic',kind,synthetic=True)


def evidence(case):
    scope=identity_scope(case);bound=digest(scope);snap=digest(current_snapshot(case))
    return dict(observed_identity=deepcopy(scope),snapshot_digest=snap,
        page_bindings=[dict(source_file_id=case.source_id,source_content_hash=scope['source_content_hash'],
                            source_version=scope['source_version'],page_number=n,stable_page_identity=p)
                       for n,p in zip(scope['page_numbers'],scope['page_identities'])],
        duplicate_check=dict(scope_digest=bound,snapshot_digest=snap,status='none',verified=True),
        permissions=dict(scope_digest=bound,snapshot_digest=snap,ai_send='denied',posting='allowed',
                         authority_reference='synthetic-native-validator-result',verified=True),
        send_required=False,input_origin='fully_manual' if case.kind=='medical' else 'owner_confirmed',
        writer=dict(name='receipt_confirmation' if case.kind=='medical' else 'pdf_intake',scope_digest=bound,
                    readback=True,replay_protected=True,verified=True),
        routes=[dict(target_id='synthetic-target-'+scope['receipt_unit_id'],state='active',
                     scope_digest=bound,snapshot_digest=snap,writer='receipt_confirmation' if case.kind=='medical' else 'pdf_intake')])


def old_pair():
    ident=dict(schema='pdf-page-review-v1',kind='medical',source_file_id='synthetic-old-original',
               source_content_hash='a'*64,page_numbers=[1],page_hashes=['b'*64],review_id='synthetic-orphan-review')
    def rows(i,token,fields):return [[label,value,'pdf-page-review-v1',token,json.dumps(i),marker] for marker,label,value in fields]
    medical=rows(ident,'old-medical',[('date','日付','2026-10-11'),('facility','施設','Synthetic manual clinic'),
                                       ('amount','金額',730),('medical_action','本人判断','')])
    recon=rows({**ident,'kind':'receipt_reconciliation','review_id':'','receipt_unit_id':''},'old-comparison',
               [('reconciliation_decision','判定','同一レシート'),('reconciliation_auth_link','確認表示','確認済み'),
                ('result','結果','未確定・会計writeなし')])
    return pdf_cases(medical+recon,{})


@pytest.mark.parametrize('name',['image','single-pdf','multi-page-one-receipt','medical'])
def test_clean_scenarios_plan_one_target_but_enable_no_action(name):
    c=clean(name,kind='medical' if name=='medical' else 'general_receipt_item_review',
            pages=[1,2] if name=='multi-page-one-receipt' else [1])
    manifest=evidence(c);before=deepcopy((c,manifest))
    decision=evaluate(c,manifest)
    assert decision.eligible and decision.proposed_target and decision.operation_targets==()
    assert (c,manifest)==before
    assert all(v is False for v in c.restrictions.values())


def test_same_page_multiple_receipts_and_mixed_pdf_keep_unit_and_purpose():
    cases=[clean('multiple',index=n) for n in (1,2,3)]
    cases += [clean('mixed',index=1,pages=[1,2]),clean('mixed',index=2,kind='medical',pages=[2])]
    decisions=evaluate_batch(cases,{c.case_id:evidence(c) for c in cases})
    assert counts(decisions)==dict(eligible=5,proposed_target_0=0,proposed_target_1=5,proposed_target_multiple=0,
                                  enabled_target_0=5,enabled_target_1=0,enabled_target_multiple=0)
    assert len({d.proposed_target for d in decisions.values()})==5


@pytest.mark.parametrize('field',['source_file_id','source_content_hash','source_version','stable_page_identity',
                                  'receipt_unit_id','receipt_index','review_purpose'])
def test_each_missing_identity_component_disables_target(field):
    c=clean();manifest=evidence(c);c.identity.pop(field)
    d=evaluate(c,manifest)
    assert not d.eligible and d.proposed_target is None and not d.operation_targets


@pytest.mark.parametrize('component',['observed_identity','page_bindings','snapshot_digest','duplicate_check',
                                      'permissions','writer','routes','send_required'])
def test_each_missing_evidence_component_disables_target(component):
    c=clean();manifest=evidence(c);manifest.pop(component)
    assert not evaluate(c,manifest).eligible


@pytest.mark.parametrize('changed',['version','snapshot','duplicate','permission_unit','permission_snapshot',
                                  'send_required','readback','replay','page_ambiguity','index_type'])
def test_stale_or_unbound_evidence_fails_closed(changed):
    c=clean();e=evidence(c)
    if changed=='version':c.identity['source_version']='synthetic-version-2'
    elif changed=='snapshot':c.fields[2][2]=731
    elif changed=='duplicate':e['duplicate_check']['status']='pending'
    elif changed=='permission_unit':e['permissions']['scope_digest']='0'*64
    elif changed=='permission_snapshot':e['permissions']['snapshot_digest']='0'*64
    elif changed=='send_required':e['send_required']=True
    elif changed=='readback':e['writer']['readback']=False
    elif changed=='replay':e['writer']['replay_protected']=False
    elif changed=='page_ambiguity':e['page_bindings']+=deepcopy(e['page_bindings'])
    else:c.identity['receipt_index']=True
    d=evaluate(c,e);assert not d.eligible and d.proposed_target is None


@pytest.mark.parametrize('change',['ai_allowed','nonmanual','send_required'])
def test_medical_cannot_enable_ai_or_adopt_a_candidate(change):
    c=clean('medical',kind='medical');e=evidence(c)
    if change=='ai_allowed':e['permissions']['ai_send']='allowed'
    elif change=='nonmanual':e['input_origin']='ai_candidate'
    else:e['send_required']=True
    assert not evaluate(c,e).eligible


def test_existing_ledger_link_requires_resolved_target_and_link_only_permission():
    c=clean('existing-link',kind='receipt_reconciliation');e=evidence(c)
    assert not evaluate(c,e).eligible
    e['duplicate_check']['status']='resolved_existing';e['permissions']['posting']='link_only'
    assert evaluate(c,e).eligible
    e['permissions']['posting']='allowed'
    assert not evaluate(c,e).eligible


def test_legacy_is_one_hold_entry_with_two_unmerged_reference_details():
    cases=old_pair();old,recon=cases;before=deepcopy([(c.identity,c.fields) for c in cases])
    assert old.disposition==LEGACY_UNMAPPED_HOLD
    relate_legacy_references(cases,[dict(primary_case_id=old.case_id,related_case_id=recon.case_id,
                                       provenance='explicit_audit_reference_only')])
    html=render(cases)
    assert html.count('class="legacy-entry"')==1
    assert html.count('class="case"')==2
    assert '関連案件：既存記帳照合（別案件）' in html and LEGACY_HOLD_REASON in html
    assert [(c.identity,c.fields) for c in cases]==before
    assert old.legacy_id!=recon.legacy_id and old.case_id!=recon.case_id
    assert old.display()[0]!='完了' and recon.display()[0]!='完了'
    assert not any(old.restrictions.values()) and not any(recon.restrictions.values())


def test_legacy_never_blocks_clean_unrelated_cases_or_creates_surrogate_authority():
    old,recon=old_pair();new=clean('new-original')
    before=deepcopy((old.identity,old.fields,recon.identity,recon.fields))
    e=evidence(new);decisions=evaluate_batch([old,recon,new],{new.case_id:e})
    assert decisions[new.case_id].eligible
    assert not decisions[old.case_id].eligible and not decisions[recon.case_id].eligible
    assert (old.identity,old.fields,recon.identity,recon.fields)==before
    assert counts(decisions)['proposed_target_1']==1 and counts(decisions)['enabled_target_1']==0
    with pytest.raises(StateError,match='legacy_operation_forbidden'):evaluate(old,e)


def test_old_page_cannot_be_repackaged_as_clean_new_unit():
    old,_=old_pair();new=clean('reused')
    new.identity.update(source_file_id=old.source_id,source_content_hash=old.identity['source_content_hash'])
    e=evidence(new)
    d=evaluate_batch([old,new],{new.case_id:e})[new.case_id]
    assert not d.eligible and d.reasons==('legacy_page_reuse_forbidden',)


def test_same_target_aliases_are_one_but_multiple_targets_stop_entire_plan():
    c=clean();e=evidence(c)
    e['routes'].append({**e['routes'][0],'entry':'legacy'})
    assert evaluate(c,e).eligible
    e['routes'].append({**e['routes'][0],'target_id':'synthetic-second-target'})
    with pytest.raises(StateError,match='multiple_operation_targets'):evaluate_batch([c],{c.case_id:e})
    # Missing other evidence cannot mask a multiple-target stop.
    e.pop('permissions')
    with pytest.raises(StateError,match='multiple_operation_targets'):evaluate(c,e)


def test_two_purposes_cannot_both_own_one_receipt_unit():
    a=clean();b=deepcopy(a);b.legacy_id='second-purpose';b.kind='medical';b.identity['review_purpose']='medical'
    with pytest.raises(StateError,match='multiple_operation_owners'):
        evaluate_batch([a,b],{a.case_id:evidence(a),b.case_id:evidence(b)})


def test_duplicate_reference_or_unknown_route_state_cannot_bypass_target_count():
    c=clean();e=evidence(c)
    with pytest.raises(StateError,match='duplicate_case_reference'):evaluate_batch([c,c],{c.case_id:e})
    e['routes'][0]['state']='unknown'
    with pytest.raises(StateError,match='route_state_unknown'):evaluate(c,e)


def test_repeated_and_simultaneous_readonly_requests_are_stable_and_zero_actions():
    old,recon=old_pair();new=clean('concurrent');cases=[old,recon,new];manifest={new.case_id:evidence(new)}
    before=deepcopy((cases,manifest))
    with ThreadPoolExecutor(max_workers=8) as pool:
        results=list(pool.map(lambda _:evaluate_batch(cases,manifest),range(40)))
    assert all(r==results[0] for r in results) and (cases,manifest)==before
    assert all(not d.operation_targets for r in results for d in r.values())


@pytest.mark.parametrize('name',['image','single-pdf','multi-page-one-receipt','multiple-receipts'])
def test_clean_general_native_materializer_exact_readback_and_no_blind_replay(name):
    from test_pdf_intake_runner import DB,CATEGORIES
    from app.pdf_intake_runner import plan,ScopedWriter,ledger_snapshot,exact_readback,ledger_source_id
    from app.receipt_pipeline import ReceiptPipeline
    from app.receipt_plan.models import ReceiptResult
    db=DB();cases=[clean(name,index=n,pages=[1,2] if name=='multi-page-one-receipt' else [1])
                  for n in (1,2,3) if name=='multiple-receipts'] if name=='multiple-receipts' else [
                      clean(name,pages=[1,2] if name=='multi-page-one-receipt' else [1])]
    for c in cases:
        assert evaluate(c,evidence(c)).eligible
        result=dict(date='2026-10-11',merchant='Synthetic shop '+str(c.identity['receipt_index']),total=730,payment_method='',
                    items=[dict(name='Synthetic milk',quantity=1.0,amount=250,major_category='食費',minor_category='食料品'),
                           dict(name='Synthetic detergent',quantity=1.0,amount=480,major_category='日用品',minor_category='洗剤')])
        page=dict(source=dict(source_file_id=c.source_id),page_number=c.pages[0])
        stamp='2026-10-11 10:00:00';uid=c.identity['receipt_unit_id']
        parsed=ReceiptResult.model_validate(result)
        expected=plan(parsed.model_dump(),uid,page,CATEGORIES,stamp);before=digest(ledger_snapshot(db))
        writer=ScopedWriter(db,CATEGORIES,expected,before)
        from app.pdf_review_fields import original_uri
        ReceiptPipeline(writer,None,clock=lambda:stamp)._materialize_result(parsed,ledger_source_id(uid),original_uri(c.source_id,c.pages[0]),[])
        assert exact_readback(db,expected,before)=='complete'
        data=deepcopy((db.data,db.writes))
        fresh=ScopedWriter(db,CATEGORIES,expected,before)
        with pytest.raises(StateError):fresh.append('レシート',expected['operations'][0]['rows'])
        assert (db.data,db.writes)==data
    assert len(db.data['レシート'])==len(cases)
    assert len({r[0] for r in db.data['レシート']})==len(cases)


def test_two_simultaneous_native_claims_have_one_conditional_winner():
    from test_receipt_unit_posting import port_setup
    from threading import Lock
    from types import SimpleNamespace
    store,http,before,tag,after=port_setup()
    other=deepcopy(store);other.http=http
    lock=Lock();original=http.put;accepted=[]
    def conditional(url,**kwargs):
        with lock:
            if kwargs['headers']['If-Match']!=http.meta['etag']:
                return SimpleNamespace(status_code=412)
            response=original(url,**kwargs);accepted.append(1);return response
    http.put=conditional
    def claim(candidate):
        try:candidate.replace(before,tag,after);return 'claimed'
        except StateError:return 'held'
    with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(claim,[store,other]))
    assert len(accepted)==1 and results.count('claimed')==1 and results.count('held')==1
    assert http.value==after


@pytest.mark.parametrize('stage',[1,2,3])
def test_clean_native_fake_writer_lost_response_is_readback_only_without_resend(stage):
    from test_pdf_intake_runner import rig
    from app.pdf_intake_runner import exact_readback
    runner,key,uid,db=rig();db.fail_after=stage
    c=clean('single-pdf');page=runner.store.load()['pages'][key]['page']
    c.identity.update(source_file_id=page['source']['source_file_id'],source_content_hash=page['source']['source_content_hash'],
                      stable_page_identity=page['stable_page_identity'],page_identities=[page['stable_page_identity']],
                      page_number=page['page_number'],page_numbers=[page['page_number']],receipt_unit_id=uid)
    assert evaluate(c,evidence(c)).eligible
    result=runner.post_normal(key,uid,apply=True)
    before=deepcopy((db.data,db.writes));saved=runner.store.load()['pages'][key]['units'][uid]['intent']['plan']
    assert exact_readback(db,saved)==('complete' if stage==3 else 'partial_or_unknown')
    replay=runner.post_normal(key,uid,apply=True)
    assert replay['replay'] and (db.data,db.writes)==before and len(db.writes)==stage
    assert result['retry_allowed'] is False


def test_fully_manual_clean_medical_native_fake_writer_and_existing_link_do_not_send_ai():
    from test_receipt_confirmation import medical,confirm
    c=clean('manual-medical',kind='medical');assert evaluate(c,evidence(c)).eligible
    service,store,db,verify,_=medical();confirm(db)
    service.capture_inputs();assert service.apply_confirmations()==1
    before=deepcopy(db.rows);assert service.apply_confirmations()==0 and db.rows==before
    assert len(db.rows['支出明細'])==1
    # A second newly observed original uses the existing manual link validator.
    source={'source_id':'synthetic-new-medical-link','version':'1','sha256':'d'*64,'mime_type':'image/png'}
    service.observe_medical(source,'synthetic-folder');service.render()
    row=next(r for r in db.rows['領収書確認'] if r[0]!=next(iter(store.value['confirmation_items'])))
    row[7:15]=['2026-09-01','Synthetic clinic',100,'医療・保険｜病院','現金','既存支出と重複（紐付け）',db.rows['支出明細'][0][0],'']
    service.capture_inputs();assert service.apply_confirmations()==1
    assert len(db.rows['支出明細'])==1 and len(db.rows['取込データ'])==2
    assert db.rows['取込データ'][-1][8]=='matched_receipt'
