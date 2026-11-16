"""Offline existing-HTTP profiles: comparisons add no requests or authority."""
from hashlib import sha256
import logging

import pytest

from app import receipt_get_diagnostics as diagnostics
from app.receipt_sheet_worker import ExistingReceiptDriveIO
from test_receipt_readback_diagnostics import (
    CASES, NEW_CODES, PRIVATE, READ_SHAPE, api_shape, exercise, no_live_or_financial_io,
)
from test_receipt_sheet_validation import RID
from test_receipt_worker_stages import PRIVATE_CODES, WORKER_CODES


class UnobservedTransport(ExistingReceiptDriveIO):
    """Control arm: identical guarded transport, with diagnostics detached."""
    def __init__(self, *args):
        super().__init__(*args)
        del self._receipt_get_diagnostics


PROFILES = (
    None, 'cas_version_lag', 'post_version_up', 'post_version_down', 'post_etag',
    'post_both', 'external_update', 'body_mismatch', 'etag_unchanged',
    'put_before', 'put_response', 'body_get',
)


def diagnostic_messages(caplog):
    records = [r for r in caplog.records if r.name == diagnostics.__name__]
    assert all(r.msg in diagnostics.CODES and not r.args and not r.exc_info and not r.stack_info
               for r in records)
    assert all(PRIVATE not in r.getMessage() and RID not in r.getMessage() for r in caplog.records)
    return [r.getMessage() for r in records]


@pytest.mark.parametrize('fault', PROFILES, ids=lambda v: v or 'normal-cas-version-change')
def test_profiles_preserve_exact_http_persistence_and_stop(fault, caplog):
    control = exercise(fault, transport_type=UnobservedTransport)
    caplog.set_level(logging.WARNING)
    observed = exercise(fault, on_claim=caplog.clear)
    # Includes every URL, GET/PUT params, If-Match, body digest, durable bytes,
    # CAS attempts/writes, ACK/cache state and stop after reconciliation.
    assert observed == control
    messages = diagnostic_messages(caplog)
    assert messages
    assert set(caplog.messages) <= diagnostics.CODES | PRIVATE_CODES | WORKER_CODES | NEW_CODES | {
        'receipt_private_metadata_json_version_changed', 'receipt_private_metadata_json_etag_changed',
    }
    assert sum(c['method'] == 'PUT' for c in observed['api_trace']) == 1
    prefix = READ_SHAPE * 2 + READ_SHAPE[2:7] + [('synthetic-private', 'PUT')]
    post_count = 0 if fault in ('put_before', 'put_response') else (
        18 if fault is None else 7 if fault == 'external_update' else 6 if fault == 'body_get' else 9)
    assert api_shape(observed['api_trace']) == prefix + (READ_SHAPE * 2)[:post_count]
    assert observed['cas_attempts'] == (4 if fault == 'external_update' else 3)
    assert observed['cas_writes'] == (2 if fault == 'put_before' else observed['cas_attempts'])
    persistent = observed['persistent_journal']
    assert persistent['generation'] == (2 if fault == 'put_before' else 4 if fault == 'external_update' else 3)
    assert persistent['requests'][RID]['state'] == ('受付済み' if fault == 'put_before' else '検証中')
    assert persistent['requests'][RID]['result'] is None
    assert observed['error'] == (None if fault is None else 'receipt_private_write_unknown')
    assert observed['stopped'] is (fault is not None)
    assert observed['stopped_after_reconcile'] is (fault is not None)
    if fault is not None:
        assert observed['replay_error'] == 'receipt_private_write_stopped'


@pytest.mark.parametrize('fault,expected', [
    (None, ('cas_boundary_version_up', 'cas_boundary_etag_different',
            'post_outer_pair_version_same', 'post_outer_pair_etag_same', 'post_body_same')),
    ('cas_version_lag', ('cas_boundary_version_same', 'post_outer_pair_version_up',
                         'post_outer_pair_etag_same', 'post_body_same')),
    ('post_version_up', ('post_outer_pair_version_up', 'post_outer_pair_etag_same')),
    ('post_version_down', ('post_outer_pair_version_down', 'post_outer_pair_etag_same')),
    ('post_etag', ('post_outer_pair_version_same', 'post_outer_pair_etag_different')),
    ('post_both', ('post_outer_pair_version_up', 'post_outer_pair_etag_different')),
    ('external_update', ('post_outer_transport_etag_different', 'post_body_different', 'post_incomplete')),
    ('body_mismatch', ('post_body_different', 'post_outer_pair_version_same', 'post_outer_pair_etag_same')),
    ('put_before', ('cas_unacknowledged',)),
    ('put_response', ('cas_unacknowledged',)),
    ('body_get', ('post_incomplete',)),
])
def test_fixed_codes_distinguish_observations_without_claiming_a_cause(caplog, fault, expected):
    caplog.set_level(logging.WARNING)
    exercise(fault, on_claim=caplog.clear)
    messages = diagnostic_messages(caplog)
    assert all('receipt_get_diag_' + suffix in messages for suffix in expected)
    if fault not in ('put_before', 'put_response', 'body_get'):
        assert 'receipt_get_diag_post_transport_pair_version_unavailable' in messages
    if fault == 'external_update':
        assert 'receipt_get_diag_post_outer_after' not in messages  # Existing short-circuit.
    if fault in ('put_before', 'put_response'):
        assert 'receipt_get_diag_cas_acknowledged' not in messages
        assert not any('receipt_get_diag_post_' in m for m in messages)
    # No label pretends to prove an external actor, delayed propagation or API bug.
    assert not any(word in m for m in messages for word in ('external_update', 'api_bug', 'cause_confirmed'))


@pytest.mark.parametrize('fault,_code', CASES, ids=[f or 'success' for f, _ in CASES])
def test_all_existing_fault_profiles_are_identical_with_observer_detached(fault, _code, caplog):
    caplog.set_level(logging.WARNING)
    assert exercise(fault) == exercise(fault, transport_type=UnobservedTransport)
    diagnostic_messages(caplog)


def test_order_and_elapsed_are_completion_intervals_in_closed_buckets(monkeypatch, caplog):
    times = iter((0, .01, .02, .03, .04, .05, 1.5, 1.6, 2.7, 7.8))
    monkeypatch.setattr(diagnostics, 'monotonic', lambda: next(times))
    caplog.set_level(logging.WARNING)
    exercise(None, on_claim=caplog.clear)
    messages = diagnostic_messages(caplog)
    assert 'receipt_get_diag_cas_boundary_elapsed_under_5s' in messages
    assert 'receipt_get_diag_post_outer_pair_elapsed_at_least_5s' in messages
    assert 'receipt_get_diag_post_transport_pair_elapsed_under_5s' in messages
    assert not any(m.endswith('_order_unexpected') for m in messages)


@pytest.mark.parametrize('old,new,relation', [
    ('9', '10', 'up'), ('10', '9', 'down'), ('10', '10', 'same'),
    ('01', '1', 'representation_changed'), (PRIVATE, '10', 'unavailable'),
    ('9' * 10000, '10', 'unavailable'), ('１０', '10', 'unavailable'),
])
def test_numeric_direction_never_uses_lexical_etag_or_unbounded_parsing(caplog, old, new, relation):
    caplog.set_level(logging.WARNING)
    observer = diagnostics.ExistingGetDiagnostics()
    observer.begin(PRIVATE.encode())
    observer.metadata('outer_before', dict(version=old, etag='"' + PRIVATE + '"'))
    observer.metadata('outer_after', dict(version=new, etag='"' + PRIVATE + '"'))
    observer.finish()
    assert 'receipt_get_diag_pre_outer_pair_version_' + relation in diagnostic_messages(caplog)
    assert observer.samples == {} and observer.planned is None and observer.phase is None
    assert sha256(PRIVATE.encode()).hexdigest() not in caplog.text


@pytest.mark.parametrize('component', ['monotonic', 'sha256', '_emit'])
def test_diagnostic_faults_do_not_change_success_or_failure(monkeypatch, component, caplog):
    controls = {f: exercise(f, transport_type=UnobservedTransport) for f in (None, 'body_mismatch', 'put_response')}
    def broken(*args, **kwargs):
        raise RuntimeError(PRIVATE)
    monkeypatch.setattr(diagnostics, component, broken)
    caplog.clear()
    for fault, control in controls.items():
        assert exercise(fault) == control
    assert PRIVATE not in caplog.text


def test_no_raw_or_encoded_values_in_logs_and_no_retained_values_after_finish(caplog):
    caplog.set_level(logging.WARNING)
    observer = diagnostics.ExistingGetDiagnostics()
    sentinel = PRIVATE + RID
    observer.begin(sentinel.encode())
    observer.metadata('outer_before', dict(version='37', etag='"' + sentinel + '"', id=sentinel))
    observer.metadata('outer_after', dict(version='38', etag='"different-' + sentinel + '"'))
    observer.enter_cas()
    observer.cas_ok()
    observer.body(sentinel.encode())
    observer.finish()
    diagnostic_messages(caplog)
    assert sentinel not in caplog.text and sha256(sentinel.encode()).hexdigest() not in caplog.text
    assert observer.__dict__ == dict(phase=None, samples={}, planned=None, ordinal=0, body_seen=False)
    assert all(r.args == () and r.exc_info is None for r in caplog.records)
