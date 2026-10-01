"""Grouping authority uses synthetic PDFs and local OCR; no external services."""
from dataclasses import replace
from io import BytesIO
import json
from types import SimpleNamespace
from unittest.mock import Mock

from PIL import Image
import pytest

from app import receipt_pdf_grouping as grouping
from app import receipt_pdf_units as pdf
from app.receipt_privacy_gate import ReceiptPrivacyBlocked
from test_receipt_pdf_units import synthetic_pdf, local_ocr, pipeline


@pytest.fixture(autouse=True)
def local_development_context(monkeypatch):
    # These tests intentionally exercise the offline/local review CLI. Actions
    # authority is covered separately and must never restore this local JSON.
    monkeypatch.delenv('GITHUB_ACTIONS', raising=False)


def service(tmp_path, evidence=None):
    proposer = grouping.AdjacentPageGrouping(lambda observations: evidence) if evidence is not None else None
    return grouping.PdfGroupingService(grouping.GroupingStore(tmp_path / 'grouping'), proposer,
                                      pdf.PdfUnitManifestStore(tmp_path / 'observations'))


def receipt_page(n, *, receipt='R99', total=False, continuation=False):
    return grouping.evidence_from_text(
        f'店舗名: PRIVATE_STORE\n2026-10-01\nレシート番号: {receipt}\n{n}/2\n'
        + ('合計 100円' if total else '') + ('続き' if continuation else ''))


def observe(kinds):
    return pdf.observe_pdf(synthetic_pdf(kinds), 'drive-source-id')


def confirm(svc, observations, groups=None):
    view = svc.prepare(observations)
    if groups is not None:
        view = svc.review(observations, view['proposal']['proposal_digest'], action='edit', page_groups=groups)
        assert view['confirmation'] is None
        assert svc.units(observations) == ()
    return svc.review(observations, view['proposal']['proposal_digest'], action='confirm')


def test_same_receipt_candidate_only(tmp_path, local_ocr):
    svc = service(tmp_path, [receipt_page(1), receipt_page(2, total=True)])
    observations = observe(['normal', 'normal'])
    view = svc.prepare(observations)
    assert view['status'] == 'grouping_required'
    proposal = view['proposal']
    assert [g['page_numbers'] for g in proposal['groups']] == [[1, 2]]
    group = proposal['groups'][0]
    assert group['confidence'] == .95
    assert group['member_page_hashes'] == [p.page_hash for p in observations.pages]
    assert group['page_classifications'] == ['normal', 'normal']
    assert proposal['source_content_hash'] == observations.source_content_hash
    assert proposal['proposal_version'] == grouping.PROPOSAL_VERSION
    assert svc.units(observations) == ()
    with pytest.raises(grouping.GroupingError):
        svc.payload(observations, group['group_id'])
    assert 'PRIVATE_STORE' not in json.dumps(view)


@pytest.mark.parametrize('evidence', [
    [receipt_page(1), receipt_page(2, receipt='OTHER')],
    [receipt_page(1, total=True), receipt_page(2)],
    [grouping.PageEvidence(), grouping.PageEvidence()],
    [receipt_page(2), receipt_page(1)],
    [replace(receipt_page(1), date='different'), receipt_page(2)],
    [replace(receipt_page(1), issuer='different'), receipt_page(2)],
])
def test_distinct_or_weak_normal_transactions_are_singletons(tmp_path, local_ocr, evidence):
    view = service(tmp_path, evidence).prepare(observe(['normal', 'normal']))
    assert [g['page_numbers'] for g in view['proposal']['groups']] == [[1], [2]]


@pytest.mark.parametrize('kinds, expected', [
    (['normal', 'medical'], 'medical'), (['medical', 'normal'], 'medical'),
    (['normal', 'unknown'], 'sensitive_unknown'), (['normal', 'payroll'], 'payroll'),
    (['medical', 'payroll'], 'sensitive_unknown'),
])
def test_sensitive_candidates_and_human_mixed_group_stay_blocked(tmp_path, local_ocr, kinds, expected):
    svc = service(tmp_path, [receipt_page(1), receipt_page(2)])
    observations = observe(kinds)
    original = [p.classification for p in observations.pages]
    assert [g['page_numbers'] for g in svc.prepare(observations)['proposal']['groups']] == [[1], [2]]
    view = confirm(svc, observations, [[1, 2]])
    assert view['confirmation']['groups'][0]['page_classifications'] == original
    unit, = svc.units(observations)
    assert unit.classification == expected
    assert unit.metadata()['status'] == ('medical_pending' if expected == 'medical' else 'privacy_pending')
    assert view['confirmation']['groups'][0]['unit_status'] == unit.metadata()['status']
    assert [p.classification for p in observations.pages] == original
    with pytest.raises(grouping.GroupingError, match='payload_forbidden'):
        svc.payload(observations, unit.unit_id)


def test_three_pages_first_two_same_third_distinct(tmp_path, local_ocr):
    evidence = [receipt_page(1), receipt_page(2, total=True), receipt_page(1, receipt='R100')]
    view = service(tmp_path, evidence).prepare(observe(['normal'] * 3))
    assert [g['page_numbers'] for g in view['proposal']['groups']] == [[1, 2], [3]]


def test_continuation_without_numbering(tmp_path, local_ocr):
    evidence = [replace(receipt_page(1, continuation=True), printed_page=0, printed_count=0),
                replace(receipt_page(2), printed_page=0, printed_count=0)]
    view = service(tmp_path, evidence).prepare(observe(['normal'] * 2))
    assert view['proposal']['groups'][0]['page_numbers'] == [1, 2]
    assert view['proposal']['groups'][0]['confidence'] == .8


def test_proposal_failure_is_local_hold(tmp_path, local_ocr):
    svc = service(tmp_path)
    svc.proposer = Mock()
    svc.proposer.propose.side_effect = RuntimeError('PRIVATE_OCR_FAILURE')
    observations = observe(['normal', 'normal'])
    view = svc.prepare(observations)
    assert view['status'] == 'grouping_required' and view['proposal'] is None
    assert svc.units(observations) == ()
    assert 'PRIVATE_OCR_FAILURE' not in json.dumps(view)


def test_confirmed_replay_and_edit_replaces_all_unit_ids(tmp_path, local_ocr):
    svc = service(tmp_path, [receipt_page(1), receipt_page(2)])
    observations = observe(['normal', 'normal'])
    view = confirm(svc, observations)
    old_units = svc.units(observations)
    replay = service(tmp_path)
    replay.proposer = Mock(side_effect=AssertionError('must not regenerate confirmed proposal'))
    assert replay.prepare(observe(['normal', 'normal'])) == view
    assert replay.units(observations) == old_units
    edited = replay.review(observations, view['proposal']['proposal_digest'], action='edit', page_groups=[[1], [2]])
    assert edited['status'] == 'grouping_required'
    assert replay.units(observations) == ()
    replay.review(observations, edited['proposal']['proposal_digest'], action='confirm')
    assert set(unit.unit_id for unit in replay.units(observations)).isdisjoint(unit.unit_id for unit in old_units)


@pytest.mark.parametrize('change', ['content', 'count', 'page_hash', 'classification', 'extraction'])
def test_source_snapshot_change_revokes_authority(tmp_path, local_ocr, change):
    svc = service(tmp_path)
    observations = observe(['normal', 'normal'])
    view = confirm(svc, observations)
    if change == 'content':
        changed = pdf.observe_pdf(synthetic_pdf(['normal', 'normal'], metadata='new_source'), 'drive-source-id')
    elif change == 'count':
        changed = replace(observations, pages=observations.pages + (replace(observations.pages[-1], page_number=3),))
    else:
        kw = ({'page_hash': 'f' * 64} if change == 'page_hash' else
              {'classification': 'medical', 'grouping_hints': None} if change == 'classification' else
              {'extraction_status': 'ocr_failed', 'classification': 'sensitive_unknown',
               'observation_complete': False, 'grouping_hints': None})
        changed = replace(observations, pages=(replace(observations.pages[0], **kw), observations.pages[1]))
    assert svc.prepare(changed)['status'] == 'grouping_required'
    assert svc.units(changed) == ()
    with pytest.raises(grouping.GroupingError, match='review_stale'):
        svc.review(changed, view['proposal']['proposal_digest'], action='confirm')


@pytest.mark.parametrize('groups', [[[1], [1, 2]], [[2], [1]], [[1, 3], [2]], [[1]], [[0], [1, 2]], [[True], [2]], []])
def test_invalid_partition_rejected_without_losing_confirmation(tmp_path, local_ocr, groups):
    svc = service(tmp_path)
    observations = observe(['normal', 'normal'])
    view = confirm(svc, observations)
    with pytest.raises(grouping.GroupingError):
        svc.review(observations, view['proposal']['proposal_digest'], action='edit', page_groups=groups)
    assert svc.prepare(observations) == view


def test_payload_is_fresh_png_and_mock_general_analysis_only(tmp_path, local_ocr):
    svc = service(tmp_path)
    original = synthetic_pdf(['normal', 'normal'])
    observations = svc.observe(original, 'drive-source-id')
    confirm(svc, observations, [[1, 2]])
    unit, = svc.units(observations)
    payload = svc.payload(observations, unit.unit_id)
    assert payload.startswith(b'\x89PNG') and payload != original
    assert b'PRIVATE_ATTACHMENT_99' not in payload
    with Image.open(BytesIO(payload)) as image:
        assert image.size == (216, 432) and image.info == {}
        assert image.getpixel((0, 0)) == image.getpixel((0, 300)) == (0, 255, 0)
    import base64
    from app.gemini_ai import GeminiAI
    from test_gemini_ai import FakeInteractions
    ai = object.__new__(GeminiAI)
    transport = FakeInteractions()
    ai.client = SimpleNamespace(interactions=transport)
    ai.model = 'synthetic'
    ai.analyze_receipt(payload, 'image/png', [])
    media = transport.request['input'][1]
    assert media['mime_type'] == 'image/png'
    assert base64.b64decode(media['data']) == payload
    assert not unit.metadata()['accounting_allowed']
    # Even after local confirmation, normal ingestion has no grouped-unit route.
    ai = Mock()
    p = pipeline(tmp_path, ai=ai, observer=Mock())
    assert p.process_bytes(original, 'application/pdf', 'drive-source-id')['status'] == 'grouping_required'
    ai.analyze_receipt.assert_not_called()
    assert not p.db.append_calls and not p.db.income_rows
    p.medical_review_observer.observe.assert_not_called()


@pytest.mark.parametrize('kinds', [['normal', 'normal'], ['normal', 'medical'], ['medical', 'normal'], ['normal', 'unknown']])
def test_unconfirmed_no_gemini_sheets_medical_drive(tmp_path, local_ocr, monkeypatch, kinds):
    from app import cli
    from app import gemini_ai, google_clients
    ai, db, medical, drive = Mock(), Mock(), Mock(), Mock()
    monkeypatch.setattr(gemini_ai.GeminiAI, 'analyze_receipt', ai)
    monkeypatch.setattr(cli, 'make', db)
    monkeypatch.setattr(cli, 'make_receipt_pipeline', medical)
    monkeypatch.setattr(google_clients, 'drive_service', drive)
    svc = service(tmp_path)
    observations = svc.observe(synthetic_pdf(kinds), 'drive-source-id')
    assert svc.prepare(observations)['confirmation'] is None
    assert svc.units(observations) == ()
    with pytest.raises(grouping.GroupingError):
        svc.payload(observations, 'unconfirmed')
    for boundary in (ai, db, medical, drive):
        boundary.assert_not_called()


def test_exact_member_and_combined_payload_gates(tmp_path, local_ocr, monkeypatch):
    svc = service(tmp_path)
    observations = observe(['normal', 'normal'])
    confirm(svc, observations, [[1, 2]])
    unit, = svc.units(observations)
    altered = replace(observations, pages=(replace(observations.pages[0], _payload=b'changed'), observations.pages[1]))
    with pytest.raises(grouping.GroupingError, match='identity_changed'):
        svc.payload(altered, unit.unit_id)
    real = grouping._checked_gate
    def deny_combined(payload, known):
        with Image.open(BytesIO(payload)) as image:
            if image.height > 216:
                raise ReceiptPrivacyBlocked()
        return real(payload, known)
    monkeypatch.setattr(grouping, '_checked_gate', deny_combined)
    with pytest.raises(ReceiptPrivacyBlocked):
        svc.payload(observations, unit.unit_id)


def test_proposal_status_cannot_be_promoted_to_confirmation(tmp_path, local_ocr):
    svc = service(tmp_path)
    observations = observe(['normal', 'normal'])
    svc.prepare(observations)
    with svc.store.record(observations.source_file_id) as record:
        record['proposal']['status'] = 'confirmed'
        record['confirmation'] = {'status': 'confirmed'}
    assert svc.units(observations) == ()


def test_confirmation_tamper_is_rejected(tmp_path, local_ocr):
    svc = service(tmp_path)
    observations = observe(['normal', 'normal'])
    confirm(svc, observations)
    with svc.store.record(observations.source_file_id) as record:
        record['confirmation']['groups'][0]['member_page_hashes'][0] = '0' * 64
    assert svc.units(observations) == ()


def test_grouping_uses_minimised_observation_hints_without_reopening_images(tmp_path, local_ocr, monkeypatch):
    from dataclasses import asdict
    observations = observe(['normal', 'normal'])
    texts = ['店舗名: TEST\n2026-10-01\nレシート番号: R1\n1/2\n続き',
             '店舗名: TEST\n2026-10-01\nレシート番号: R1\n2/2\n合計']
    observations = replace(observations, pages=tuple(replace(p,
        grouping_hints=asdict(grouping.evidence_from_text(text)))
        for p, text in zip(observations.pages, texts)))
    ocr = Mock(side_effect=AssertionError('Grouping must not reopen observation images'))
    monkeypatch.setattr(grouping, '_extract_receipt_text', ocr)
    svc = service(tmp_path)
    assert svc.prepare(observations)['proposal']['groups'][0]['page_numbers'] == [1, 2]
    no_hints = replace(observations, pages=tuple(replace(p, grouping_hints=None) for p in observations.pages))
    view = service(tmp_path / 'no-hints').prepare(no_hints)
    assert [g['page_numbers'] for g in view['proposal']['groups']] == [[1], [2]]
    ocr.assert_not_called()


def test_cli_show_edit_confirm_replay_and_offline_export(tmp_path, local_ocr, capsys):
    path = tmp_path / 'receipt.pdf'
    path.write_bytes(synthetic_pdf(['normal', 'normal']))
    base = [str(path), '--source-file-id', 'source-local', '--state-dir', str(tmp_path / 'state'),
            '--observation-dir', str(tmp_path / 'observations')]
    assert grouping.main(['show', *base]) == 0
    view = json.loads(capsys.readouterr().out)
    assert grouping.main(['confirm', *base]) == 1
    capsys.readouterr()
    assert grouping.main(['edit', *base, '--expected-proposal', view['proposal']['proposal_digest'],
                          '--groups', '[[1,2]]']) == 0
    view = json.loads(capsys.readouterr().out)
    assert view['confirmation'] is None
    assert grouping.main(['confirm', *base, '--expected-proposal', view['proposal']['proposal_digest']]) == 0
    view = json.loads(capsys.readouterr().out)
    assert view['status'] == 'grouping_confirmed'
    assert grouping.main(['show', *base]) == 0
    assert json.loads(capsys.readouterr().out) == view
    assert grouping.main(['units', *base]) == 0
    unit = json.loads(capsys.readouterr().out)['units'][0]
    output = tmp_path / 'safe.png'
    assert grouping.main(['payload', *base, '--unit-id', unit['unit_id'], '--output', str(output)]) == 0
    capsys.readouterr()
    assert output.read_bytes().startswith(b'\x89PNG')
    assert grouping.main(['payload', *base, '--unit-id', unit['unit_id'], '--output', str(path)]) == 1
    capsys.readouterr()
    assert path.read_bytes().startswith(b'%PDF')


def test_store_corruption_and_busy_fail_closed(tmp_path, local_ocr):
    svc = service(tmp_path)
    observations = observe(['normal', 'normal'])
    svc.prepare(observations)
    path = next(svc.store.directory.glob('*.json'))
    path.write_text('invalid', encoding='utf-8')
    with pytest.raises(grouping.GroupingError, match='store_invalid'):
        svc.units(observations)
    path.with_suffix('.lock').touch()
    with pytest.raises(grouping.GroupingError, match='store_busy'):
        svc.units(observations)


def test_rejected_proposal_stays_rejected_and_can_be_edited(tmp_path, local_ocr):
    svc = service(tmp_path)
    observations = observe(['normal', 'normal'])
    view = confirm(svc, observations)
    rejected = svc.review(observations, view['proposal']['proposal_digest'], action='reject')
    assert svc.prepare(observations) == rejected
    assert svc.units(observations) == ()
    assert all(g['status'] == 'rejected' for g in rejected['proposal']['groups'])
    with pytest.raises(grouping.GroupingError, match='action_invalid'):
        svc.review(observations, rejected['proposal']['proposal_digest'], action='confirm')
    edit = svc.review(observations, rejected['proposal']['proposal_digest'], action='edit', page_groups=[[1, 2]])
    svc.review(observations, edit['proposal']['proposal_digest'], action='confirm')
    assert svc.units(observations)


def test_failed_observation_revokes_and_retains_identity_revision(tmp_path, local_ocr):
    svc = service(tmp_path)
    observations = observe(['normal', 'normal'])
    confirm(svc, observations)
    old = {u.unit_id for u in svc.units(observations)}
    invalid = replace(observations, pages=(), status='pdf_observation_failed')
    assert svc.prepare(invalid)['status'] == 'grouping_required'
    assert svc.units(invalid) == ()
    confirm(svc, observations)
    assert old.isdisjoint(u.unit_id for u in svc.units(observations))


def test_existing_sensitive_provenance_and_known_source_are_retained(tmp_path, local_ocr, monkeypatch):
    from test_receipt_pdf_units import TEXTS
    from app import receipt_text_extraction as extraction
    svc = service(tmp_path)
    content = synthetic_pdf(['normal', 'medical'])
    first = svc.observe(content, 'source')
    assert first.pages[1].classification == 'medical'
    monkeypatch.setattr(extraction, '_run_image_ocr', lambda image: TEXTS['normal'])
    monkeypatch.setattr(extraction, '_run_image_ocr_tokens', lambda image, page:
        (extraction._StructuredOcrToken(TEXTS['normal'], page, 1, 1, 20, 5, 99, (1, 1, 1, 5)),))
    replay = svc.observe(content, 'source')
    assert replay.pages[1].classification in {'medical', 'sensitive_unknown'}
    restricted = svc.observe(synthetic_pdf(['normal', 'normal']), 'known-sensitive',
                             known_source_classification='sensitive_unknown')
    assert all(p.classification == 'sensitive_unknown' for p in restricted.pages)


def test_stale_confirmation_direct_review_revokes_stored_authority(tmp_path, local_ocr):
    svc = service(tmp_path)
    observations = observe(['normal', 'normal'])
    view = confirm(svc, observations)
    changed = pdf.observe_pdf(synthetic_pdf(['normal', 'normal'], metadata='changed'), 'drive-source-id')
    with pytest.raises(grouping.GroupingError, match='review_stale'):
        svc.review(changed, view['proposal']['proposal_digest'], action='confirm')
    with svc.store.record('drive-source-id') as record:
        assert 'confirmation' not in record


def test_single_page_grouping_cli_does_not_overwrite_accounting_manifest(tmp_path, local_ocr):
    svc = service(tmp_path)
    content = synthetic_pdf(['normal'])
    p = pipeline(tmp_path)
    report = p.process_bytes(content, 'application/pdf', 'single')
    svc.observation_store = p.pdf_manifest_store
    assert svc.prepare(svc.observe(content, 'single'))['status'] == 'grouping_required'
    saved = json.loads(next(p.pdf_manifest_store.directory.glob('*.json')).read_text())
    assert saved == report
