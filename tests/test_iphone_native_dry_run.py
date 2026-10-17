"""Raw cells -> existing validators, only synthetic native fixtures and IO."""
from copy import deepcopy
from dataclasses import replace

import pytest

from app.drive_run_state import StateError
from app.iphone_native_dry_run import (bind_versions, bind_case, preview_bound_draft,
    medical_fields_only, day_value)
from app.iphone_sheet_reader import raw_value, issue_contract, validate_snapshot, original_formula
from app.unified_receipt_review import digest
from app.unified_receipt_eligibility import current_snapshot
from test_iphone_review_preview import (from_case, general_fixture, structure_fixture, forbidden)
from test_unified_receipt_write import medical_native
from test_iphone_sheet_reader import fixture, text


def raw_draft(canonical, kind):
    alias={'一般':'SYN-G101','医療':'SYN-M101','PDF':'SYN-P201'}[kind]
    cells = [{} for _ in range(17)]
    cells[0:2] = [text(alias),text(kind)]
    cells[3] = text('入力中')
    for marker,col in dict(date=4,merchant=5,amount=6,category=7,payment=8,memo=12).items():
        value = canonical['inputs'][marker]
        cells[col] = {'userEnteredValue':{'numberValue':value}} if type(value)==int else text(value)
    cells[10:12] = [text('合成結合選択'),text('合成分割選択')]
    native=canonical['identity']
    identity=dict(source_id=native['source_file_id'],sha256=native['source_content_hash'],
                  source_version='independent-drive-revision',version_scheme='drive_revision_id')
    cells[15:17]=[text(identity['sha256']),text(identity['source_id'])]
    d=dict(case_id=alias,kind=kind,identity=identity,cells=cells,held=False,
           input_snapshot_digest=digest(cells),authorized=False,operation_targets=[])
    observed=dict(source_id=identity['source_id'],sha256=identity['sha256'],
                  drive_revision_id=identity['source_version'],native_source_version=native['source_version'])
    binding=bind_versions(lambda:deepcopy(observed),identity,native)
    return d,binding


def medical_fixture():
    c,b,p,_,service,db,store,fake,io=medical_native()
    canonical=from_case(c,'medical'); d,v=raw_draft(canonical,'医療')
    context=dict(canonical=canonical,case=c,port=p,native_digest=digest(current_snapshot(c)),version_binding=v)
    context['sheet_binding']=bind_case(d['case_id'],canonical,context['native_digest'])
    return d,context,(b,p,db,store,fake,io)


def test_raw_medical_draft_reuses_native_manual_validator_and_never_executes(monkeypatch):
    d,context,(b,p,db,store,fake,io)=medical_fixture()
    before=deepcopy((db.rows,store.value,io.raw))
    for obj,method in [(store,'save'),(db,'append_raw'),(db,'update_row_raw'),(p,'execute'),(b,'submit')]:
        monkeypatch.setattr(obj,method,forbidden)
    checked=preview_bound_draft(d,d['input_snapshot_digest'],context)
    assert checked['validation']['status']=='validated_not_authorized'
    assert checked['authorized'] is False and checked['operation_targets']==[]
    assert (db.rows,store.value,io.raw)==before
    assert fake.ai_calls==fake.moves==io.writes==0


def test_full_eight_case_raw_reader_to_existing_medical_validator(monkeypatch):
    d,context,(b,p,db,store,fake,io)=medical_fixture()
    # Keep the derived native Case ID intact; the manager owns the alias map.
    raw,sources=fixture(); cells=raw['sheets'][0]['data'][0]['rowData'][5]['values']
    for col in [4,5,6,7,8,12,15,16]:cells[col]['userEnteredValue']=deepcopy(d['cells'][col]['userEnteredValue'])
    source=dict(case_id='SYN-M101',**d['identity'])
    sources=[source if s['case_id']=='SYN-M101' else s for s in sources]
    cells[2]['userEnteredValue']={'formulaValue':original_formula('SYN-M101',source,'医療')}
    contract=issue_contract(raw,sources)
    batch=validate_snapshot(contract,raw,{s['source_id']:s for s in sources})
    captured=next(v for v in batch.drafts.values() if v['case_id']=='SYN-M101')
    before=deepcopy((db.rows,store.value,io.raw))
    monkeypatch.setattr(p,'execute',forbidden); monkeypatch.setattr(store,'save',forbidden)
    result=preview_bound_draft(captured,captured['input_snapshot_digest'],context)
    assert result['validation']['accounting_writes']==0
    assert (db.rows,store.value,io.raw)==before and fake.ai_calls==fake.moves==io.writes==0


def test_raw_general_draft_reuses_per_item_validator_without_category_broadcast(monkeypatch):
    c,p,db,runner,record,s,canonical=general_fixture(); d,v=raw_draft(canonical,'一般')
    context=dict(canonical=canonical,case=c,native_digest=digest(current_snapshot(c)),record=record,
                 accepted_snapshot=s,accepted_digest=digest(s),categories=db.categories(),version_binding=v)
    context['sheet_binding']=bind_case(d['case_id'],canonical,context['native_digest'])
    before=deepcopy((db.data,runner.store.load()))
    monkeypatch.setattr(runner.store,'save',forbidden); monkeypatch.setattr(p,'execute',forbidden)
    monkeypatch.setattr(db,'append_raw',forbidden)
    checked=preview_bound_draft(d,d['input_snapshot_digest'],context)
    assert checked['validation']['proposed_native_snapshot']['items']==s['items']
    assert checked['authorized'] is False and checked['operation_targets']==[]
    assert (db.data,runner.store.load())==before
    d['cells'][7]=text('食費｜食品'); d['input_snapshot_digest']=digest(d['cells'])
    with pytest.raises(StateError,match='item_category_contract'):
        preview_bound_draft(d,d['input_snapshot_digest'],context)
    assert (db.data,runner.store.load())==before


def test_raw_pdf_selection_reuses_structure_contract_without_financial_permission(monkeypatch):
    f,o,canonical=structure_fixture(); d,v=raw_draft(canonical,'PDF')
    context=dict(canonical=canonical,observation=o,request_id=f.rid,version_binding=v,native_digest=digest(o.evidence),
                 selections={('合成結合選択','合成分割選択'):canonical['inputs']['partition']})
    context['sheet_binding']=bind_case(d['case_id'],canonical,context['native_digest'])
    before=(f.state_io.raw,f.io.raw)
    monkeypatch.setattr(f.grouping,'review',forbidden); monkeypatch.setattr(f.port,'execute',forbidden)
    checked=preview_bound_draft(d,d['input_snapshot_digest'],context)
    assert checked['validation']['accounting_allowed'] is False
    assert checked['operation_targets']==[] and checked['authorized'] is False
    assert (f.state_io.raw,f.io.raw)==before
    assert f.io.writes==f.accounting_appends==f.ai_sends==f.source_moves==f.native_calls==0


@pytest.mark.parametrize('attack',['snapshot','case','kind','hash','original_id','revision','native_version','missing_binding'])
def test_native_boundary_rechecks_saved_snapshot_and_distinct_source_versions(attack):
    d,context,(_,_,db,store,fake,io)=medical_fixture(); before=deepcopy((db.rows,store.value,io.raw))
    expected=d['input_snapshot_digest']
    if attack=='snapshot':expected='0'*64
    elif attack=='case':d['case_id']='foreign'
    elif attack=='kind':d['kind']='一般'
    elif attack=='hash':d['identity']['sha256']='0'*64
    elif attack=='original_id':d['identity']['source_id']='foreign'
    elif attack=='revision':d['identity']['source_version']='new-revision'
    elif attack=='native_version':context['version_binding']=replace(context['version_binding'],native_source_version='new-native-version')
    else:context.pop('version_binding')
    with pytest.raises(StateError):preview_bound_draft(d,expected,context)
    assert (db.rows,store.value,io.raw)==before and fake.ai_calls==fake.moves==io.writes==0


@pytest.mark.parametrize('attack',['racing','missing','wrong_revision','wrong_native','wrong_source','same_string_no_evidence'])
def test_two_version_namespaces_require_fresh_independent_co_observation(attack):
    sheet=dict(source_id='fictitious-source',sha256='a'*64,source_version='revision-abc',version_scheme='drive_revision_id')
    native=dict(source_file_id='fictitious-source',source_content_hash='a'*64,source_version='17')
    observed=dict(source_id='fictitious-source',sha256='a'*64,drive_revision_id='revision-abc',native_source_version='17')
    if attack=='missing':observed.pop('drive_revision_id')
    elif attack=='wrong_revision':observed['drive_revision_id']='17'
    elif attack=='wrong_native':observed['native_source_version']='revision-abc'
    elif attack=='wrong_source':observed['source_id']='foreign'
    elif attack=='same_string_no_evidence':observed=None
    counter=[0]
    def read():
        counter[0]+=1; r=deepcopy(observed)
        if attack=='racing' and counter[0]==2:r['native_source_version']='18'
        return r
    with pytest.raises(StateError):bind_versions(read,sheet,native)


def test_missing_native_context_and_sheet_hold_never_turn_into_confirm_request():
    d,_,_=medical_fixture()
    assert preview_bound_draft(d,d['input_snapshot_digest'],None)['status']=='native_snapshot_required'
    d['cells'][3]=text('保留'); d['input_snapshot_digest']=digest(d['cells'])
    assert preview_bound_draft(d,d['input_snapshot_digest'],None)['status']=='held'


@pytest.mark.parametrize('value',['',0,-1,True,1.5])
def test_native_medical_parser_rejects_invalid_manual_amount_without_writes(value):
    d,_,_=medical_fixture()
    d['cells'][6]={'userEnteredValue':{'boolValue':value}} if type(value)==bool else (
        {'userEnteredValue':{'numberValue':value}} if type(value) in (int,float) else text(value))
    if type(value)==bool:
        with pytest.raises(StateError):medical_fields_only(d,[('医療・保険','病院')])
    else:
        assert medical_fields_only(d,[('医療・保険','病院')])['status']=='incomplete_or_invalid_manual_fields'


@pytest.mark.parametrize('value',[-1,1.2,float('inf'),float('nan'),2958466])
def test_sheet_date_serial_is_not_coerced_to_a_different_day(value):
    with pytest.raises((StateError,ValueError,OverflowError)):day_value(value)
