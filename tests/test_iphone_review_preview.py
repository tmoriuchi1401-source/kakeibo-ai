"""Real existing validators with disposable synthetic state; no live IO."""
from copy import deepcopy
from dataclasses import asdict, replace

import pytest

from app.drive_run_state import StateError
from app.iphone_review_drafts import capture, regenerate, snapshot
from app.iphone_review_preview import (preview_medical, preview_general,
    preview_structure, structure_selection)
from app.unified_receipt_eligibility import current_snapshot
from app.unified_receipt_review import digest
from test_iphone_review_drafts import row
from test_unified_receipt_write import medical_native, pdf_native


def forbidden(*args, **kwargs):
    raise AssertionError('preview_must_not_write_or_execute')


def from_case(case, kind):
    r = row(case.case_id, kind)
    i = case.identity
    r['identity'] = dict(source_file_id=i['source_file_id'], source_content_hash=i['source_content_hash'],
        source_version=i['source_version'], page_numbers=case.pages, page_identities=i['page_identities'],
        receipt_unit_id=i['receipt_unit_id'], receipt_index=i['receipt_index'])
    r['original_url'] = 'https://drive.google.com/file/d/'+i['source_file_id']+'/view'
    for marker, _, value in case.fields:
        if marker in r['inputs']:
            r['inputs'][marker] = deepcopy(value)
    return r


def capture_one(canonical, draft):
    issued = regenerate([canonical], generation=1)
    live = asdict(issued); live['rows'] = [draft]
    journal = capture(issued, live, deepcopy(live))
    return next(iter(journal.values()))


def test_medical_sheet_draft_to_existing_manual_validator_is_readonly(monkeypatch):
    c, bridge, port, request, service, db, store, fake, io = medical_native()
    canonical = from_case(c, 'medical'); draft = deepcopy(canonical)
    draft['inputs'].update(amount=101, memo='原本を見て手入力途中')
    draft['notes']['memo'] = '消さない注記'
    draft = capture_one(canonical, draft)
    before = deepcopy((db.rows, store.value, io.raw))
    monkeypatch.setattr(store, 'save', forbidden)
    monkeypatch.setattr(db, 'append_raw', forbidden)
    monkeypatch.setattr(db, 'update_row_raw', forbidden)
    monkeypatch.setattr(port, 'execute', forbidden)
    monkeypatch.setattr(bridge, 'submit', forbidden)
    checked = preview_medical(draft, canonical, snapshot(draft), c, digest(current_snapshot(c)), port)
    assert checked['validation']['status'] == 'validated_not_authorized'
    assert checked['handoff']['inputs']['amount'] == 101
    assert checked['handoff']['notes'] == draft['notes']
    assert checked['authorized'] is False and checked['operation_targets'] == []
    assert before == (db.rows, store.value, io.raw)
    assert fake.ai_calls == fake.moves == io.writes == 0


@pytest.mark.parametrize('amount', ['', 0, -1, 'AI候補'])
def test_medical_invalid_input_fails_native_validator_without_state_change(amount):
    c, b, p, _, service, db, store, fake, io = medical_native()
    r = from_case(c, 'medical'); r['inputs']['amount'] = amount
    before = deepcopy((db.rows, store.value, io.raw))
    with pytest.raises((StateError, ValueError)):
        preview_medical(r, r, snapshot(r), c, digest(current_snapshot(c)), p)
    assert before == (db.rows, store.value, io.raw)
    assert fake.ai_calls == fake.moves == io.writes == 0


@pytest.mark.parametrize('failure', ['case_id', 'source_version', 'kind', 'input_snapshot', 'native_snapshot'])
def test_independent_native_boundary_rejects_foreign_or_stale_drafts(failure):
    c, b, p, _, service, db, store, fake, io = medical_native()
    canonical = from_case(c, 'medical'); r = deepcopy(canonical)
    native_digest = digest(current_snapshot(c)); expected = snapshot(r)
    if failure == 'case_id':
        r['case_id'] = 'another-case'
    elif failure == 'source_version':
        r['identity']['source_version'] = '2'
    elif failure == 'kind':
        r.update(kind='normal')
    elif failure == 'input_snapshot':
        r['inputs']['amount'] = 101
    else:
        native_digest = digest(['stale'])
    with pytest.raises(StateError):
        preview_medical(r, canonical, expected, c, native_digest, p)
    assert io.writes == fake.ai_calls == fake.moves == 0


def general_fixture():
    c, b, p, request, db, runner = pdf_native()
    record = runner.store.load()['pages'][p.key]['units'][p.uid]['candidate']['record']
    s = p.snapshot_reader()
    from app.receipt_item_review import check_snapshot, card
    current = check_snapshot(s, card(record, categories=db.categories()))
    canonical = from_case(c, 'normal')
    for f in ('date', 'merchant', 'amount', 'payment', 'memo'):
        canonical['inputs'][f] = current[f]
    canonical['inputs']['category'] = ''  # Existing item categories stay intact.
    return c, p, db, runner, record, s, canonical


def test_general_sheet_draft_uses_existing_item_snapshot_validator(monkeypatch):
    c, p, db, runner, record, s, canonical = general_fixture()
    r = deepcopy(canonical); r['inputs']['merchant'] = '修正した合成店舗'
    r = capture_one(canonical, r)
    before = deepcopy((db.data, runner.store.load()))
    monkeypatch.setattr(runner.store, 'save', forbidden)
    monkeypatch.setattr(p, 'execute', forbidden)
    monkeypatch.setattr(db, 'append_raw', forbidden)
    checked = preview_general(r, canonical, snapshot(r), c, digest(current_snapshot(c)),
                              record, s, digest(s), db.categories())
    assert checked['validation']['proposed_native_snapshot']['items'] == s['items']
    assert checked['validation']['native_status'] != 'imported'
    assert checked['authorized'] is False and checked['operation_targets'] == []
    assert before == (db.data, runner.store.load())


@pytest.mark.parametrize('failure', ['category_broadcast', 'foreign_unit', 'stale_snapshot'])
def test_general_never_invents_item_categories_or_rebinds_original(failure):
    c, p, db, runner, record, s, canonical = general_fixture()
    r = deepcopy(canonical); expected = digest(s)
    if failure == 'category_broadcast':
        r['inputs']['category'] = '食費｜食品'
    elif failure == 'foreign_unit':
        record = deepcopy(record); record['legacy']['identity']['receipt_unit_id'] = 'foreign'
    else:
        expected = digest(['old'])
    before = deepcopy((db.data, runner.store.load()))
    with pytest.raises(StateError):
        preview_general(r, canonical, snapshot(r), c, digest(current_snapshot(c)), record, s, expected, db.categories())
    assert before == (db.data, runner.store.load())


def structure_fixture():
    from unified_purpose_fixtures import existing_review_fixture
    f = existing_review_fixture('confirm_grouping')
    o = f.observe(f.case_id, f.purpose, f.action, f.rid)
    r = row(o.case_id, 'grouping'); e = o.evidence
    r['identity'].update({k: e['source'][k] for k in ('source_file_id', 'source_content_hash', 'source_version')})
    r['identity'].update(page_numbers=[p['page_number'] for p in e['pages']],
                         page_identities=[p['stable_page_identity'] for p in e['pages']])
    r['original_url'] = 'https://drive.google.com/file/d/'+r['identity']['source_file_id']+'/view'
    r['inputs']['partition'] = structure_selection(o)
    return f, o, r


def test_pdf_structure_draft_cannot_create_financial_authority(monkeypatch):
    f, o, r = structure_fixture()
    before = (f.state_io.raw, f.io.raw)
    monkeypatch.setattr(f.grouping, 'review', forbidden)
    monkeypatch.setattr(f.port, 'execute', forbidden)
    checked = preview_structure(r, r, snapshot(r), o, f.rid)
    assert checked['validation']['accounting_allowed'] is False
    assert checked['authorized'] is False and checked['operation_targets'] == []
    assert before == (f.state_io.raw, f.io.raw)
    assert f.accounting_appends == f.ai_sends == f.source_moves == f.native_calls == 0


@pytest.mark.parametrize('failure', ['partition', 'version', 'purpose', 'snapshot'])
def test_pdf_draft_requires_exact_native_structure_contract(failure):
    f, o, r = structure_fixture(); expected = snapshot(r)
    if failure == 'partition':
        r['inputs']['partition'] = 'invented operation'
        expected = snapshot(r)
    elif failure == 'version':
        r['identity']['source_version'] = '2'
    elif failure == 'purpose':
        o = replace(o, purpose='ACCOUNTING_POST')
    else:
        expected = digest(['old'])
    with pytest.raises(StateError):
        preview_structure(r, r, expected, o, f.rid)
    assert f.io.writes == f.accounting_appends == f.ai_sends == f.source_moves == f.native_calls == 0
