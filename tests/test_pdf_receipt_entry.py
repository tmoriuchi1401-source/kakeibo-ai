"""Existing production scan -> source plan -> receipt writer/replay boundary."""
from copy import deepcopy
from hashlib import sha256
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from app import receipt_confirmation_production as scan,production_source as source,drive_receipts
from app.receipt_pipeline import ReceiptPipeline
from app.receipt_confirmation import ReceiptConfirmation,TITLE,HEADERS
from app.drive_run_state import StateError
from test_pdf_unit_intake import setup
from test_receipt_pdf_units import local_ocr
from test_receipt_confirmation import Store,DB as ReviewDB


@pytest.mark.parametrize('confirmed',[False,True])
def test_scan_never_passes_whole_pdf_to_medical_or_normal_gate(confirmed,local_ocr,monkeypatch,tmp_path):
    service,live,g,db,sdk,state=setup(('normal','medical'),{2:'医療'},confirmed=confirmed)
    store=Store();reviewdb=ReviewDB();reviewdb.ensure_sheet(TITLE,HEADERS)
    settings=SimpleNamespace(spreadsheet_id='synthetic-sheet',receipt_drive_folder_id='synthetic-inbox')
    metadata=Mock(return_value={'id':'drive-source-id','mimeType':'application/pdf','parents':['synthetic-inbox'],'version':'1'})
    monkeypatch.setattr(scan,'open_context',lambda *a:(settings,store,reviewdb,metadata))
    reader=Mock();reader.files().list().execute.return_value={'files':[
        {'id':'drive-source-id','mimeType':'application/pdf','version':'1'}]}
    monkeypatch.setattr('app.google_clients.read_only_drive_service',lambda:reader)
    monkeypatch.setattr('app.google_clients.download_drive_file',lambda *a:live.content)
    monkeypatch.setattr('app.pdf_unit_runtime.open_intake',lambda *a,**kw:service)
    forbidden=Mock(side_effect=AssertionError('No whole-PDF privacy/Medical/owner route'))
    monkeypatch.setattr(scan,'evaluate_receipt_privacy',forbidden)
    monkeypatch.setattr(ReceiptConfirmation,'observe_medical',forbidden)
    monkeypatch.setattr(ReceiptConfirmation,'route_owner_intake',forbidden)
    monkeypatch.setattr(scan,'configure_ui',lambda *a:None)
    monkeypatch.setattr(scan,'sync_review_visibility',lambda *a:None)
    env={'PDF_UNIT_PROCESSING_ENABLED':'true','RECEIPT_SCAN_PLAN':str(tmp_path/'plan.json')}
    before=state.payload
    result=scan.execute(env,True)
    assert result['found']==1 and result['medical_detected']==result['medical_pending']==result['written']==0
    plans=json.loads((tmp_path/'plan.json').read_bytes())['sources']
    assert len(plans)==int(confirmed)
    if confirmed:
        entry=plans[0]
        assert entry['sha256']==sha256(live.content).hexdigest() and entry['pdf_preflight']['page_count']==2
        assert entry['pdf_preflight']['pages'][1]['classification']=='medical'
        assert (tmp_path/(sha256(b'drive-source-id').hexdigest()+'.bin')).read_bytes()==live.content
    else:assert not list(tmp_path.glob('*.bin'))
    assert not list(tmp_path.glob('*.png')) and sdk.call_count==0 and not db.appends and state.payload==before
    forbidden.assert_not_called();reader.files().update.assert_not_called()


def test_production_source_uses_scan_snapshot_normal_unit_then_replay_without_refresh(local_ocr,monkeypatch,tmp_path):
    service,live,g,db,sdk,state=setup(('normal','medical'),{2:'医療'})
    view=service.preview(live.content,'drive-source-id')
    content=tmp_path/'source.bin';content.write_bytes(live.content)
    plan=tmp_path/'plan.json';plan.write_text(json.dumps({'sources':[{'source_id':'drive-source-id',
        'version':'1','mime_type':'application/pdf','path':str(content),'sha256':sha256(live.content).hexdigest(),
        'pdf_preflight':view['pdf_preflight']}]}))
    monkeypatch.setenv('RECEIPT_CONFIRMATION_BINDING','configured')
    monkeypatch.setenv('RECEIPT_SCAN_PLAN',str(plan))
    monkeypatch.setattr(source,'SheetsDB',lambda *a,**kw:db)
    monkeypatch.setattr(source,'make_receipt_pipeline',lambda *a:ReceiptPipeline(db,None,pdf_unit_intake=service))
    api=Mock();api.files().list().execute.return_value={'files':[{'id':'drive-source-id','version':'1',
        'mimeType':'application/pdf','parents':['synthetic-inbox'],'name':'PRIVATE_NAME'}]}
    monkeypatch.setattr(drive_receipts,'drive_service',lambda:api)
    download=Mock(side_effect=AssertionError('Only the fresh authority reader can re-download original'))
    monkeypatch.setattr(drive_receipts,'download_drive_file',download)
    refresh=Mock();monkeypatch.setattr('app.expense_view.ExpenseViewPipeline',lambda *a:SimpleNamespace(refresh=refresh))
    settings=SimpleNamespace(validate=Mock(),spreadsheet_id='synthetic-sheet',
        receipt_drive_folder_id='synthetic-inbox',processed_drive_folder_id='synthetic-processed')
    result=source.receipts(settings,apply=True)
    assert result==dict(found=1,written=1,needs_review=1,unchanged=0,failure=0)
    before=(sdk.call_count,len(db.appends),state.payload)
    replay=source.receipts(settings,apply=True)
    assert replay==dict(found=1,written=0,needs_review=1,unchanged=1,failure=0)
    assert before==(sdk.call_count,len(db.appends),state.payload) and refresh.call_count==1
    assert 'PRIVATE_NAME' not in json.dumps(result)
    api.files().update.assert_not_called();download.assert_not_called()


def test_unknown_unit_delivery_stops_later_source_processing_without_moving_parent(monkeypatch):
    api=Mock();api.files().list().execute.return_value={'files':[{'id':str(i),'name':'private',
        'mimeType':'application/pdf','parents':['synthetic-inbox']} for i in range(2)]}
    monkeypatch.setattr(drive_receipts,'drive_service',lambda:api)
    monkeypatch.setattr(drive_receipts,'download_drive_file',lambda *a:b'synthetic')
    pipeline=Mock();pipeline.process_bytes.return_value={'document_type':'pdf_page_units',
        'units':[{'unit_id':'PRIVATE_UNIT','status':'analysis_or_write_held'}],'archive_allowed':False}
    progress=[]
    with pytest.raises(StateError,match='pdf_unit_reconciliation_required'):
        drive_receipts.process_inbox('synthetic-inbox',pipeline,'synthetic-processed',
            progress=lambda stage,result=None:progress.append((stage,result)))
    assert pipeline.process_bytes.call_count==1 and 'PRIVATE_UNIT' not in json.dumps(progress)
    api.files().update.assert_not_called()


def test_readonly_production_preview_never_uses_whole_pdf_gate(local_ocr,monkeypatch):
    service,live,g,db,sdk,state=setup(('normal','medical'),{2:'医療'});before=state.payload
    settings=SimpleNamespace(validate=Mock(),spreadsheet_id='synthetic-sheet',receipt_drive_folder_id='synthetic-inbox')
    db.import_ids=lambda:set();monkeypatch.setattr(source,'SheetsDB',lambda *a,**kw:db)
    reader=Mock();reader.files().list().execute.return_value={'files':[{'id':'drive-source-id','mimeType':'application/pdf'}]}
    monkeypatch.setattr(source,'read_only_drive_service',lambda:reader)
    monkeypatch.setattr(source,'download_drive_file',lambda *a,**kw:live.content)
    monkeypatch.setenv('PDF_UNIT_PROCESSING_ENABLED','true')
    monkeypatch.setattr('app.pdf_unit_runtime.open_intake',lambda *a,**kw:service)
    gate=Mock(side_effect=AssertionError('No whole PDF gate'));monkeypatch.setattr(source,'evaluate_receipt_privacy',gate)
    counts=source.receipts(settings,apply=False)
    assert counts==dict(found=1,written=0,needs_review=1,unchanged=0,failure=0,new_eligible=1)
    assert state.payload==before and sdk.call_count==0 and not db.appends
    gate.assert_not_called()
