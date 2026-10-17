from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
import json

import pytest

from app.drive_run_state import StateError
from app.receipt_confirmation import HEADERS
from app.unified_receipt_review import Case, STATES, inventory, pdf_cases, project, input_differences, annotate_posting_receipts
from app.unified_receipt_preview import render


def identity(index=1, purpose='general_receipt_item_review'):
    return dict(source_file_id='synthetic-original', source_content_hash='a'*64,
                page_identity='b'*64, page_number=1, page_numbers=[1],
                receipt_unit_id='unit-'+str(index), receipt_index=index, kind=purpose)


def card(ident=None, token='t1', fields=None):
    ident = ident or identity()
    fields = fields or [('target','対象','p1'),('state','状態','反映済み'),('date','支払日',46290),
                        ('structure_confirmation','明細確認','確認済み'),('action','操作','記帳する')]
    return [[label,value]+['']*15+['general-receipt-item-review-v1',token,json.dumps(ident),marker,'','']
            for marker,label,value in fields]


def receipt(kind='medical', status='waiting'):
    key='review-synthetic'
    item=dict(kind=kind, status=status, source=dict(source_id='synthetic-image',sha256='c'*64,version='1'),inputs=['']*8)
    row=[key,'医療','未確認','https://drive.google.com/file/d/synthetic-image/view','','OCRの旧候補','医療OCRの旧候補']+['']*9
    return [HEADERS,row],{key:item}


@pytest.mark.parametrize('purpose', ['medical','receipt_reconciliation','grouping','page_kind','intake'])
def test_purposes_do_not_become_source_completion(purpose):
    c=Case('old','v1',identity(purpose=purpose),[],'PDFページ確認',purpose,authority_status='closed_user',verified_terminal=True)
    assert c.display()[0]=='処理待ち'


def test_displayed_applied_is_not_terminal():
    c=pdf_cases(card(),{})[0]
    assert c.display()[0]=='本人確認が必要'
    c.authority_status='imported'
    assert c.display()[0]=='処理待ち'
    c.verified_terminal=True
    assert c.display()[0]=='完了'


def test_medical_complete_needs_validator_terminal_evidence():
    c=Case('m','v1',identity(purpose='medical'),[],'領収書確認','medical',authority_status='medical_manual_imported')
    assert c.display()[0]=='処理待ち'
    c.verified_terminal=True
    assert c.display()[0]=='完了'


def test_same_page_multiple_receipts_remain_separate():
    cases=pdf_cases(card(identity(1),'t1')+card(identity(2),'t2'),{})
    assert len(cases)==2 and cases[0].unit_key!=cases[1].unit_key
    assert cases[0].case_id!=cases[1].case_id


def test_production_stable_page_marker_is_supported_without_rewriting_identity():
    ident = identity();ident['stable_page_identity'] = ident.pop('page_identity')
    c = pdf_cases(card(ident), {})[0]
    assert c.unit_key and not c.issues
    assert 'page_identity' not in c.identity
    conflicting = {**ident, 'page_identity':'d'*64}
    assert pdf_cases(card(conflicting), {})[0].unit_key is None


def test_render_options_do_not_create_false_unsent_inputs_or_change_snapshots():
    live=[46290,'施設',1500,'医療費','','医療費を確定','','']
    saved=['2026-09-25','施設','1500','医療費','','医療費を確定','','']
    before=deepcopy((live,saved))
    assert input_differences(live,saved)==[]
    assert (live,saved)==before
    changed=[*live];changed[1]='変更した施設'
    assert input_differences(changed,saved)==[1]
    assert input_differences(['invalid']+live[1:],saved)==[0]


def test_same_file_different_version_or_purpose_never_merges():
    i=identity();j={**i,'source_content_hash':'d'*64};k={**i,'kind':'receipt_reconciliation'}
    cases=pdf_cases(card(i,'t1')+card(j,'t2')+card(k,'t3'),{})
    assert len({c.case_id for c in cases})==3


def test_unknown_old_review_id_and_missing_unit_fail_closed():
    i=identity(purpose='medical');i.pop('receipt_unit_id');i['review_id']='unknown'
    c=pdf_cases(card(i),{})[0]
    assert len(c.issues)==2 and c.display()[0]=='保留・再確認'


def test_reconciliation_blocks_medical_on_immutable_page_without_joining():
    rows,items=receipt()
    cases=project(rows,card(identity(purpose='medical'),'m')+card(identity(purpose='receipt_reconciliation'),'r'),items)
    medical=next(c for c in cases if c.legacy_id=='m')
    assert medical.display()[0]=='保留・再確認'
    assert any('照合を先に' in x for x in medical.issues)
    assert len(cases)==3


def test_medical_legacy_ai_candidate_is_omitted():
    rows,items=receipt()
    c=project(rows,[],items)[0]
    assert c.value('candidate')==''
    assert '医療OCRの旧候補' not in render([c])


def test_unsent_input_and_history_preserved_without_mutation():
    rows,items=receipt(status='superseded');rows[1][8]='本人の途中入力';rows[1][12]='医療費を確定'
    before=deepcopy((rows,items))
    cases=project(rows,[],items)
    assert (rows,items)==before
    assert cases[0].value('merchant')=='本人の途中入力'
    assert cases[0].history and any('未送信' in x for x in cases[0].issues)


def test_replay_concurrent_projection_has_no_operation_targets():
    rows,items=receipt()
    with ThreadPoolExecutor(max_workers=8) as pool:
        views=list(pool.map(lambda _:project(rows,card(),items),range(20)))
    assert all(inventory(v)==inventory(views[0]) for v in views)
    assert all(inventory(v)['operation_targets']==0 for v in views)
    assert all(inventory(v)['accounting_writes']==inventory(v)['source_moves']==inventory(v)['ai_sends']==0 for v in views)


def test_semantic_markers_survive_inserted_rows():
    assert pdf_cases([[]]*400+card(),{})[0].case_id==pdf_cases(card(),{})[0].case_id


@pytest.mark.parametrize('change',['identity','marker','token'])
def test_corrupt_or_duplicated_card_is_rejected(change):
    rows=card()
    if change=='identity':rows[1][19]=json.dumps(identity(2))
    elif change=='marker':rows[1][20]=rows[0][20]
    else:rows[0][18]=''
    with pytest.raises(StateError):pdf_cases(rows,{})


def test_preview_escapes_cells_and_has_no_form_or_script():
    c=pdf_cases(card(fields=[('memo','メモ','<script>alert(1)</script>')]),{})[0]
    html=render([c])
    assert '<script>' not in html and '<form' not in html and '<input' not in html
    assert '&lt;script&gt;' in html
    assert 'data-field="memo"' in html and '案件終了' in html
    assert 'max-width:600px' in html


def test_long_case_repeats_case_identity():
    c=pdf_cases(card(fields=[('f'+str(n),'入力欄',str(n)) for n in range(20)]),{})[0]
    assert render([c]).count('band repeat')==2


@pytest.mark.parametrize('changed', ['', 'amount', 'category', 'receipt', 'identity'])
def test_native_posting_receipt_binding_is_readonly_and_fail_closed(changed):
    from test_receipt_unit_posting import setup, apply
    from app.receipt_item_review_ui import encoded_rows
    from app.receipt_plan.items import card as native_card
    from test_receipt_unit_plan import CATS
    args=setup();apply(args);rd,store,writer,_=args
    view={**native_card(rd.record,categories=CATS),**rd.snapshot}
    rows=encoded_rows(view);receipt=deepcopy(store.value)
    # This native fixture grants its string-date snapshot; retain those exact
    # bytes rather than introducing Sheets' alternate numeric date rendering.
    for row,source_row in zip(rows,view['rows']):row[1]=source_row[2]
    cases=pdf_cases(rows,{})
    if changed=='amount':next(r for r in rows if r[20]=='amount')[1]+=1
    elif changed=='category':next(r for r in rows if r[20].startswith('item:'))[16]='他のカテゴリ'
    elif changed=='receipt':receipt['grant']['snapshot_digest']='0'*64
    elif changed=='identity':view['identity']={**view['identity'],'source_content_hash':'0'*64}
    before=deepcopy((rows,receipt,rd.tables,writer.calls))
    annotate_posting_receipts(cases,rows,[(dict(view=view),receipt)])
    assert (rows,receipt,rd.tables,writer.calls)==before
    assert not cases[0].verified_terminal
    assert cases[0].display()[0]==('保留・再確認' if changed else '処理待ち')
    if not changed:
        assert cases[0].accepted and cases[0].last_processed_at
        assert '保存済み証跡' in render(cases)
        with pytest.raises(StateError):annotate_posting_receipts(cases,rows,[(dict(view=view),receipt)]*2)


@pytest.mark.parametrize('scenario',['image','single-pdf','multi-page-one-receipt','mixed-pdf','unknown','source-changed','mid-input'])
def test_readonly_scenario_identifiers(scenario):
    rows,items=receipt(kind='intake' if scenario=='unknown' else 'medical' if scenario=='mixed-pdf' else 'normal')
    if scenario=='source-changed':items['review-synthetic']['status']='superseded'
    if scenario=='mid-input':rows[1][14]='入力途中'
    pdf=[] if scenario=='image' else card({**identity(), 'page_numbers':[1,2] if scenario=='multi-page-one-receipt' else [1]})
    cases=project(rows,pdf,items)
    assert len(cases)==(1 if scenario=='image' else 2)
    assert all(c.display()[0] in STATES for c in cases)
    assert inventory(cases)['operation_targets']==0
