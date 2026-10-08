from copy import deepcopy
import json
from types import SimpleNamespace
import pytest
from app.drive_run_state import StateError
from app.receipt_audit import digest
from app.page_receipt_model import stable_page
from app.pdf_page_review import SCHEMA
from app.pdf_unit_readonly_analysis import SOURCE_KEY
from services.human_general import reconciliation_readers as readers
from test_human_general_real_page import fixture
from test_human_general_auth_transport import keys


def setup(monkeypatch,keys):
    _,_,drive_config,info=fixture(monkeypatch,keys)
    h=drive_config['page']['source']['source_content_hash'];monkeypatch.setattr(readers,'HASH',h)
    from app.pdf_page_identity import page_identity
    identity=dict(source_file_id=readers.SOURCE,source_content_hash=h,page_count=14,page_number=1,
        page_identity=page_identity(h,1,14),receipt_unit_id='',review_identity=digest('protected review'),revision=2)
    token=digest('medical card');rec_token=digest('comparison card')
    input_values=dict(date=46299,facility='Synthetic facility',amount=159,category='医療',payment='',memo='')
    config=dict(target_identity=identity,drive=drive_config,medical_token=token,reconciliation_token=rec_token,
        input_digest=digest(input_values),comparison_ledger_id='R-1U23lqFfSFJNVFfdeFMlrUR28059HHThI-01',
        ledger_snapshot_digest=digest('placeholder'),linked_rows_digest=digest('placeholder'),
        receipt_id='R-1U23lqFfSFJNVFfdeFMlrUR28059HHThI',import_id='receipt:1U23lqFfSFJNVFfdeFMlrUR28059HHThI')
    medical_identity=dict(source_file_id=readers.SOURCE,page_numbers=[1],kind='medical')
    ledger=[config['comparison_ledger_id'],'2026-10-05','Synthetic facility','item',159,'医療','通院','','manual',
        config['receipt_id'],config['import_id'],'','active']
    receipt=[config['receipt_id'],'synthetic'];import_row=[config['import_id'],'synthetic']
    config['ledger_snapshot_digest']=digest(ledger);config['linked_rows_digest']=digest([ledger,receipt,import_row])
    rec_identity={**identity,'candidate_ledger_id':config['comparison_ledger_id'],
        'ledger_snapshot_digest':config['ledger_snapshot_digest']}
    ui=[[f,v,SCHEMA,token,json.dumps(medical_identity),f] for f,v in input_values.items()]
    ui.append(['判定','同一レシート',SCHEMA,rec_token,json.dumps(rec_identity),'reconciliation_decision'])
    tables=[ui,[['header'],ledger],[['header'],receipt],[['header'],import_row]]
    source=b'synthetic source with immutable fourteen-page baseline'
    proposal=dict(source_content_hash=h,page_count=14,pages=[dict(page_identity=identity['page_identity'],human_classification='medical')])
    class Drive:
        def request(self,*args,**kwargs):return 'GET only'
        def begin_request(self):pass
        def end_request(self):pass
        def acl(self):pass
        def fresh(self):pass
        def read(self,_):return json.dumps({'records':{SOURCE_KEY:{'proposal':proposal}}}).encode(),'"v1"'
        def source(self,sid):assert sid==readers.SOURCE;return source
        def refresh_before_write(self):pass
    class Sheets:
        def __init__(self):self.calls=[]
        def get(self,url,**kwargs):
            self.calls.append((url,kwargs));assert url.endswith('/'+readers.SID+'/values:batchGet')
            return SimpleNamespace(status_code=200,json=lambda:dict(valueRanges=[{'values':deepcopy(r)} for r in tables]))
    sheets=Sheets();reader=readers.ReconciliationReaders(config,info,drive=Drive(),sheets=sheets)
    return reader,config,tables,proposal


def test_p1_readers_original_input_and_exact_linked_rows_only(monkeypatch,keys):
    r,c,t,p=setup(monkeypatch,keys)
    assert r.current(readers.SOURCE,1,'')==c['target_identity']
    assert r.ledger(c['comparison_ledger_id'])['readback_complete'] and r.decision()=='same'
    assert len(r.sheets.calls)==1
    with pytest.raises(StateError):r.drive.request('any',put=b'{}')
    with pytest.raises(StateError):r.current(readers.SOURCE,4,'')
    with pytest.raises(StateError):r.ledger('R-other')


@pytest.mark.parametrize('changed',['input','page_kind','page_identity','ledger','receipt','import','duplicate','decision_identity'])
def test_stale_ambiguous_fresh_data_rejected(monkeypatch,keys,changed):
    r,c,t,p=setup(monkeypatch,keys)
    if changed=='input':t[0][0][1]+=1
    elif changed=='page_kind':p['pages'][0]['human_classification']='normal'
    elif changed=='page_identity':p['pages'][0]['page_identity']=digest('wrong page')
    elif changed=='ledger':t[1][1][4]+=1
    elif changed=='receipt':t[2][1][1]='changed'
    elif changed=='import':t[3][1][1]='changed'
    elif changed=='duplicate':t[1].append(deepcopy(t[1][1]))
    elif changed=='decision_identity':t[0][-1][4]=json.dumps({**c['target_identity'],'revision':3})
    with pytest.raises(StateError):
        r.current(readers.SOURCE,1,'');r.ledger(c['comparison_ledger_id']);r.decision()


def test_request_scoped_cache_cannot_hide_input_change_before_commit(monkeypatch,keys):
    r,c,t,p=setup(monkeypatch,keys);r.current(readers.SOURCE,1,'');t[0][0][1]+=1
    with pytest.raises(StateError):r.refresh()
