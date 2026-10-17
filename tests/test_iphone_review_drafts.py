"""Independent in-memory one-tab model; never accesses any Google workbook."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import asdict
from threading import Barrier, Lock

import pytest

from app.drive_run_state import StateError
from app.iphone_review_drafts import (FIELDS, PURPOSE, binding, capture, policy,
    regenerate, snapshot, validate_handoff, protection_request, LEGACY_UNMAPPED_HOLD)
from app.unified_receipt_review import digest


def row(cid='G101', kind='normal', hold=''):
    return dict(case_id=cid, kind=kind, purpose=PURPOSE[kind], hold=hold,
        identity=dict(source_file_id='synthetic-source-'+cid, source_content_hash=digest(['bytes', cid]),
            source_version='1', page_numbers=[1], page_identities=[digest(['page', cid])],
            receipt_unit_id='' if kind == 'grouping' or cid == 'H301' else 'synthetic-unit-'+cid,
            receipt_index=0 if kind == 'grouping' or cid == 'H301' else 1),
        original_url='https://drive.google.com/file/d/synthetic-source-'+cid+'/view',
        inputs={f: '' for f in FIELDS}, notes={})


def fixture():
    rows = [row(), row('G102'), row('M201', 'medical'), row('P201', 'grouping'),
            row('G103', hold='保留'), row('H301', 'medical', LEGACY_UNMAPPED_HOLD)]
    rows[4]['inputs'].update(amount=321, merchant='保留前の合成入力', memo='途中メモを保持')
    return regenerate(rows, generation=1)


def reads(issued):
    return asdict(issued)


def test_capture_draft_and_notes_preserved_after_reload_and_regeneration():
    issued = fixture(); live = reads(issued)
    live['rows'][0]['inputs'].update(amount=120, memo='入力途中')
    live['rows'][0]['notes']['memo'] = '原本と往復した途中メモ'
    journal = capture(issued, live, deepcopy(live))
    regenerated = regenerate(issued.rows, journal, generation=2)
    assert regenerated.rows[0] == live['rows'][0]
    assert regenerated.rows[4:] == issued.rows[4:]
    assert capture(regenerated, reads(regenerated), reads(regenerated)) == journal


@pytest.mark.parametrize('mutation', ['sort', 'insert', 'delete', 'duplicate', 'replace', 'partial_identity_sort'])
def test_unmanaged_layout_changes_stop_without_transferring_inputs(mutation):
    issued = fixture(); live = reads(issued); before = deepcopy(issued)
    if mutation == 'sort':
        live['rows'].reverse()  # values/notes move; positional protections remain
    elif mutation == 'insert':
        live['rows'].insert(0, row('NEW'))
    elif mutation == 'delete':
        del live['rows'][0]
    elif mutation == 'duplicate':
        live['rows'][1] = deepcopy(live['rows'][0])
    elif mutation == 'replace':
        live['rows'][0] = row('NEW')
    else:
        live['rows'][0]['identity'], live['rows'][1]['identity'] = live['rows'][1]['identity'], live['rows'][0]['identity']
    with pytest.raises(StateError):
        capture(issued, live, deepcopy(live))
    assert issued == before


def test_control_repair_plan_is_derived_from_ids_after_offline_reorder():
    issued = fixture(); live = reads(issued)
    live['rows'][0]['inputs']['memo'] = 'G101 only'
    journal = capture(issued, live, deepcopy(live))
    regenerated = regenerate(list(reversed(issued.rows)), journal, generation=2)
    assert regenerated.rows[-1]['inputs']['memo'] == 'G101 only'
    assert regenerated.controls == policy(regenerated.rows)
    assert regenerated.controls[0]['case_id'] == 'H301'
    assert regenerated.controls[0]['editable'] == regenerated.controls[1]['editable'] == []
    assert regenerated.controls[2]['editable'] == ['partition', 'memo']
    request = protection_request(regenerated, 7)['addProtectedRange']['protectedRange']
    assert request['range'] == {'sheetId': 7} and request['warningOnly'] is False
    assert all(r['startRowIndex'] >= 3 for r in request['unprotectedRanges'])


@pytest.mark.parametrize('field', ['identity', 'kind', 'purpose', 'hold', 'original_url'])
def test_editable_looking_cell_does_not_override_identity_or_kind(field):
    issued = fixture(); live = reads(issued)
    if field == 'identity':
        live['rows'][0][field]['source_version'] = '2'
    else:
        live['rows'][0][field] = 'tampered'
    with pytest.raises(StateError):
        capture(issued, live, deepcopy(live))


@pytest.mark.parametrize('control', ['editable', 'validations', 'binding'])
def test_protection_or_validation_drift_stops_capture(control):
    issued = fixture(); live = reads(issued)
    live['controls'][0][control] = [] if control != 'binding' else 'wrong'
    with pytest.raises(StateError, match='protection_or_validation'):
        capture(issued, live, deepcopy(live))


@pytest.mark.parametrize('idx', [4, 5])
def test_held_rows_cannot_be_edited_even_by_owner_in_model(idx):
    issued = fixture(); live = reads(issued)
    live['rows'][idx]['inputs']['memo'] = 'owner bypassed protection'
    with pytest.raises(StateError, match='held_input'):
        capture(issued, live, deepcopy(live))
    assert issued.controls[idx]['editable'] == []


@pytest.mark.parametrize('idx,field', [(0, 'partition'), (2, 'partition'), (3, 'amount')])
def test_kind_inapplicable_fields_rejected_independently_of_controls(idx, field):
    issued = fixture(); live = reads(issued); live['rows'][idx]['inputs'][field] = 'foreign'
    with pytest.raises(StateError):
        capture(issued, live, deepcopy(live))


def test_in_progress_edit_between_reads_fails_and_keeps_original_journal():
    issued = fixture(); a = reads(issued); b = deepcopy(a)
    b['rows'][0]['inputs']['memo'] = 'still typing'
    with pytest.raises(StateError, match='concurrent_or_unknown'):
        capture(issued, a, b)
    assert issued.rows[0]['inputs']['memo'] == ''


def test_generation_prevents_reusing_capture_after_rebuild():
    old = fixture(); newer = regenerate(old.rows, generation=2)
    with pytest.raises(StateError, match='stale_generation'):
        capture(newer, reads(old), reads(old))


@pytest.mark.parametrize('part', ['source_file_id', 'source_content_hash', 'source_version', 'receipt_unit_id', 'receipt_index', 'page_identities'])
def test_changed_original_does_not_inherit_prior_draft(part):
    issued = fixture(); live = reads(issued); live['rows'][0]['inputs']['memo'] = 'old source only'
    journal = capture(issued, live, deepcopy(live)); new = deepcopy(issued.rows)
    i = new[0]['identity']
    i[part] = {'source_file_id': 'new-source', 'source_content_hash': 'a'*64, 'source_version': '2',
               'receipt_unit_id': 'new-unit', 'receipt_index': 2, 'page_identities': ['b'*64]}[part]
    new[0]['original_url'] = 'https://drive.google.com/file/d/'+i['source_file_id']+'/view'
    rebuilt = regenerate(new, journal, generation=2)
    assert rebuilt.rows[0]['inputs']['memo'] == ''
    assert journal[binding(issued.rows[0])]['inputs']['memo'] == 'old source only'


def test_deleted_case_journal_is_retained_and_row_slot_never_reused():
    issued = fixture(); live = reads(issued); live['rows'][0]['inputs']['memo'] = 'deleted case draft'
    journal = capture(issued, live, live)
    new = [row('NEW')] + issued.rows[1:]
    assert regenerate(new, journal, generation=2).rows[0]['inputs']['memo'] == ''
    assert regenerate(issued.rows, journal, generation=3).rows[0]['inputs']['memo'] == 'deleted case draft'


def test_memory_cas_model_allows_only_one_concurrent_draft_commit():
    # This is a contract simulation, NOT a claim that Sheets supports CAS.
    issued = fixture(); revision = 0; journal = {}; lock = Lock(); barrier = Barrier(2)
    def save(amount):
        nonlocal revision, journal
        live = reads(issued); live['rows'][0]['inputs']['amount'] = amount
        changes = capture(issued, live, live); expected = revision
        barrier.wait(timeout=5)
        with lock:
            if revision != expected:
                return 'conflict'
            journal = changes; revision += 1
            return 'saved'
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(save, [100, 200])) == ['conflict', 'saved']
    assert len(journal) == 6 and revision == 1


@pytest.mark.parametrize('value', [True, None, [], {}, '=NOW()'])
def test_raw_formula_or_nonliteral_input_rejected(value):
    issued = fixture(); live = reads(issued); live['rows'][0]['inputs']['amount'] = value
    with pytest.raises(StateError):
        capture(issued, live, live)


def test_downstream_validator_independently_rechecks_identity_snapshot_and_hold():
    canonical = row(); draft = deepcopy(canonical); draft['inputs']['amount'] = 123
    result = validate_handoff(draft, canonical, snapshot(draft))
    assert result['authorized'] is False and result['operation_targets'] == []
    changed = deepcopy(canonical); changed['identity']['source_version'] = '2'
    with pytest.raises(StateError, match='identity_changed'):
        validate_handoff(draft, changed, snapshot(draft))
    with pytest.raises(StateError, match='snapshot_changed'):
        validate_handoff(draft, canonical, snapshot(canonical))
    canonical['hold'] = '保留'
    with pytest.raises(StateError, match='held_case'):
        validate_handoff(draft, canonical, snapshot(draft))


def test_valid_value_only_swap_is_unprovable_and_never_authorizes_posting():
    # No cell-level validator can distinguish this from two intentional edits.
    # Explicit authenticated source/snapshot re-review is a mandatory next gate.
    issued = fixture(); live = reads(issued)
    live['rows'][0]['inputs']['amount'] = 200
    live['rows'][1]['inputs']['amount'] = 100
    journal = capture(issued, live, live)
    for r in journal.values():
        if not r['hold']:
            assert validate_handoff(r, {x['case_id']: x for x in issued.rows}[r['case_id']], snapshot(r))['authorized'] is False
