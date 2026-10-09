"""Different item categories, request binding and actual legacy writer recovery."""
from copy import deepcopy
import json
import time
from types import SimpleNamespace
import pytest
from app import receipt_item_review as r, receipt_item_review_ui as ui, receipt_item_queue as queue
from app import general_receipt_completion as legacy, receipt_pipeline
from app.page_receipt_model import digest
from app.models import ReceiptResult,ReceiptItem
from app.drive_run_state import StateError
from app.pdf_receipt_write_canary import PlanningDB
from app.pdf_grouping_ui import QUEUE
from app.human_general_auth_transport import VerifiedActor,ISSUER
from test_general_receipt_completion import setup, memory
from test_page_receipts import BINDING,UUID
from test_receipt_pipeline import _normal_gate
from test_human_general_auth_transport import Rig,keys

CATS=[('食費','食品'),('日用品','消耗品')]
SECOND='00000000-0000-4000-8000-000000000002'


def candidate(*,count=1,kind='normal',adjustment=False,unstable=False):
    from test_page_receipts import page
    p,raw=page(kind)
    p,raw,m,a,b=setup(count,(p,raw))
    for extraction in (a,b):
        for located in extraction.receipts:
            located.receipt.total=730
            located.receipt.items=[ReceiptItem(name='牛乳',amount=250,major_category='食費',minor_category='食品'),
                                  ReceiptItem(name='洗剤',amount=480,major_category='日用品',minor_category='消耗品')]
            if adjustment:
                located.receipt.items.append(ReceiptItem(name='全体値引き',amount=-30,major_category='食費',minor_category='食品'))
                located.receipt.total=700
    one,two=a.receipts[0].receipt,b.receipts[-1].receipt
    if unstable:two.items[0].name='異なる商品'
    d=legacy.draft(p,m,m['units'][0]['receipt_unit_id'],[one,two],categories=CATS)
    return r.prepare(d,CATS),p,raw,m


def service(record,p,raw,manifest):
    owner={};transport=memory(r.empty_state(BINDING));now=int(time.time())
    # Reuse the real GeneralCompletion freshness/HGA verification boundary.
    fresh=legacy.GeneralCompletion(None,lambda *_:p,lambda _:raw,lambda _:manifest,lambda _:None,
                                  lambda:CATS,lambda _:None,lambda *_:False)
    s=r.ReviewRequests(r.ReviewStore(transport,BINDING,preflight=lambda:None),
        fresh=fresh.fresh,categories=lambda:CATS,read_current=lambda _:owner.get('current'),
        verified_actor=lambda rid,sha,ident: VerifiedActor(ISSUER,'synthetic-owner','owner@example.test',
            'google_oidc_code_pkce_v1',now,rid,digest([r.SCHEMA,'post_receipt',rid,sha,ident]),1) if owner.get(rid)==sha else None,
        duplicates=lambda *_:False,owner_actor_id=digest([ISSUER,'synthetic-owner']),clock=lambda:now)
    return s,transport,owner


def snap(record,**updates):
    f=r.fields(record);f.update(updates)
    return r.snapshot(r.card(record,categories=CATS),f)


def test_different_categories_prefill_and_not_broadcast_by_legacy_form():
    d,*_=candidate();f=r.fields(d)
    assert [i['category'] for i in f['items']]==['食費｜食品','日用品｜消耗品']
    final=r.evaluate(d,f,CATS)
    assert final['status']=='ready_to_confirm'
    assert [(i['major_category'],i['minor_category']) for i in final['parsed']['items']]==CATS
    old=legacy.evaluate(d['legacy'],{**d['legacy']['prefill'],'category':'食費｜食品'},CATS)
    assert old['status']=='needs_review' and 'item_category_review_required' in old['issues']
    assert [(i['major_category'],i['minor_category']) for i in old['parsed']['items']]==CATS


@pytest.mark.parametrize('problem',['low_confidence','disagree','invalid','single_read','legacy_mixed'])
def test_untrusted_category_blank(problem):
    d,p,raw,m=candidate();x=deepcopy(d['legacy'])
    if problem=='legacy_mixed':x.pop('item_category_evidence')
    else:
        x['item_category_evidence'][1].update(category='',corroborated=False)
    x.pop('candidate_digest');x['candidate_digest']=digest(x)
    review=r.prepare(x,CATS)
    assert review['items'][1]['category']==''
    final=r.evaluate(review,r.fields(review),CATS)
    assert final['status']=='needs_human_completion'


def test_actual_category_confidence_and_reread_disagreement():
    p,raw,m,a,b=setup()
    a.receipts[0].receipt.items[0].confidence=.4
    x=legacy.draft(p,m,m['units'][0]['receipt_unit_id'],[a.receipts[0].receipt,b.receipts[0].receipt],categories=CATS)
    assert not x['item_category_evidence'][0]['corroborated']
    a.receipts[0].receipt.items[0].confidence=.9
    b.receipts[0].receipt.items[0].minor_category='消耗品';b.receipts[0].receipt.items[0].major_category='日用品'
    x=legacy.draft(p,m,m['units'][0]['receipt_unit_id'],[a.receipts[0].receipt,b.receipts[0].receipt],categories=CATS)
    assert x['item_category_evidence'][0]['category']==''


def test_legacy_uniform_receipt_category_is_not_item_evidence():
    d,*_=candidate();x=deepcopy(d['legacy']);x.pop('item_category_evidence')
    x['prefill']['category']='食費｜食品'
    for item in x['parsed']['items']:item.update(major_category='食費',minor_category='食品')
    x.pop('candidate_digest');x['candidate_digest']=digest(x)
    assert all(i['category']=='' for i in r.prepare(x,CATS)['items'])


@pytest.mark.parametrize('case',['product','aggregate','conflict','structure'])
def test_existing_approved_product_rules_are_narrow_and_keep_gates(case):
    from app.category_rules import CategoryRule
    from datetime import datetime,timezone
    from dataclasses import replace
    d,*_=candidate(unstable=case=='structure')
    x=deepcopy(d['legacy']);x['prefill']['merchant']='試験店';x['parsed']['merchant']='試験店'
    x.pop('candidate_digest');x['candidate_digest']=digest(x);d=r.prepare(x,CATS)
    merchant=d['legacy']['prefill']['merchant']
    rule=CategoryRule('CR-synthetic','product','receipt','','',merchant,'','牛乳',None,
        CATS[0],'R-approved',datetime(2026,1,1,tzinfo=timezone.utc),1,True)
    rules=[rule]
    if case=='aggregate':rules=[replace(rule,kind='store_total',product_name='')]
    if case=='conflict':rules.append(replace(rule,rule_id='CR-conflict',category=CATS[1]))
    out=r.approved_product_prefill(d,CATS,rules,imported_at='2026-10-09T00:00:00+00:00')
    if case=='product':
        assert out['rule_evidence'][out['items'][0]['item_id']]['method']=='approved_product_rule'
        assert r.evaluate(out,r.fields(out),CATS)['item_category_provenance'][out['items'][0]['item_id']]=='human'
        assert out['items'][1]['category']=='日用品｜消耗品'
    elif case=='conflict':assert out['items'][0]['category']==''
    else:assert out==d


def test_owner_item_category_override_and_bulk_only_fills_blank():
    d,*_=candidate();f=r.fields(d);f['items'][0]['category']='日用品｜消耗品'
    f['items'][1]['category']=''
    f=r.bulk_category(d,f,'食費｜食品',CATS)
    assert [i['category'] for i in f['items']]==['日用品｜消耗品','食費｜食品']
    final=r.evaluate(d,f,CATS)
    assert all(x=='human_override' for x in final['item_category_provenance'].values())


@pytest.mark.parametrize('missing',['date','amount','item_category'])
def test_missing_required_only_yellow_and_completes(missing):
    d,*_=candidate();f=r.fields(d)
    if missing=='item_category':f['items'][1]['category']=''
    else:f[missing]=''
    c=r.card(d,f,categories=CATS);rows=ui.encoded_rows(c)
    rules=ui.rules_for_rows(rows,40)
    yellow=[rule for rule in rules if rule['booleanRule']['format']['backgroundColor']==ui.YELLOW]
    assert len(yellow)==4 and all(row[22]!='review' for row in rows)
    assert not any('merchant' in rule['booleanRule']['condition']['values'][0]['userEnteredValue'] for rule in rules)
    # Sheets evaluates these blank-only rules live. Filling the same cell
    # makes its LEN(TRIM(TO_TEXT(...)))=0 condition false, with no new trigger.
    assert all('=0' in rule['booleanRule']['condition']['values'][0]['userEnteredValue'] for rule in yellow)
    assert r.evaluate(d,f,CATS)['status']=='needs_human_completion'
    f=r.fields(d);assert r.evaluate(d,f,CATS)['status']=='ready_to_confirm'


def test_reread_gate_independent_from_complete_categories():
    d,*_=candidate(unstable=True);f=r.fields(d)
    assert all(i['category']=='' for i in f['items'])
    for i in f['items']:i['category']='食費｜食品'
    final=r.evaluate(d,f,CATS)
    assert final['required_fields_complete'] and final['status']=='needs_review'
    assert 'reread_item_structure_changed' in final['issues']
    colors=[rule['booleanRule']['format']['backgroundColor'] for rule in ui.rules_for_rows(ui.encoded_rows(r.card(d,f,categories=CATS)),0)]
    assert ui.ORANGE in colors and ui.YELLOW in colors


@pytest.mark.parametrize('change',['item_count','item_order','item_id','name','amount','source','digest'])
def test_item_and_candidate_tamper_rejected(change):
    d,*_=candidate();f=r.fields(d)
    if change=='item_count':f['items'].pop()
    if change=='item_order':f['items'].reverse()
    if change in {'item_id','name'}:f['items'][0][change]='changed'
    if change=='amount':f['items'][0]['amount']=999
    if change=='source':d['legacy']['identity']['source_content_hash']='f'*64
    if change=='digest':d['digest']='f'*64
    with pytest.raises(StateError):r.evaluate(d,f,CATS)


def test_global_discount_mixed_categories_held_and_printed_sign_preserved():
    d,*_=candidate(adjustment=True);f=r.fields(d)
    result=r.evaluate(d,f,CATS)
    assert 'tax_discount_allocation_review' in result['issues'] and result['status']=='needs_review'
    assert result['parsed']['items'][-1]['amount']==-30
    assert result['item_sum']==700 and len(result['parsed']['items'])==3
    for i in f['items']:i['category']='食費｜食品'
    assert r.evaluate(d,f,CATS)['status']=='ready_to_confirm'


def test_printed_external_tax_and_linked_discount_categories_not_double_counted():
    d,*_=candidate();x=deepcopy(d['legacy'])
    x['parsed']['items'].append(dict(name='外税',quantity=1,amount=73,major_category='食費',minor_category='食品',note='',confidence=.9))
    x['parsed']['total']=803;x['prefill']['amount']=803
    x['item_category_evidence'].append({'item_index':3,'category':'食費｜食品','corroborated':True})
    x.pop('candidate_digest');x['candidate_digest']=digest(x)
    d=r.prepare(x,CATS);assert r.evaluate(d,r.fields(d),CATS)['status']=='needs_review'
    d['adjustment_targets']={d['items'][2]['item_id']:d['items'][0]['item_id']}
    d.pop('digest');d['digest']=digest(d)
    result=r.evaluate(d,r.fields(d),CATS)
    assert result['status']=='ready_to_confirm' and result['item_sum']==803


@pytest.mark.parametrize('total',[0,731,'abc'])
def test_total_invalid_or_mismatch_not_repaired_with_fake_item(total):
    d,*_=candidate();f=r.fields(d);f['amount']=total
    out=r.evaluate(d,f,CATS);assert out['status']!='ready_to_confirm'
    assert len(d['items'])==2


@pytest.mark.parametrize('action',r.ACTIONS)
def test_dropdown_request_distinct_from_execution(action):
    d,p,raw,m=candidate();s,t,owner=service(d,p,raw,m);value=snap(d,action=action)
    owner['current']=value;owner[UUID]=digest(value)
    if action=='未選択':
        with pytest.raises(StateError):s.capture(UUID,d,value)
        assert t.writes==0
    else:
        out=s.capture(UUID,d,value)
        assert out['status']==('held' if action=='保留する' else 'validated_not_written')
        assert not out['plan']['posting_authority']
        with pytest.raises(StateError,match='live_write_disabled'):
            s.execute(UUID,d,existing_writer=lambda _:pytest.fail('writer'),accounting_readback=lambda _:None,append_history=lambda _:None)


@pytest.mark.parametrize('problem',['source','review','actor','snapshot','duplicate','unknown_duplicate'])
def test_backend_fresh_actor_duplicate_and_snapshot_gates(problem):
    d,p,raw,m=candidate();s,t,owner=service(d,p,raw,m);value=snap(d,action='記帳する')
    owner['current']=value;owner[UUID]=digest(value)
    if problem in {'source','review'}:s.fresh=lambda _:(_ for _ in ()).throw(StateError('stale_'+problem))
    if problem=='actor':owner.pop(UUID)
    if problem=='snapshot':owner['current']=snap(d,date='2026/10/06')
    if problem in {'duplicate','unknown_duplicate'}:
        s.duplicates=lambda *_:True if problem=='duplicate' else None
        result=s.capture(UUID,d,value);assert result['status']=='needs_review';return
    with pytest.raises(StateError):s.capture(UUID,d,value)
    assert t.writes==0


def test_capture_timeout_readback_replay_no_second_append():
    d,p,raw,m=candidate();s,t,owner=service(d,p,raw,m);value=snap(d,action='記帳する');owner['current']=value;owner[UUID]=digest(value)
    save=t.replace_versioned
    def unknown(*args):save(*args);raise TimeoutError('synthetic')
    t.replace_versioned=unknown
    with pytest.raises(TimeoutError):s.capture(UUID,d,value)
    before=t.payload,t.version,t.writes
    assert s.capture(UUID,d,value)['replayed'] and before==(t.payload,t.version,t.writes)
    owner[SECOND]=digest(value)
    with pytest.raises(StateError):s.capture(SECOND,d,value)


def test_conditional_412_does_not_retry():
    d,p,raw,m=candidate();s,t,owner=service(d,p,raw,m);value=snap(d,action='記帳する');owner['current']=value;owner[UUID]=digest(value)
    def race(*_):raise StateError('changed_since_read')
    t.replace_versioned=race
    with pytest.raises(StateError,match='changed_since_read'):s.capture(UUID,d,value)
    assert not s.store.load()['requests']


def test_actual_existing_writer_item_category_and_readback_recovery(monkeypatch):
    monkeypatch.setattr(receipt_pipeline,'evaluate_receipt_privacy',lambda *_args,**_kw:_normal_gate())
    d,p,raw,m=candidate();s,t,owner=service(d,p,raw,m);value=snap(d,action='記帳する');owner['current']=value;owner[UUID]=digest(value)
    request=s.capture(UUID,d,value);db=PlanningDB(CATS);events={};calls=[]
    def writer(request):
        calls.append(1);parsed=ReceiptResult.model_validate(request['plan']['parsed'])
        pipeline=receipt_pipeline.ReceiptPipeline(db,SimpleNamespace(analyze_receipt=lambda *_a,**_k:parsed),clock=lambda:'synthetic-clock')
        assert pipeline._process_image_bytes(b'synthetic-png','image/png',request['unit_id'],observe_medical=False)['status']=='imported'
        raise TimeoutError('response lost after actual writer returned')
    def readback(request):
        spend=[row for title,row in db.plan if title=='支出明細']
        return 'complete' if len(spend)==2 and [tuple(row[5:7]) for row in spend]==CATS else 'unknown'
    def history(request):events.setdefault(request['request_id'],{'event_type':'imported','ledger_id':'R-'+request['unit_id'],'authority_digest':digest(request['snapshot'])})
    out=s.execute(UUID,d,existing_writer=writer,accounting_readback=readback,append_history=history,execution_enabled=True)
    assert out['terminal'] and not out['visible_in_daily_review']
    assert len([x for x in db.plan if x[0]=='レシート'])==1 and len([x for x in db.plan if x[0]=='取込データ'])==1
    before=deepcopy(db.plan),t.writes
    assert s.execute(UUID,d,existing_writer=writer,accounting_readback=readback,append_history=history,execution_enabled=True)['replayed']
    assert len(calls)==len(events)==1 and before==(db.plan,t.writes)


@pytest.mark.parametrize('outcome',['unknown','partial','not_written'])
def test_failure_never_reappends_and_never_hides_card(outcome):
    d,p,raw,m=candidate();s,t,owner=service(d,p,raw,m);value=snap(d,action='記帳する');owner['current']=value;owner[UUID]=digest(value);calls=[]
    s.capture(UUID,d,value)
    def writer(_):calls.append(1);raise TimeoutError()
    kwargs=dict(existing_writer=writer,accounting_readback=lambda _:outcome,append_history=lambda _:pytest.fail('history'),execution_enabled=True)
    out=s.execute(UUID,d,**kwargs)
    assert out['visible_in_daily_review'] and not out['terminal']
    with pytest.raises(StateError):s.execute(UUID,d,**kwargs)
    assert len(calls)==1


def test_ui_layout_row_moves_added_blocks_and_roundtrip():
    d,*_=candidate();card=r.card(d,categories=CATS)
    old=json.dumps({'source_file_id':card['identity']['source_file_id'],'page_numbers':[1]})
    before=[['unchanged']]+[['x','', 'pdf-page-review-v1','old-token',old,'date'] for _ in range(13)]+[['other page untouched']]
    batches,rows,start,extra=ui.requests(before,before,[card],CATS,column_count=16,row_count=1001)
    assert start==1 and extra==len(rows)-13
    assert sum(ui.WIDTHS.values())+46<=375
    assert not any(any(k in x for k in ('addSheet','deleteDimension','deleteSheet')) for x in batches)
    value=ui.read_snapshot(rows,card,'')
    assert r.evaluate(d,r.check_snapshot(value,card),CATS)['status']=='ready_to_confirm'
    shifted=ui.rules_for_rows(rows,200)
    assert any('$Q212' in rule['booleanRule']['condition']['values'][0]['userEnteredValue'] for rule in shifted)
    assert all(x[17]==r.SCHEMA and x[2:16]==['']*14 for x in rows)


def test_multiple_receipt_units_independent_requests():
    d,p,raw,m=candidate(count=2)
    s,t,owner=service(d,p,raw,m)
    value=snap(d,action='保留する');owner['current']=value
    assert s.capture(UUID,d,value)['status']=='held'
    # A held first Unit is not a parent-PDF terminal state and cannot authorize
    # a sibling's accounting; Unit identities remain disjoint in its manifest.
    assert len(m['units'])==2 and m['units'][0]['receipt_unit_id']!=m['units'][1]['receipt_unit_id']
    x=deepcopy(d['legacy']);unit=m['units'][1]
    x['identity']=legacy.unit_identity(p,m,unit['receipt_unit_id'])
    x.pop('candidate_digest');x['candidate_digest']=digest(x)
    other=r.prepare(x,CATS);value=snap(other,action='記帳する')
    owner['current']=value;owner[SECOND]=digest(value)
    assert s.capture(SECOND,other,value)['status']=='validated_not_written'
    state=s.store.load()
    assert state['requests'][UUID]['status']=='held' and len(state['requests'])==2


class Sheet:
    def __init__(self):self.rows=[];self.writes=0;self.disconnect=False
    def _get(self,*_):return deepcopy(self.rows)
    def _write(self,data):
        assert len(data)==1 and data[0]['range'].startswith("'"+QUEUE+"'!")
        self.rows.extend(deepcopy(data[0]['values']));self.writes+=1
        if self.disconnect:raise TimeoutError('synthetic after delivery')


def test_existing_hidden_queue_runner_capture_replay_hold_and_timeout():
    d,*_=candidate();card=r.card(d,categories=CATS);sheet=Sheet();value=snap(d,action='記帳する')
    assert queue.capture(sheet,card,value,request_id=UUID,clock=lambda:'synthetic')['status']=='would_capture'
    assert sheet.writes==0
    sheet.disconnect=True
    assert queue.capture(sheet,card,value,request_id=UUID,clock=lambda:'synthetic',write_enabled=True)['status']=='accepted'
    assert queue.capture(sheet,card,value,request_id=SECOND,clock=lambda:'synthetic',write_enabled=True)['appended']==0
    assert sheet.writes==1
    changed=snap(d,action='保留する')
    with pytest.raises(StateError):queue.capture(sheet,card,changed,request_id=UUID,clock=lambda:'synthetic',write_enabled=True)


def test_p14_like_ten_rows_never_becomes_ready_from_category_completion():
    d,*_=candidate(unstable=True);x=deepcopy(d['legacy'])
    x['parsed']['items']=[dict(name=f'商品{i}',amount=100,quantity=1,major_category='食費',minor_category='食品',note='',confidence=.9) for i in range(9)]
    x['parsed']['items'].append(dict(name='全品割引',amount=-13,quantity=1,major_category='食費',minor_category='食品',note='',confidence=.9))
    x['parsed']['total']=887;x['prefill']['amount']=887;x.pop('item_category_evidence')
    x.pop('candidate_digest');x['candidate_digest']=digest(x);d=r.prepare(x,CATS);f=r.fields(d)
    for i in f['items']:i['category']='食費｜食品'
    assert len(r.card(d,f,categories=CATS)['items'])==10
    result=r.evaluate(d,f,CATS)
    assert result['required_fields_complete'] and result['status']=='needs_review'


@pytest.mark.parametrize('problem',['self_report','wrong_owner','wrong_method','wrong_uuid','wrong_snapshot','wrong_page','expired'])
def test_actor_evidence_cannot_be_copied_from_sheet_or_other_request(problem):
    from dataclasses import replace
    d,p,raw,m=candidate();s,t,owner=service(d,p,raw,m);value=snap(d,action='記帳する')
    owner['current']=value;owner[UUID]=digest(value);verified=s.actor(UUID,digest(value),d['legacy']['identity'])
    if problem=='self_report':verified={'email':'owner@example.test'}
    elif problem=='wrong_owner':verified=replace(verified,subject='other-owner')
    elif problem=='wrong_method':verified=replace(verified,method='sheet_cell')
    elif problem=='wrong_uuid':verified=replace(verified,request_id=SECOND)
    elif problem in {'wrong_snapshot','wrong_page'}:verified=replace(verified,request_digest='f'*64)
    elif problem=='expired':verified=replace(verified,verified_at=verified.verified_at-601)
    s.actor=lambda *_:verified
    with pytest.raises(StateError,match='verified_actor'):s.capture(UUID,d,value)
    assert t.writes==0


def test_signed_oidc_hga_item_completion_shared_freshness_boundary(keys):
    from dataclasses import replace
    signed=Rig(keys);d,p,raw,m=candidate(kind='unknown');signed.p=p;signed.source=raw
    actor=signed.authenticate();signed.confirm()
    grant=next(iter(json.loads(signed.grants.payload)['grants'].values()))
    s,t,owner=service(d,p,raw,m)
    fresh=legacy.GeneralCompletion(None,lambda *_:p,lambda _:raw,lambda _:m,lambda _:grant,
                                  lambda:CATS,lambda _:None,lambda *_:False)
    s.fresh=fresh.fresh;s.owner_actor_id=actor.actor_id;s.clock=lambda:signed.now
    # Synthetic trusted server binds the ACTIVE verified session to this new
    # explicit receipt snapshot. HGA by itself is not posting permission.
    s.actor=lambda rid,sha,ident:replace(actor,request_id=rid,request_digest=digest([r.SCHEMA,'post_receipt',rid,sha,ident]))
    value=snap(d,action='記帳する');owner['current']=value
    result=s.capture(UUID,d,value)
    assert result['status']=='validated_not_written' and result['actor']['actor_id']==actor.actor_id
    assert p.automatic_classification=='sensitive_unknown' and signed.exchange_calls==1
    assert not result['plan']['posting_authority']
    fresh.grant=lambda _:None
    with pytest.raises(StateError):s.capture(UUID,d,value)


def test_compact_permanent_event_year_partition_terminal_visibility_and_replay():
    from app.receipt_audit import MemoryAuditRepository,IDENTITY_FIELDS,needs_attention,encoded
    d,p,raw,m=candidate();s,t,owner=service(d,p,raw,m);value=snap(d,action='記帳する')
    owner['current']=value;owner[UUID]=digest(value);request=s.capture(UUID,d,value)
    event=r.permanent_event(request,d,'2026-10-09T00:00:00+00:00')
    assert len(encoded(event))<2048 and 'snapshot' not in event and 'items' not in event
    repo=MemoryAuditRepository();repo.commit(event,0)
    before=repo.writes;assert repo.commit(event,0)['replayed'] and repo.writes==before
    identity={k:event[k] for k in IDENTITY_FIELDS}
    assert not needs_attention(repo.get_current(identity)) and ('2026',event['event_id']) in repo.events
    for item in request['plan']['parsed']['items']:assert 'category_source=gemini' in item['note']
    card=r.card(d,categories=CATS);rows=ui.encoded_rows(card)
    request['status']='complete'
    batch=ui.terminal_hide_requests(rows,card,request,lambda _:'complete')
    assert list(batch[0])==['updateDimensionProperties']
    with pytest.raises(StateError):ui.terminal_hide_requests(rows,card,request,lambda _:'unknown')


def test_duplicate_worker_after_claim_only_readback_and_no_writer_resend():
    d,p,raw,m=candidate();s,t,owner=service(d,p,raw,m);value=snap(d,action='記帳する')
    owner['current']=value;owner[UUID]=digest(value);s.capture(UUID,d,value)
    state=s.store.load();state['requests'][UUID]['status']='running';state['generation']+=1;s.store.save(state)
    out=s.execute(UUID,d,existing_writer=lambda _:pytest.fail('duplicate worker resend'),
        accounting_readback=lambda _:'complete',append_history=lambda _:None,execution_enabled=True)
    assert out['status']=='complete' and out['terminal']
