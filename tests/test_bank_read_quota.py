"""Synthetic clocks/Google responses only; no credentials or production I/O."""
from copy import deepcopy
from types import SimpleNamespace

import pytest
from googleapiclient.errors import HttpError
from httplib2 import Response

from app.sheets import SheetsDB, SheetsReadPacer
from app import bank_fixed_backfill as fixed


class Clock:
    def __init__(self):
        self.now, self.sleeps = 0.0, []
    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


def quota():
    return HttpError(Response({'status': '429'}), b'private quota text')


def paced_db(clock, *, jitter=lambda: 0.5):
    return SheetsDB('synthetic', service=object(), read_sleeper=clock.sleep,
        read_pacer=SheetsReadPacer(clock=lambda: clock.now, sleep=clock.sleep, interval=1.25),
        read_retry_base=20, read_retry_attempts=5, read_retry_jitter=jitter)


def test_reads_and_retry_attempts_share_pacing_and_continue_after_quota_clears():
    clock, times = Clock(), []
    db = paced_db(clock)
    replies = iter([quota(), quota(), {'values': [[1]]}])
    def execute():
        times.append(clock.now)
        value = next(replies)
        if isinstance(value, Exception): raise value
        return value
    assert db._execute_sheet_read(lambda: SimpleNamespace(execute=execute)) == {'values': [[1]]}
    assert times == pytest.approx([0, 20.5, 61])
    assert db.sheet_read_metrics() == dict(logical=1, attempts=3, retries=2)


def test_continuous_reads_stay_below_sixty_in_every_rolling_minute():
    clock, times = Clock(), []
    db = paced_db(clock)
    for _ in range(150):
        db._execute_sheet_read(lambda: SimpleNamespace(execute=lambda: times.append(clock.now)))
    assert all(b-a >= 1.25 for a, b in zip(times, times[1:]))
    assert max(sum(t <= x < t+60 for x in times) for t in times) == 48


def test_quota_retry_is_bounded_then_stops_without_a_write():
    clock, times = Clock(), []
    db = paced_db(clock)
    def execute():
        times.append(clock.now)
        raise quota()
    with pytest.raises(HttpError):
        db._execute_sheet_read(lambda: SimpleNamespace(execute=execute))
    assert times == pytest.approx([0, 20.5, 61, 121, 181])
    assert clock.sleeps == pytest.approx([20.5, 40.5, 60, 60])
    assert db.sheet_read_metrics() == dict(logical=1, attempts=5, retries=4)


def test_shared_principal_pacer_spans_db_objects():
    clock, times = Clock(), []
    first = paced_db(clock)
    second = SheetsDB('other', service=object(), read_pacer=first._read_pacer)
    for db in [first, second, first]:
        db._execute_sheet_read(lambda: SimpleNamespace(execute=lambda: times.append(clock.now)))
    assert times == pytest.approx([0, 1.25, 2.5])


def test_financial_write_429_is_never_retried():
    calls, sleeps = [], []
    class Service:
        def spreadsheets(self): return self
        def values(self): return self
        def append(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(execute=lambda: (_ for _ in ()).throw(quota()))
    db = SheetsDB('synthetic', service=Service(), read_sleeper=sleeps.append,
                  read_retry_base=20, read_retry_attempts=5)
    with pytest.raises(HttpError): db.append_raw('収入明細', [['synthetic']])
    assert len(calls) == 1 and not sleeps
    assert db.sheet_read_metrics() == dict(logical=0, attempts=0, retries=0)


def test_raw_batch_read_is_fresh_preserves_empty_ranges_and_all_cells():
    calls = []
    rows = [[['ID', '', 123, '2026-10-28']], []]
    class Service:
        def spreadsheets(self): return self
        def values(self): return self
        def batchGet(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(execute=lambda: {'valueRanges': [{'values': deepcopy(r)} for r in rows]})
    db = SheetsDB('synthetic', service=Service())
    ranges = ['A!A:D', 'B!A:D']
    assert db.get_raw_ranges(ranges) == rows
    rows[0][0][2] = 124
    assert db.get_raw_ranges(ranges)[0][0][2] == 124
    assert len(calls) == 2 and calls[0]['valueRenderOption'] == 'UNFORMATTED_VALUE'


def test_missing_batch_range_stops_instead_of_skipping_a_financial_table():
    class Service:
        def spreadsheets(self): return self
        def values(self): return self
        def batchGet(self, **kwargs):
            return SimpleNamespace(execute=lambda: {'valueRanges': []})
    with pytest.raises(RuntimeError, match='range_count_mismatch'):
        SheetsDB('synthetic', service=Service()).get_raw_ranges(['A!A:D'])


def test_append_rechecks_target_after_full_guard_without_reusing_old_snapshot():
    reads, writes = [], []
    row = ['new', '2026-10-28', 100, '利息', 'interest', 'account', 'source', 'bank', 'reason', 'hash']
    current = []
    class DB:
        sid = 'synthetic'
        def get(self, rng): reads.append(rng); return deepcopy(current)
        def append_raw(self, *args): writes.append(args)
    def guard(): current.append(deepcopy(row))  # A conflicting writer after earlier observations.
    target = fixed.FixedWriteDB(DB(), [], {}, {'new': row}, guard)
    with pytest.raises(ValueError, match='duplicate_append'):
        target.append_raw('収入明細', [row])
    assert reads == ['収入明細!A2:J'] and not writes


def test_attestation_expiry_during_slow_read_stops_before_financial_request():
    writes = []
    row = ['new', '2026-10-28', 100, '利息', 'interest', 'account', 'source', 'bank', 'reason', 'hash']
    class DB:
        sid = 'synthetic'
        def get(self, rng): return []
        def append_raw(self, *args): writes.append(args)
    def expired(): raise RuntimeError('fresh_stop_attestation_required')
    target = fixed.FixedWriteDB(DB(), [], {}, {'new': row}, lambda: None, before_write=expired)
    with pytest.raises(RuntimeError, match='attestation'):
        target.append_raw('収入明細', [row])
    assert not writes


@pytest.mark.parametrize('interval', [0, 1, float('nan'), float('inf')])
def test_pacer_cannot_be_configured_above_supported_rate(interval):
    with pytest.raises(ValueError): SheetsReadPacer(interval=interval)
