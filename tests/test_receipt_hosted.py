"""Run the existing materializer/correction validator/store, never live IO."""
from copy import deepcopy
from hashlib import sha256
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app import receipt_hosted as h
from app.drive_run_state import StateError
from app.receipt_pipeline import ReceiptPipeline
from app.receipt_reimport_production import ReimportStore
from app.page_receipt_model import digest
from app.receipt_review_cases import snapshot_from_row
from test_receipt_reimport_production import fixtures


RAW = b'synthetic whole receipt'
SID = 'synthetic-hosted-source'


class Review:
    def __init__(self):
        self.cases, self.rows = {}, {}
        self.intakes = self.registrations = self.claims = self.finishes = 0
        self.lose_finish = False

    def intake(self, entry, row):
        self.intakes += 1
        self.cases[entry['source']['source_id']] = deepcopy(entry)
        self.row = deepcopy(row)

    def lookup_source(self, sid, hashed):
        return deepcopy(self.cases.get(sid))

    def register_once(self, *, amount=100, category='食費'):
        entry = next(iter(self.cases.values()))
        row = deepcopy(self.row)
        row[8] = 'register:' + entry['request_id']
        row[11], row[12], row[4], row[5] = amount, category, amount, 100-amount
        snapshot = snapshot_from_row(row, entry)
        frozen = [entry['request_id'], entry['case_id'], entry['source']['source_id'],
                  entry['source']['version'], entry['source']['sha256'],
                  json.dumps(snapshot), digest(snapshot), '受付済み', '', '2026-01-01T00:00:00.000Z']
        self.rows[entry['request_id']] = dict(entry=deepcopy(entry), row=frozen)
        self.registrations += 1
        return entry['request_id']

    def read(self, rid):
        return deepcopy(self.rows[rid])

    def pending(self):
        return [rid for rid, r in self.rows.items() if r['row'][7] == '受付済み']

    def claim(self, rid, before):
        assert self.rows[rid]['row'] == before
        self.claims += 1
        self.rows[rid]['row'][7] = '検証中'
        return deepcopy(self.rows[rid]['row'])

    def finish(self, rid, before, result):
        assert self.rows[rid]['row'] == before
        self.finishes += 1
        self.rows[rid]['row'][7:9] = [result['state'], json.dumps(result)]
        if self.lose_finish:
            raise TimeoutError('synthetic lost reply')
        return deepcopy(self.rows[rid]['row'])


def setup(monkeypatch, *, mismatch=False, writer=True, review=True):
    store, transport, old = fixtures()
    whole = deepcopy(store.value)
    whole[h.KEY] = dict(schema=h.KEY, enabled=True, financial_write_enabled=False,
                        records={}, requests={}, stopped=False)
    store.save(whole)
    parsed = old['pipeline'].reanalyze_bytes.return_value['parsed']
    from app.models import ReceiptResult
    parsed = ReceiptResult.model_validate(parsed)
    if mismatch:
        parsed.items[0].amount = 80
    ai = Mock()
    ai.analyze_receipt.return_value = parsed
    db = SimpleNamespace(categories=lambda: [('食費', '食品')])
    pipeline = ReceiptPipeline(db, ai)
    source = dict(source_id=SID, version='1', sha256=sha256(RAW).hexdigest(), mime_type='image/png')
    port = Review() if review else None
    sink = h.IsolatedReceiptWriter(db.categories()) if writer else None
    gate = SimpleNamespace(classification='normal', gemini_allowed=True, buyback_evidence=False)
    monkeypatch.setattr(h, 'evaluate_receipt_privacy', lambda *_a, **_k: gate)
    fresh = Mock(return_value=True)
    args = dict(sources={SID: source}, verify_source=fresh, review=port, writer=sink)
    return h.HostedReceiptPipeline(store, pipeline, **args), transport, ai, port, sink, gate, args


def restored(runtime, transport, args):
    old = runtime.owner.store
    store = ReimportStore(transport, old.expected_digest, old.spreadsheet_id)
    return h.HostedReceiptPipeline(store, runtime.pipeline, **args)


def test_normal_reaches_real_materializer_without_review_or_approval(monkeypatch):
    runtime, transport, ai, review, writer, _, args = setup(monkeypatch)
    result = runtime.process_bytes(RAW, 'image/png', SID)
    assert result['reason'] == 'synthetic_complete'
    assert [t for t, _ in writer.db.plan] == ['レシート', '支出明細', '取込データ']
    assert writer.db.plan[1][1][4] == 100
    assert review.intakes == review.registrations == 0
    assert ai.analyze_receipt.call_count == 1
    before = deepcopy(writer.db.plan)
    restored(runtime, transport, args).process_bytes(RAW, 'image/png', SID)
    assert writer.db.plan == before and ai.analyze_receipt.call_count == 1
    assert result['accounting_readback_complete'] is False  # synthetic is not production completion


def test_exception_reuses_analysis_one_registration_reaches_writer_and_same_result(monkeypatch):
    runtime, transport, ai, review, writer, _, args = setup(monkeypatch, mismatch=True)
    assert runtime.process_bytes(RAW, 'image/png', SID)['status'] == 'needs_review'
    assert writer.db.plan == [] and review.intakes == 1
    rid = review.register_once()
    fresh = restored(runtime, transport, args)
    result = fresh.register(rid)
    assert result['state'] == '検証済み・未記帳'
    assert writer.db.plan[1][1][4] == 100
    assert review.registrations == review.claims == review.finishes == 1
    assert json.loads(review.read(rid)['row'][8]) == result
    assert ai.analyze_receipt.call_count == 1
    with pytest.raises(StateError, match='replay'):
        restored(fresh, transport, args).register(rid)
    assert review.claims == 1


@pytest.mark.parametrize('amount,category', [(90, '食費'), (100, '存在しないカテゴリ')])
def test_failed_revalidation_never_calls_writer(monkeypatch, amount, category):
    runtime, _, ai, review, writer, _, _ = setup(monkeypatch, mismatch=True)
    runtime.process_bytes(RAW, 'image/png', SID)
    rid = review.register_once(amount=amount, category=category)
    assert runtime.register(rid)['state'] in {'保留', '修正が必要'}
    assert writer.db.plan == [] and ai.analyze_receipt.call_count == 1


@pytest.mark.parametrize('kind', ['medical', 'sensitive_unknown', 'payroll'])
def test_restrictive_originals_never_reach_ai_or_writer(monkeypatch, kind):
    runtime, _, ai, review, writer, gate, _ = setup(monkeypatch)
    gate.classification, gate.gemini_allowed = kind, False
    assert runtime.process_bytes(RAW, 'image/png', SID)['status'] == 'privacy_blocked'
    assert ai.analyze_receipt.call_count == review.intakes == 0 and writer.db.plan == []


def test_ai_unknown_google_reservation_survives_fresh_runner(monkeypatch):
    runtime, transport, ai, _, writer, _, args = setup(monkeypatch)
    ai.analyze_receipt.side_effect = TimeoutError()
    with pytest.raises(TimeoutError):
        runtime.process_bytes(RAW, 'image/png', SID)
    with pytest.raises(StateError, match='unknown_no_retry'):
        restored(runtime, transport, args)
    assert ai.analyze_receipt.call_count == 1 and writer.db.plan == []


def test_unknown_after_writer_keeps_google_request_and_never_resends(monkeypatch):
    runtime, transport, ai, review, writer, _, args = setup(monkeypatch, mismatch=True)
    runtime.process_bytes(RAW, 'image/png', SID)
    rid = review.register_once()
    review.lose_finish = True
    with pytest.raises(TimeoutError):
        runtime.register(rid)
    before = deepcopy(writer.db.plan)
    with pytest.raises(StateError, match='unknown_no_retry'):
        restored(runtime, transport, args)
    assert writer.db.plan == before and review.finishes == 1 and ai.analyze_receipt.call_count == 1


def test_analyzed_result_is_reused_when_review_transport_is_later_connected(monkeypatch):
    runtime, transport, ai, _, _, _, args = setup(monkeypatch, mismatch=True, review=False)
    with pytest.raises(StateError, match='pat_free_review_connection_required'):
        runtime.process_bytes(RAW, 'image/png', SID)
    args['review'] = Review()
    fresh = restored(runtime, transport, args)
    fresh.process_bytes(RAW, 'image/png', SID)
    assert ai.analyze_receipt.call_count == 1 and args['review'].intakes == 1


def test_duplicate_copy_and_old_owner_are_rejected_before_ai(monkeypatch):
    runtime, transport, ai, _, _, _, args = setup(monkeypatch)
    runtime.process_bytes(RAW, 'image/png', SID)
    args['sources']['synthetic-copy'] = dict(args['sources'][SID], source_id='synthetic-copy')
    with pytest.raises(StateError, match='duplicate_content'):
        restored(runtime, transport, args).process_bytes(RAW, 'image/png', 'synthetic-copy')
    assert ai.analyze_receipt.call_count == 1


def test_legacy_pending_owner_and_multipage_original_cannot_be_reowned(monkeypatch):
    runtime, _, ai, _, writer, _, args = setup(monkeypatch)
    whole = deepcopy(runtime.owner.store.value)
    whole['confirmation_items'] = {'old': dict(status='pending', source=args['sources'][SID])}
    runtime.owner.store.save(whole)
    with pytest.raises(StateError, match='historical_owner_conflict'):
        runtime.process_bytes(RAW, 'image/png', SID)
    assert ai.analyze_receipt.call_count == 0 and writer.db.plan == []
    runtime.owner.store.value['confirmation_items'] = {}
    args['sources'][SID]['mime_type'] = 'application/pdf'
    monkeypatch.setattr('app.pdf_intake_production.page_count', lambda _: 2)
    assert runtime.process_bytes(RAW, 'application/pdf', SID)['status'] == 'privacy_blocked'
    assert ai.analyze_receipt.call_count == 0


def test_pending_poll_consumes_one_registration_without_another_ai(monkeypatch):
    runtime, transport, ai, review, writer, _, args = setup(monkeypatch, mismatch=True)
    runtime.process_bytes(RAW, 'image/png', SID)
    review.register_once()
    assert restored(runtime, transport, args).process_pending() == 1
    assert writer.db.plan and ai.analyze_receipt.call_count == 1
    assert review.registrations == review.claims == review.finishes == 1


def test_remote_intent_failure_prevents_ai(monkeypatch):
    runtime, transport, ai, _, writer, _, args = setup(monkeypatch)
    transport.fail_before = True
    with pytest.raises(StateError, match='write_unknown'):
        runtime.process_bytes(RAW, 'image/png', SID)
    assert ai.analyze_receipt.call_count == 0 and writer.db.plan == []


def test_existing_bound_analysis_is_reused_without_ai(monkeypatch):
    runtime, transport, ai, review, writer, _, args = setup(monkeypatch, mismatch=True)
    store = runtime.owner.store
    whole = deepcopy(store.value)
    source = whole['manifest']['sources'][0]
    whole['records'][source['source_id']] = dict(phase='complete',
        parsed=ai.analyze_receipt.return_value.model_dump())
    store.save(whole)
    args['sources'] = {source['source_id']: source}
    fresh = restored(runtime, transport, args)
    assert fresh.process_bytes(b'fixture', 'image/png', source['source_id'])['status'] == 'needs_review'
    assert ai.analyze_receipt.call_count == 0 and review.intakes == 1 and writer.db.plan == []


def test_state_missing_corrupt_stopped_or_live_write_enabled_fails_closed(monkeypatch):
    runtime, _, _, _, _, _, args = setup(monkeypatch)
    original = deepcopy(runtime.owner.store.value)
    for change in ('missing', 'stopped', 'write', 'corrupt'):
        value = deepcopy(original)
        if change == 'missing':
            del value[h.KEY]
        elif change == 'stopped':
            value[h.KEY]['stopped'] = True
        elif change == 'write':
            value[h.KEY]['financial_write_enabled'] = True
        else:
            value[h.KEY]['requests'] = []
        runtime.owner.store.value = value
        with pytest.raises(StateError):
            h.HostedReceiptPipeline(runtime.owner.store, runtime.pipeline, **args)


def test_live_factory_denies_local_or_self_hosted_before_google(monkeypatch):
    monkeypatch.setattr('app.production_flow.verify_execution_boundary', lambda *_: None)
    for env in ({}, {'RUNNER_ENVIRONMENT': 'self-hosted', 'RUNNER_OS': 'Linux'}):
        with pytest.raises(StateError, match='hosted_runner_required'):
            h.open_pipeline(None, None, [], env)


def test_production_source_uses_existing_hosted_receipt_entry(monkeypatch, tmp_path):
    from app import production_source as source, drive_receipts as drive
    runtime, _, ai, review, writer, _, args = setup(monkeypatch)
    raw = tmp_path/'original'
    raw.write_bytes(RAW)
    plan = tmp_path/'plan.json'
    entry = dict(args['sources'][SID], path=str(raw))
    plan.write_text(json.dumps(dict(sources=[entry], multipage_sources=[], hosted_receipts=True)))
    monkeypatch.setenv('RECEIPT_CONFIRMATION_BINDING', 'synthetic-binding')
    monkeypatch.setenv('RECEIPT_SCAN_PLAN', str(plan))
    monkeypatch.setattr(source, 'SheetsDB', lambda *_a, **_k: object())
    monkeypatch.setattr(source, 'make_receipt_pipeline', lambda *_: runtime.pipeline)
    calls = []
    monkeypatch.setattr(h, 'open_pipeline', lambda *_a: calls.append('hosted') or runtime)
    service = Mock()
    service.files().list().execute.return_value = {'files': [dict(id=SID, name='synthetic',
        mimeType='image/png', version='1', parents=['synthetic-inbox'])]}
    monkeypatch.setattr(drive, 'drive_service', lambda: service)
    settings = SimpleNamespace(validate=lambda **_: None, spreadsheet_id='synthetic-sheet',
        receipt_drive_folder_id='synthetic-inbox', processed_drive_folder_id='synthetic-processed')
    result = source.receipts(settings, apply=True)
    assert calls == ['hosted'] and writer.db.plan and review.intakes == 0
    assert result['failure'] == 0 and ai.analyze_receipt.call_count == 1
    service.files().update.assert_not_called()
