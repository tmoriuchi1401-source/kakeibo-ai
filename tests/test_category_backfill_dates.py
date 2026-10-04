"""Calendar-only ledger date compatibility; unchanged backfill safety boundary."""
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone

import pytest

from app.category_backfill import BackfillSpec, CategoryBackfillPipeline, _period_ymd, _ymd
from test_category_backfill import BackfillDB, condition


EPOCH = date(1899, 12, 30)


def representations(day):
    serial = (day - EPOCH).days
    return [day.isoformat(), day.strftime('%Y/%m/%d'), day,
            datetime.combine(day, datetime.min.time()),
            datetime.combine(day, datetime.min.time(), timezone(timedelta(hours=14))),
            datetime.combine(day, datetime.max.time(), timezone(timedelta(hours=-12))),
            day.isoformat() + 'T23:59:59-12:00', serial, float(serial),
            serial + 0.999999, serial + 0.000001]


@pytest.mark.parametrize('day', [date(2026, 9, 1), date(2026, 9, 14), date(2026, 9, 30),
                                date(2026, 12, 31), date(2027, 1, 1), date(2024, 2, 29)])
def test_same_calendar_day_and_month_for_all_supported_representations(day):
    pipe = CategoryBackfillPipeline(BackfillDB(), preview_enabled=True, apply_enabled=True)
    start = day.replace(day=1)
    following = (start + timedelta(days=32)).replace(day=1)
    spec = BackfillSpec(condition(), start.isoformat(), (following - timedelta(days=1)).isoformat())
    next_month = BackfillSpec(condition(), following.isoformat(), (following + timedelta(days=1)).isoformat())
    for value in representations(day):
        assert _ymd(value) == day, value
        assert pipe._in_period(['id', value], spec), value
        assert not pipe._in_period(['id', value], next_month), value


@pytest.mark.parametrize('value', [None, '', ' ', True, False, float('nan'), float('inf'),
                                 float('-inf'), -1, 2958466, 10**400, '46279',
                                 '2026-02-29', '2026-13-01', '2026-00-01', 'not a date', [], {}])
def test_invalid_and_empty_ledger_dates_fail_closed(value):
    assert _ymd(value) is None
    pipe = CategoryBackfillPipeline(BackfillDB(), preview_enabled=True, apply_enabled=True)
    assert not pipe._in_period(['id', value], BackfillSpec(condition()))


def test_serial_epoch_and_maximum_are_calendar_dates_without_timezone_conversion():
    assert _ymd(0) == EPOCH
    assert _ymd(2.5) == date(1900, 1, 1)
    assert _ymd(46279) == date(2026, 9, 14)
    assert _ymd(2958465.9) == date.max


@pytest.mark.parametrize('value', [46279, 46279.5, '46279', '2026-09-14T00:00:00Z', True, None, ''])
def test_period_controls_stay_strict_and_do_not_accept_serials(value):
    assert _period_ymd(value) is None


@pytest.mark.parametrize('serial', [False, True])
def test_serial_and_string_preview_apply_identical_ids_without_broadening(serial):
    db = BackfillDB()
    day = date(2026, 8, 10)
    value = (day - EPOCH).days if serial else day.isoformat()
    db.expenses['M-one'][1][1] = value
    db.expenses['M-outside'] = (4, ['M-outside', '2026-07-31', '請求名', '自動計上', 100,
        'その他', '未分類', '', 'PayPay', '', 'p1', '', 'active'])
    before = deepcopy(db.expenses)
    pipe = CategoryBackfillPipeline(db, preview_enabled=True, apply_enabled=True, id_factory=lambda: 'CB-date')
    preview = pipe.preview(BackfillSpec(condition(), '2026-08-01', '2026-08-31'))
    assert preview['targets'] == 1 and preview['total_amount'] == 100
    assert [r[1] for r in db.targets] == ['M-one']
    assert db.targets[0][3] == value  # Raw identity snapshot/digest representation stays intact.
    assert db.expenses == before and not db.category_updates and not db.rules
    assert pipe.confirm('CB-date', expected_count=1)['state'] == 'confirmed'
    assert pipe.apply('CB-date', expected_count=1)['applied'] == 1
    assert db.category_updates == [(2, '食費', '外食')]
    assert db.expenses['M-outside'] == before['M-outside']
    assert db.expenses['M-partial'] == before['M-partial']
    snapshot = deepcopy((db.expenses, db.requests, db.targets, db.category_updates, db.rules))
    assert pipe.apply('CB-date', expected_count=1)['state'] == 'held'
    assert (db.expenses, db.requests, db.targets, db.category_updates, db.rules) == snapshot


@pytest.mark.parametrize('note', ['レシート等との照合待ち', 'NEEDS_REVIEW', '要確認', '重複'])
def test_serial_date_never_bypasses_reconciliation_exclusion(note):
    db = BackfillDB()
    db.expenses['M-one'][1][1] = (date(2026, 8, 10) - EPOCH).days
    db.imports[0][11] = note
    before = deepcopy(db.expenses)
    pipe = CategoryBackfillPipeline(db, preview_enabled=True, apply_enabled=True)
    preview = pipe.preview(BackfillSpec(condition(), '2026-08-01', '2026-08-31'))
    assert preview['state'] == 'preview_empty' and preview['targets'] == 0
    assert preview['excluded']['duplicate_or_reconciliation_uncertain'] == 1
    assert not db.requests and not db.targets and not db.rules and not db.category_updates
    assert db.expenses == before


def test_backend_rejects_even_a_forged_zero_target_confirm_and_apply():
    db = BackfillDB()
    pipe = CategoryBackfillPipeline(db, preview_enabled=True, apply_enabled=True, id_factory=lambda: 'CB-empty')
    # Existing all-month legacy zero preview is a completed audit, never executable.
    preview = pipe.preview(BackfillSpec(condition(), '2024-01-01', '2024-01-31', ui_all_months=True))
    assert preview['targets'] == 0 and not db.targets
    before = deepcopy((db.expenses, db.requests, db.rules, db.category_updates))
    assert pipe.confirm('CB-empty', expected_count=0) == {'state': 'held', 'reason': 'target_count_changed'}
    assert pipe.apply('CB-empty', expected_count=0)['reason'] == 'explicit_confirmation_required'
    assert (db.expenses, db.requests, db.rules, db.category_updates) == before
    db.requests[0][2], db.requests[0][9] = 'confirmed', True
    forged = deepcopy((db.expenses, db.requests, db.rules, db.category_updates))
    assert pipe.apply('CB-empty', expected_count=0)['reason'] == 'explicit_confirmation_required'
    assert (db.expenses, db.requests, db.rules, db.category_updates) == forged
