from copy import deepcopy

import pytest

from app.drive_run_state import StateError
from app.iphone_item_drafts import issue, inspect
from app.unified_receipt_review import digest
from test_iphone_review_preview import general_fixture


def fixture():
    _, _, db, runner, record, saved, _ = general_fixture()
    return issue(record, saved, db.categories()), record, saved, db, runner


def test_partial_item_input_keeps_old_values_notes_and_requires_redisplay():
    value, record, saved, db, runner = fixture(); before = deepcopy((db.data, runner.store.load()))
    value['entries'][0].update(state='entered', note='未送信の途中メモ')
    result = inspect(value, record, saved, db.categories())
    assert result['owner_reconfirmation_required'] and not result['authorized'] and result['operation_targets'] == []
    assert value['entries'][0]['old'] == saved['items'][0]
    assert result['input_digest'] == digest(value)
    assert before == (db.data, runner.store.load())


@pytest.mark.parametrize('state', ['unentered', 'held', 'not_applicable'])
def test_missing_hold_and_not_applicable_remain_distinct_non_ready(state):
    value, record, saved, db, _ = fixture()
    for entry in value['entries']: entry['state'] = state
    result = inspect(value, record, saved, db.categories())
    assert result['status'] == 'partial_or_held'
    assert {e['state'] for e in value['entries']} == {state}
    assert result['proposed_snapshot']['items'] == saved['items']


def test_every_entered_item_passes_existing_validator_without_authority():
    value, record, saved, db, _ = fixture()
    for entry in value['entries']: entry['state'] = 'entered'
    result = inspect(value, record, saved, db.categories())
    assert result['native']['status'] == 'ready_to_confirm'
    assert result['status'] == 'ready_for_owner_redisplay' and not result['authorized']


@pytest.mark.parametrize('attack', ['add', 'delete', 'reorder', 'duplicate', 'foreign', 'token', 'marker',
    'old_amount', 'snapshot', 'original_link', 'bad_state', 'proposed_identity', 'changed_amount'])
def test_identity_membership_order_and_native_rules_refuse_corruption(attack):
    value, record, saved, db, _ = fixture(); entry = value['entries'][0]
    if attack == 'add': value['entries'].append(deepcopy(entry))
    elif attack == 'delete': value['entries'].pop()
    elif attack == 'reorder': value['entries'].reverse()
    elif attack == 'duplicate': value['entries'].append(deepcopy(entry)); value['entries'].pop(0)
    elif attack == 'foreign': entry['identity']['source_file_id'] = 'foreign'
    elif attack == 'token': entry['token'] = 'changed'
    elif attack == 'marker': entry['marker'] = 'item:other'
    elif attack == 'old_amount': entry['old']['amount'] += 1
    elif attack == 'snapshot': value['snapshot_digest'] = 'f'*64
    elif attack == 'original_link': entry['original_link'] = 'https://example.test'
    elif attack == 'bad_state': entry['state'] = 'auto_confirmed'
    elif attack == 'proposed_identity': entry['proposed']['item_id'] = 'other'
    else: entry['proposed']['amount'] += 1
    with pytest.raises(StateError): inspect(value, record, saved, db.categories())


def test_category_missing_or_invalid_never_ready_or_receipt_broadcast():
    value, record, saved, db, _ = fixture()
    for entry in value['entries']:
        entry['state'] = 'entered'; entry['proposed']['category'] = 'invalid｜category'
    result = inspect(value, record, saved, db.categories())
    assert result['native']['missing_required'] and result['status'] == 'partial_or_held'
    assert saved['items'] != result['proposed_snapshot']['items']
