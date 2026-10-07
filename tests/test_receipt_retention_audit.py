from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
from uuid import uuid4
from types import SimpleNamespace
import pytest
from app.drive_run_state import StateError
from app import receipt_audit as audit
from app.receipt_retention import metadata, cleanup_dry_run, POLICIES, PERMANENT, event_capacity
from app.receipt_reconciliation_authority import (binding, ReconciliationConfirmation,
                                                confirmed_distinct_pair)
from app.human_general_auth_transport import VerifiedActor, ISSUER
from app.pdf_page_identity import page_identity
from app.receipt_reconciliation_ui import reconciliation_card, visible_cards, visibility_requests

NOW = datetime(2026,10,8,0,0,0,tzinfo=timezone.utc)


def identity(number=1):
    h = audit.digest('synthetic source')
    return dict(source_file_id='synthetic-receipt-source', source_content_hash=h, page_count=14,
                page_number=number, page_identity=page_identity(h,number,14), receipt_unit_id='',
                review_identity=audit.digest(['review',number]), revision=2)


class Rig:
    def __init__(self, decision='same', repo=None, number=1):
        self.ident = identity(number); self.rid = str(uuid4()); self.ledger = 'R-existing-receipt-01'
        self.b = binding(self.ident, request_id=self.rid, candidate_ledger_id=self.ledger,
                         ledger_snapshot_digest=audit.digest('existing exact rows'), decision=decision)
        self.record = dict(binding=self.b,digest=audit.digest(self.b),created_at='2026-10-07T23:55:00+00:00',
                           expires_at='2026-10-08T00:05:00+00:00')
        self.actor = VerifiedActor(ISSUER,'123456789','owner@example.test','google_oidc_code_pkce_v1',
                                  int(NOW.timestamp()),self.rid,self.record['digest'],1)
        self.owner = self.actor.actor_id
        self.repo = repo or audit.MemoryAuditRepository()
        self.target = dict(ledger_id=self.ledger,snapshot_digest=self.b['ledger_snapshot_digest'],active=True,readback_complete=True)
        self.source_hash = self.ident['source_content_hash']
        self.current = deepcopy(self.ident)
        self.now = NOW

    def service(self):
        return ReconciliationConfirmation(self.repo,trusted_request=lambda _:self.record,
            verified_actor=lambda _:self.actor,current_identity=lambda *_:self.current,
            load_source_hash=lambda _:self.source_hash,ledger_readback=lambda _:self.target,
            owner_actor_id=self.owner,clock=lambda:self.now)

    def confirm(self):
        choice = {'same':'同一レシート','different':'別のレシート','unknown':'判断できない'}[self.b['selected_decision']]
        return self.service().confirm(self.rid,selected_decision=choice,method='POST',explicit_post=True)


def sample_event(): return Rig().confirm()['event']


def test_same_binding_append_current_replay_and_year_lookup():
    r=Rig(); first=r.confirm(); state=r.repo.get_current(r.ident)
    assert first['status']=='reconciled_existing' and first['accounting_write']==0
    assert state['ledger_id']==r.ledger and not audit.needs_attention(state)
    assert state['last_event']['year']=='2026'
    assert r.repo.get_event(state['last_event'])==first['event']
    assert r.confirm()['replayed'] is True
    assert r.repo.writes==1 and len(r.repo.events)==len(r.repo.receipts)==1
    assert state==r.repo.get_current(r.ident)


def test_distinct_only_its_pair_and_stays_visible_until_posting():
    r=Rig('different'); assert r.confirm()['status']=='confirmed_distinct'
    assert confirmed_distinct_pair(r.repo,r.ident,r.ledger)
    assert not confirmed_distinct_pair(r.repo,r.ident,'R-other-01')
    assert not confirmed_distinct_pair(r.repo,identity(4),r.ledger)
    current=r.repo.get_current(r.ident);assert audit.needs_attention(current)
    rid=str(uuid4())
    value=audit.event(r.ident,request_id=rid,request_digest=audit.digest('posting'),event_type='imported',
        ledger_id='R-new-01',decision='confirmed',actor_id=r.owner,confirmed_at=NOW.isoformat(),
        authority_digest=audit.digest('existing writer readback'),reason_code='writer_exact_readback')
    r.repo.commit(value,current['state_revision'])
    assert not audit.needs_attention(r.repo.get_current(r.ident))
    assert confirmed_distinct_pair(r.repo,r.ident,r.ledger)


def test_unknown_creates_no_terminal_event_or_authority():
    r=Rig('unknown'); assert r.confirm()['status']=='reconciliation_required'
    assert r.confirm()['event'] is None and r.repo.writes==0
    assert r.repo.events==r.repo.receipts==r.repo.current=={}
    assert audit.needs_attention(r.repo.get_current(r.ident))


@pytest.mark.parametrize('mutation', ['actor_missing','actor_other','actor_dict','uuid','decision','source',
                                     'page','review','revision','ledger','expired','post'])
def test_reconciliation_rejects_unknown_actor_tamper_stale(mutation):
    r=Rig()
    if mutation=='actor_missing':r.actor=None
    elif mutation=='actor_other':r.actor=replace(r.actor,subject='987654321')
    elif mutation=='actor_dict':r.actor=r.actor.record()
    elif mutation=='uuid':r.rid=str(uuid4())
    elif mutation=='decision':r.b['selected_decision']='different'
    elif mutation=='source':r.source_hash=audit.digest('changed PDF')
    elif mutation=='page':r.current=identity(4)
    elif mutation=='review':r.current['review_identity']=audit.digest('changed review')
    elif mutation=='revision':r.current['revision']+=1
    elif mutation=='ledger':r.target['snapshot_digest']=audit.digest('row modified')
    elif mutation=='expired':r.now=datetime(2026,10,9,tzinfo=timezone.utc)
    with pytest.raises(StateError):
        if mutation=='post':r.service().confirm(r.rid,selected_decision='同一レシート',method='GET',explicit_post=True)
        else:r.confirm()
    assert r.repo.writes==0


def test_one_p1_gate_does_not_block_p4_current_state():
    repo=audit.MemoryAuditRepository(); p1=Rig('unknown',repo,1);p4=Rig('same',repo,4)
    p1.confirm();p4.confirm()
    assert audit.needs_attention(repo.get_current(p1.ident))
    assert not audit.needs_attention(repo.get_current(p4.ident))


def test_request_uuid_durable_index_survives_year_boundary_and_ttl():
    r=Rig();e=r.confirm()['event'];size=r.repo.writes
    changed=deepcopy(e);changed['confirmed_at']='2027-01-01T00:00:00+00:00'
    changed['event_digest']=audit.digest({k:v for k,v in changed.items() if k!='event_digest'})
    with pytest.raises(StateError,match='reuse'):r.repo.commit(changed,1)
    assert r.repo.writes==size and r.repo.lookup(r.rid)['event_ref']['year']=='2026'
    assert 'expires_at' not in r.repo.lookup(r.rid)


def test_cas_conflict_and_different_uuid_same_pair_do_not_append():
    r=Rig();e=sample_event()
    with pytest.raises(StateError,match='412'):r.repo.commit(e,10)
    assert r.repo.writes==0
    r.confirm();r2=Rig(repo=r.repo)
    with pytest.raises(StateError):r2.confirm()
    assert r.repo.writes==1


@pytest.mark.parametrize('field', ['image_bytes','pdf_bytes','raw_response','ocr_text','token','cookie','ui_snapshot','patient_name'])
def test_long_event_rejects_every_extra_field_even_if_digest_recomputed(field):
    e=sample_event();e[field]='do not persist'
    e['event_digest']=audit.digest({k:v for k,v in e.items() if k!='event_digest'})
    with pytest.raises(StateError):audit.validate_event(e)


def test_size_budget_and_bounded_provenance():
    e=sample_event();assert len(audit.encoded(e))<=audit.MAX_EVENT_BYTES
    out=event_capacity([e]);assert out['estimated_bytes']['1000000']==out['mean_bytes']*1_000_000
    e['provenance']={'date':'human_override','amount':'human','category':'gemini'}
    e['event_digest']=audit.digest({k:v for k,v in e.items() if k!='event_digest'});audit.validate_event(e)
    e['provenance']['memo']='raw OCR text'
    e['event_digest']=audit.digest({k:v for k,v in e.items() if k!='event_digest'})
    with pytest.raises(StateError):audit.validate_event(e)


@pytest.mark.parametrize('kind',sorted(PERMANENT))
def test_cleanup_cannot_delete_permanent_even_if_mislabeled_expired(kind):
    item=metadata(kind,'2020-01-01T00:00:00+00:00',object_id='original',byte_size=1000)
    item.update(retention_class='ephemeral',expires_at='2020-01-01T00:01:00+00:00')
    result=cleanup_dry_run([item],NOW.isoformat())
    assert result['protected_count']==1 and result['candidate_count']==result['deleted']==0


def test_cleanup_expiry_namespace_legacy_unknown_and_wrong_ttl_fail_closed():
    due=metadata('analysis_details','2026-01-01T00:00:00+00:00',object_id='old',byte_size=1000)
    fresh=metadata('oauth_session',NOW.isoformat(),object_id='fresh',byte_size=100)
    wrong=deepcopy(due);wrong['expires_at']=NOW.isoformat()
    other=deepcopy(due);other['namespace']='source-originals'
    result=cleanup_dry_run([due,fresh,wrong,other,{'object_id':'legacy'}],NOW.isoformat())
    assert result['candidate_count']==1 and result['candidate_bytes']==1000 and result['rejected_count']==3
    assert result['counts_by_kind']=={'analysis_details':1} and result['deleted']==0


def test_ui_reuses_existing_card_and_hides_rows_without_clear_or_delete():
    r=Rig();card=reconciliation_card(r.ident,r.ledger,r.b['ledger_snapshot_digest'])
    assert [x[0] for x in card['rows']].count('reconciliation_decision')==1
    assert [x[2] for x in card['rows'] if x[0]=='reconciliation_decision']==['判断できない']
    r.confirm();state=r.repo.get_current(r.ident)
    assert visible_cards([card],{audit.entity_key(r.ident):state})==[]
    unrelated={'identity':{'kind':'general_manual'},'token':'other','rows':[]}
    assert visible_cards([card,unrelated],{audit.entity_key(r.ident):state})==[unrelated]
    rows=[[label,value,card['identity']['schema'],card['token']] for _,label,value in card['rows']]
    requests=visibility_requests(rows,{card['token']:state})
    assert len(requests)==len(rows)
    assert all(set(q)=={'updateDimensionProperties'} and q['updateDimensionProperties']['properties']=={'hiddenByUser':True} for q in requests)
    assert 'delete' not in str(requests).lower() and 'clear' not in str(requests).lower()


class FakeDocument:
    def __init__(self,client,path):self.client,self.path=client,path
    def collection(self,name):return FakeDocument(self.client,self.path+'/'+name)
    def document(self,name):return FakeDocument(self.client,self.path+'/'+name)
    def get(self,**kwargs):
        value=deepcopy(self.client.docs.get(self.path))
        self.client.reads.append(self.path)
        return SimpleNamespace(exists=value is not None,to_dict=lambda:value)


class FakeTransaction:
    def __init__(self,client):self.client=client;self.ops=[]
    def create(self,ref,value):self.ops.append(('create',ref.path,deepcopy(value)))
    def set(self,ref,value):self.ops.append(('set',ref.path,deepcopy(value)))
    def commit(self):
        self.client.commits+=1
        if self.client.fail=='before':raise TimeoutError()
        copied=deepcopy(self.client.docs)
        for op,path,value in self.ops:
            if op=='create' and path in copied:raise StateError('HTTP_412')
            copied[path]=value
        self.client.docs=copied
        if self.client.fail=='after':raise TimeoutError()


class FakeClient:
    def __init__(self):self.docs={};self.reads=[];self.commits=0;self.fail=None
    def collection(self,name):return FakeDocument(self,name)
    def transaction(self,**kwargs):
        assert kwargs=={'max_attempts':1}
        return FakeTransaction(self)


@pytest.fixture
def firestore_repo(monkeypatch):
    from google.cloud import firestore
    from app.receipt_audit_firestore import FirestoreAuditRepository
    def transactional(fn):
        def run(tx):
            result=fn(tx);tx.commit();return result
        return run
    monkeypatch.setattr(firestore,'transactional',transactional)
    client=FakeClient()
    return FirestoreAuditRepository(client,audit.digest('scope'),write_enabled=True),client


def test_firestore_atomic_layout_readback_replay_and_current_no_history_scan(firestore_repo):
    repo,client=firestore_repo;r=Rig(repo=repo);first=r.confirm()
    assert first['replayed'] is False and len(client.docs)==4
    assert '/years/2026/events/' in next(p for p in client.docs if '/events/' in p)
    before=deepcopy(client.docs);assert r.confirm()['replayed'] is True
    assert client.docs==before and client.commits==1
    client.reads=[];repo.get_current(r.ident)
    assert len(client.reads)==1 and '/current/' in client.reads[0]
    assert all('expires_at' not in v for v in client.docs.values())


@pytest.mark.parametrize('failure',['before','after'])
def test_atomic_failure_unknown_outcome_readback_no_retry(firestore_repo,failure):
    repo,client=firestore_repo;r=Rig(repo=repo);client.fail=failure
    with pytest.raises(StateError,match='unknown'):r.confirm()
    assert client.commits==1
    marker=repo.lookup(r.rid)
    assert bool(marker)==(failure=='after')
    if marker:assert repo.get_event(marker['event_ref'])['event_digest']==marker['event_digest']
    assert len(client.docs)==(4 if failure=='after' else 0)


def test_live_adapter_disabled_by_default():
    from app.receipt_audit_firestore import FirestoreAuditRepository
    repo=FirestoreAuditRepository(FakeClient(),audit.digest('scope'))
    with pytest.raises(StateError,match='disabled'):repo.commit(sample_event(),0)


def test_existing_pdf_card_publisher_provides_exact_dropdown_no_new_sheet():
    from app.pdf_page_review import PageReviewSheet, SCHEMA
    from test_pdf_grouping_transport_ui import FakeSheets
    r=Rig();card=reconciliation_card(r.ident,r.ledger,r.b['ledger_snapshot_digest'])
    native=FakeSheets();sheet=PageReviewSheet(native,'synthetic-management',[])
    old=[[label,value,SCHEMA,card['token'],'identity',field] for field,label,value in card['rows']]
    next(row for row in old if row[5]=='reconciliation_decision')[1]='別のレシート'
    sheet._rows=lambda:[['項目','内容'],*old]
    sheet.publish_cards([card])
    rules=[x['setDataValidation']['rule'] for x in native.formats if x.get('setDataValidation',{}).get('rule')]
    assert any(x['condition']['type']=='ONE_OF_LIST' and x['condition']['values']==[
        {'userEnteredValue':s} for s in ('同一レシート','別のレシート','判断できない')] and x['strict'] for x in rules)
    assert not any('addSheet' in x for x in native.formats)
    rows=native.writes[-1][0]['values']
    assert next(row for row in rows if len(row)>5 and row[5]=='reconciliation_decision')[1]=='別のレシート'


def test_ui_failure_after_commit_replay_never_appends_again():
    r=Rig();r.confirm();before=deepcopy(r.repo.current)
    # UI is a projection, not part of the confirmation or accounting operation.
    with pytest.raises(OSError):raise OSError('synthetic UI refresh failure')
    assert r.confirm()['replayed'] is True
    assert r.repo.writes==1 and r.repo.current==before


def test_concurrent_same_confirmation_creates_exactly_one_event():
    from concurrent.futures import ThreadPoolExecutor
    repo=audit.MemoryAuditRepository();value=sample_event()
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(lambda _:repo.commit(value,0),range(2)))
    assert sorted(x['replayed'] for x in results)==[False,True]
    assert len(repo.events)==len(repo.receipts)==repo.writes==1


def test_size_limit_and_unknown_current_fail_closed(monkeypatch):
    e=sample_event();monkeypatch.setattr(audit,'MAX_EVENT_BYTES',len(audit.encoded(e))-1)
    with pytest.raises(StateError):audit.validate_event(e)
    state=audit.pending(identity());state['status']='mysterious'
    with pytest.raises(StateError):audit.needs_attention(state)


@pytest.mark.parametrize('timestamp',['2026-01-01T00:00:00','2026-01-01T09:00:00+09:00'])
def test_year_partition_requires_explicit_utc(timestamp):
    e=sample_event();e['confirmed_at']=timestamp
    with pytest.raises(StateError):audit.partition(e)


def test_cleanup_malformed_entries_are_retained():
    result=cleanup_dry_run([None,'legacy',42],NOW.isoformat())
    assert result['rejected_count']==3 and result['candidate_count']==result['deleted']==0


@pytest.mark.parametrize('field,value',[('reason_code','explicit_hga_ai_consent'),('ledger_id','')])
def test_event_cannot_masquerade_as_another_decision(field,value):
    e=sample_event();e[field]=value
    e['event_digest']=audit.digest({k:v for k,v in e.items() if k!='event_digest'})
    with pytest.raises(StateError):audit.validate_event(e)


@pytest.mark.parametrize('revision',[True,-1,'0'])
def test_cas_revision_must_be_nonnegative_integer(revision,firestore_repo):
    for repo in (audit.MemoryAuditRepository(),firestore_repo[0]):
        with pytest.raises(StateError,match='revision'):repo.commit(sample_event(),revision)
    assert firestore_repo[1].commits==0
