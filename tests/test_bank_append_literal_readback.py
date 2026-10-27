from copy import deepcopy
from types import SimpleNamespace

import pytest

from app.bank_income_recurring import _exact_present
from app.bank_fixed_backfill import FixedWriteDB


def row():
    return ['fixed-id', '2026-10-28 06:00:00', 'synthetic', 'fixed-id', '2025-01-01',
            'synthetic', -1234, '銀行口座', 'bank_non_expense', '', 'hash', '']


def test_formatted_money_fails_but_raw_all_twelve_columns_pass_without_write():
    expected = row()
    raw = expected[:-1]  # Sheets may omit trailing empty cells.
    formatted = deepcopy(expected)
    formatted[6] = '-1,234'
    db = SimpleNamespace(get=lambda _: [formatted], get_raw=lambda _: [raw])
    _exact_present(db, '取込データ', [expected])
    with pytest.raises(RuntimeError):
        _exact_present(SimpleNamespace(get=lambda _: [formatted]), '取込データ', [expected])


@pytest.mark.parametrize('column', range(12))
def test_raw_content_difference_in_any_column_is_rejected(column):
    expected = row()
    changed = deepcopy(expected)
    changed[column] = -9999 if column == 6 else str(changed[column]) + 'changed'
    db = SimpleNamespace(get=lambda _: [expected], get_raw=lambda _: [changed])
    with pytest.raises(RuntimeError):
        _exact_present(db, '取込データ', [expected])


def test_duplicate_id_and_nonempty_trailing_memo_are_rejected():
    expected = row()
    db = SimpleNamespace(get=lambda _: [], get_raw=lambda _: [expected, expected])
    with pytest.raises(RuntimeError):
        _exact_present(db, '取込データ', [expected])
    db.get_raw = lambda _: [expected[:-1] + ['unexpected']]
    with pytest.raises(RuntimeError):
        _exact_present(db, '取込データ', [expected])


def test_fixed_write_adapter_forwards_raw_reads():
    expected = row()
    db = SimpleNamespace(sid='synthetic', get=lambda _: [['formatted']], get_raw=lambda _: [expected])
    guarded = FixedWriteDB(db, [expected[0]], {expected[0]: expected}, {}, lambda: None)
    assert guarded.get_raw('取込データ!A2:L') == [expected]


def test_boolean_money_is_not_an_integer_representation():
    expected = row()
    expected[6] = 1
    saved = deepcopy(expected)
    saved[6] = True
    db = SimpleNamespace(get=lambda _: [saved], get_raw=lambda _: [saved])
    with pytest.raises(RuntimeError):
        _exact_present(db, '取込データ', [expected])
