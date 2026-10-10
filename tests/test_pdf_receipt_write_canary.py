from copy import deepcopy
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import Mock
import json
import pytest
from PIL import Image
from app import pdf_receipt_write_canary as c, receipt_pipeline as pipeline
from app.models import ReceiptResult
from app.drive_run_state import StateError
from app.sheets import HEADERS

CATS=[('食費','食品')]
class Transport:
    def __init__(self,value):self.payload=c.encoded(value);self.tag='"1"';self.writes=0
    def read_versioned(self):return self.payload,self.tag
    def replace_versioned(self,payload,tag,proposed):
        assert (payload,tag)==self.read_versioned()
        self.payload=proposed;self.writes+=1;self.tag='"'+str(self.writes+1)+'"'

class DB:
    def __init__(self):self.rows={t:[] for t in c.TABLES};self.appends=[];self.lost=None
    def categories(self):return CATS
    def get_raw(self,rng):
        title=rng.split('!')[0].strip("'")
        if '1:' in rng:return [HEADERS[title]] if '!M1:' not in rng else [['計上状態']]
        return deepcopy(self.rows[title])
    def append_raw(self,title,rows):
        self.rows[title].extend(deepcopy(rows));self.appends.append((title,deepcopy(rows)))
        if self.lost==title:raise TimeoutError('synthetic ambiguous delivery')

def context(monkeypatch):
    monkeypatch.setattr(pipeline,'evaluate_receipt_privacy',Mock(return_value=SimpleNamespace(
        classification='normal',gemini_allowed=True,buyback_evidence=False)))
    manifest={'source_file_id':'synthetic-pdf','source_content_hash':'a'*64,'grouping_revision':2,
        'confirmation_digest':'b'*64,'unit_ids':{'11':'stable-unit'},'page_identities':{'11':'c'*64}}
    transport=Transport(dict(schema=c.SCHEMA,manifest=manifest,records={}))
    store=c.CanaryStore(transport,manifest);db=DB()
    result=ReceiptResult(date='2026-09-26',total=159,items=[dict(name='商品',amount=159,
        major_category='食費',minor_category='食品')])
    out=BytesIO();Image.new('RGB',(2,2)).save(out,format='PNG');png=out.getvalue()
    proof=dict(unit_id='stable-unit',page_identity='c'*64,page_number=11,source_content_hash='a'*64,
        confirmation_digest='b'*64,grouping_revision=2,human_classification='normal',effective_classification='normal')
    fresh=lambda _:('stable-unit',png,result.model_copy(deep=True),CATS,deepcopy(proof))
    return store,db,fresh,Mock(),transport,proof

def test_real_pipeline_p11_readback_and_replay_zero_writes(monkeypatch):
    store,db,fresh,verify,t,_=context(monkeypatch)
    report=c.run_canary(store,db,11,fresh,verify)
    assert report['appended']==3 and report['payment_method']=='' and report['readback']
    assert [x[0] for x in db.appends]==['レシート','支出明細','取込データ']
    assert db.rows['レシート'][0][2]=='' and db.rows['支出明細'][0][7]==''
    count=t.writes;appends=deepcopy(db.appends)
    assert c.run_canary(store,db,11,fresh,verify)['status']=='replayed'
    assert db.appends==appends and t.writes==count

@pytest.mark.parametrize('page',[1,4,10,14,15])
def test_medical_and_privacy_held_pages_never_reach_writer(monkeypatch,page):
    store,db,fresh,verify,t,_=context(monkeypatch)
    fresh=Mock(wraps=fresh)
    with pytest.raises(StateError,match='page_not_allowed'):c.run_canary(store,db,page,fresh,verify)
    fresh.assert_not_called();assert not db.appends and t.writes==0

@pytest.mark.parametrize('field,value',[('source_content_hash','d'*64),('page_identity','d'*64),
    ('grouping_revision',3),('human_classification','medical'),('effective_classification','sensitive_unknown')])
def test_stale_or_sensitive_authority_never_writes(monkeypatch,field,value):
    store,db,fresh,verify,t,proof=context(monkeypatch);proof[field]=value
    with pytest.raises(StateError,match='authority_mismatch'):c.run_canary(store,db,11,fresh,verify)
    assert not db.appends and t.writes==0

def test_exact_privacy_failure_has_no_intent_or_accounting_write(monkeypatch):
    store,db,fresh,verify,t,_=context(monkeypatch)
    monkeypatch.setattr(pipeline,'evaluate_receipt_privacy',Mock(return_value=SimpleNamespace(
        classification='sensitive_unknown',gemini_allowed=False,reason_code='insufficient',
        extraction_status='failed',extraction_method='image_ocr',text_present=False,
        medical_payment_amount=None,medical_candidate_count=0,category=None)))
    with pytest.raises(StateError,match='validation_failed'):c.run_canary(store,db,11,fresh,verify)
    assert not db.appends and t.writes==0

def test_store_failure_and_source_change_before_write_are_fail_closed(monkeypatch):
    store,db,fresh,verify,t,_=context(monkeypatch)
    t.replace_versioned=Mock(side_effect=StateError('state_changed_since_read'))
    with pytest.raises(StateError):c.run_canary(store,db,11,fresh,verify)
    assert not db.appends
    store,db,fresh,verify,t,_=context(monkeypatch)
    verify.side_effect=[None,StateError('source_changed')]
    with pytest.raises(StateError):c.run_canary(store,db,11,fresh,verify)
    assert not db.appends and t.writes==1

def test_duplicate_and_changed_replay_never_overwrite(monkeypatch):
    store,db,fresh,verify,t,_=context(monkeypatch)
    db.rows['支出明細']=[['another','2026-09-26','','商品',159]+['']*7+['active']]
    with pytest.raises(StateError,match='possible_duplicate'):c.run_canary(store,db,11,fresh,verify)
    assert not db.appends and t.writes==0
    store,db,fresh,verify,t,_=context(monkeypatch)
    c.run_canary(store,db,11,fresh,verify);db.rows['支出明細'][0][4]=160
    before=deepcopy(db.appends)
    with pytest.raises(StateError,match='content_conflict'):c.run_canary(store,db,11,fresh,verify)
    assert db.appends==before

def test_ambiguous_append_readback_no_resend(monkeypatch):
    store,db,fresh,verify,t,_=context(monkeypatch);db.lost='支出明細'
    assert c.run_canary(store,db,11,fresh,verify)['readback']
    assert len(db.appends)==3 and len(db.rows['支出明細'])==1

def test_pending_intent_never_auto_retries(monkeypatch):
    store,db,fresh,verify,t,_=context(monkeypatch)
    c.run_canary(store,db,11,fresh,verify)
    store.value['records']['stable-unit']['phase']='pending';store.save(store.value)
    before=deepcopy(db.appends)
    with pytest.raises(StateError,match='pending_reconciliation'):c.run_canary(store,db,11,fresh,verify)
    assert db.appends==before
