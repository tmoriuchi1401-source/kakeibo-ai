"""Synthetic PDFs, real local render/gates, mocked OCR and Gemini only."""
from io import BytesIO
import json
from types import SimpleNamespace
from unittest.mock import Mock

from PIL import Image
from pypdf import PdfReader, PdfWriter
from pypdf.generic import (DictionaryObject, NameObject, DecodedStreamObject,
                          ArrayObject, NumberObject, TextStringObject)
import pytest

from app import receipt_pdf_units as pdf, receipt_text_extraction as extraction
from app.drive_receipts import should_archive_result
from app.gemini_ai import GeminiAI
from app.receipt_pipeline import ReceiptPipeline
from app.receipt_privacy_gate import ReceiptPrivacyBlocked, require_receipt_ai_permission
from test_receipt_pipeline import FakeDB, FakeAI, _normal_receipt_result

TEXTS = {'normal': 'レシート 商品 小計 100円 現金 100円',
         'medical': '病院 診療 患者番号 PRIVATE_MEDICAL_42 支払額 1200円',
         'payroll': '給与明細 基本給 300000円', 'unknown': 'unresolved document',
         'failed': 'OCR fails', 'incomplete': 'レシート 商品 合計 100円'}
COLORS = {'normal': (0, 255, 0), 'medical': (255, 0, 0),
          'payroll': (0, 0, 255), 'unknown': (255, 255, 0),
          'failed': (255, 0, 255), 'incomplete': (0, 255, 255)}


def synthetic_pdf(kinds, *, embedded=(), metadata='PRIVATE_ATTACHMENT_99'):
    """Create coloured scan pages, optionally including extractable Unicode text."""
    writer = PdfWriter()
    for n, kind in enumerate(kinds):
        page = writer.add_blank_page(width=72, height=72)
        color = ' '.join(str(v / 255) for v in COLORS[kind])
        drawing = f'{color} rg 0 0 72 72 re f\n'.encode()
        if n in embedded:
            # A Type0/ToUnicode font preserves Japanese text for pypdf while
            # letting PDFium render the same independent page observation.
            text = TEXTS[kind]
            cmap = DecodedStreamObject()
            mapping = '\n'.join(f'<{i:04x}> <{ord(c):04x}>' for i, c in enumerate(text, 1))
            cmap.set_data(('begincmap\n1 begincodespacerange\n<0000> <ffff>\n'
                           'endcodespacerange\n' + str(len(text)) + ' beginbfchar\n'
                           + mapping + '\nendbfchar\nendcmap').encode())
            descendant = DictionaryObject({NameObject('/Type'): NameObject('/Font'),
                NameObject('/Subtype'): NameObject('/CIDFontType2'),
                NameObject('/BaseFont'): NameObject('/Arial'),
                NameObject('/CIDSystemInfo'): DictionaryObject({
                    NameObject('/Registry'): TextStringObject('Adobe'),
                    NameObject('/Ordering'): TextStringObject('Identity'),
                    NameObject('/Supplement'): NumberObject(0)})})
            font = DictionaryObject({NameObject('/Type'): NameObject('/Font'),
                NameObject('/Subtype'): NameObject('/Type0'), NameObject('/BaseFont'): NameObject('/Arial'),
                NameObject('/Encoding'): NameObject('/Identity-H'),
                NameObject('/DescendantFonts'): ArrayObject([writer._add_object(descendant)]),
                NameObject('/ToUnicode'): writer._add_object(cmap)})
            page[NameObject('/Resources')] = DictionaryObject({NameObject('/Font'):
                DictionaryObject({NameObject('/F1'): writer._add_object(font)})})
            encoded = ''.join(f'{i:04x}' for i in range(1, len(text) + 1))
            drawing += f'0 0 0 rg BT /F1 2 Tf 5 36 Td <{encoded}> Tj ET'.encode()
        stream = DecodedStreamObject()
        stream.set_data(drawing)
        page[NameObject('/Contents')] = writer._add_object(stream)
    writer.add_metadata({'/Title': metadata})
    writer.add_attachment('sensitive.txt', metadata.encode())
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


def kind_of(image):
    return next(kind for kind, color in COLORS.items() if image.convert('RGB').getpixel((0, 0)) == color)


@pytest.fixture
def local_ocr(monkeypatch):
    observed = []
    def text(image):
        kind = kind_of(image)
        observed.append(kind)
        if kind == 'failed':
            raise RuntimeError('PRIVATE_OCR_FAILURE')
        return TEXTS[kind]
    def tokens(image, page):
        kind = kind_of(image)
        if kind == 'incomplete':
            return ()
        return (extraction._StructuredOcrToken(TEXTS[kind], page, 1, 1, 20, 5, 99, (1, 1, 1, 5)),)
    monkeypatch.setattr(extraction, '_run_image_ocr', text)
    monkeypatch.setattr(extraction, '_run_image_ocr_tokens', tokens)
    return observed


class Ledger(FakeDB):
    def get(self, name):
        if name == '取込データ!A2:L':
            return [row for sheet, rows in self.append_calls if sheet == '取込データ' for row in rows]
        return super().get(name)


def pipeline(tmp_path, ai=None, db=None, observer=None):
    return ReceiptPipeline(db or Ledger(), ai or FakeAI(_normal_receipt_result()),
        medical_review_observer=observer, pdf_manifest_store=pdf.PdfUnitManifestStore(tmp_path / 'units'))


@pytest.mark.parametrize('kinds', [
    ('normal',), ('medical',), ('payroll',), ('unknown',), ('incomplete',), ('failed',),
    ('normal', 'normal'), ('normal', 'medical'), ('medical', 'normal'),
    ('normal', 'unknown'), ('normal', 'payroll'), ('normal', 'incomplete'),
    ('normal', 'failed', 'normal'), ('normal',) * 5, ('normal', 'medical', 'normal'),
])
def test_pages_are_privacy_observations_only_single_normal_can_send(tmp_path, local_ocr, kinds):
    original = synthetic_pdf(kinds)
    ai = FakeAI(_normal_receipt_result())
    def analyze(payload, mime, cats, **policy):
        # At the first send, every page has already passed local observation.
        assert set(local_ocr) >= set(kinds)
        assert payload != original and not payload.startswith(b'%PDF')
        assert mime == 'image/png'
        with Image.open(BytesIO(payload)) as image:
            assert kind_of(image) == 'normal'
            assert image.size == (216, 216)
            assert image.info == {}  # fresh pixels, no PDF metadata/attachments
        assert b'PRIVATE_ATTACHMENT_99' not in payload
        assert b'PRIVATE_MEDICAL_42' not in payload
        return _normal_receipt_result()
    ai.analyze_receipt.side_effect = analyze
    observer = Mock()
    p = pipeline(tmp_path, ai=ai, observer=observer)
    result = p.process_bytes(original, 'application/pdf', 'source-pdf')
    assert len(result['pages']) == result['page_count'] == len(kinds)
    assert set(local_ocr) >= set(kinds)
    for n, page in enumerate(result['pages']):
        assert page['source_file_id'] == 'source-pdf' and page['page_number'] == n + 1
        assert len(page['page_hash']) == len(page['source_content_hash']) == 64
        assert page['classification'] == ('sensitive_unknown' if kinds[n] in
            {'unknown', 'failed', 'incomplete'} else kinds[n])
    if len(kinds) > 1:
        assert result['units'] == []
        assert result['observation_status'] == 'observed'
        assert result['status'] == 'grouping_required'
        assert not result['all_units_terminal']
        ai.analyze_receipt.assert_not_called()
        assert p.db.append_calls == [] and p.db.income_rows == []
        assert p.db.ensure_expense_status_column_calls == p.db.category_calls == 0
    else:
        assert len(result['units']) == 1
        assert result['units'][0]['page_range'] == [1, 1]
        assert result['units'][0]['status'] == ('imported' if kinds[0] == 'normal' else
            'medical_pending' if kinds[0] == 'medical' else 'privacy_pending')
        assert ai.analyze_receipt.call_count == int(kinds[0] == 'normal')
        if kinds[0] != 'normal':
            assert p.db.append_calls == []
    assert not should_archive_result(result) and not result['archive_allowed']
    observer.observe.assert_not_called()
    saved = json.loads(next((tmp_path / 'units').glob('*.json')).read_text())
    assert saved == result
    assert 'PRIVATE_MEDICAL_42' not in json.dumps(saved)
    assert 'PRIVATE_OCR_FAILURE' not in json.dumps(saved)


def test_embedded_and_scan_all_observed(tmp_path, local_ocr):
    original = synthetic_pdf(['normal', 'medical'], embedded=(0,))
    reader = PdfReader(BytesIO(original))
    assert reader.pages[0].extract_text() == TEXTS['normal']
    assert not reader.pages[1].extract_text()
    result = pipeline(tmp_path).process_bytes(original, 'application/pdf', 'mixed')
    assert result['status'] == 'grouping_required' and result['units'] == []
    assert [p['classification'] for p in result['pages']] == ['normal', 'medical']
    assert {'normal', 'medical'} <= set(local_ocr)


def test_embedded_sensitive_cannot_be_ignored_even_if_visual_ocr_says_normal(tmp_path, local_ocr, monkeypatch):
    original = synthetic_pdf(['medical'], embedded=(0,))
    monkeypatch.setattr(extraction, '_run_image_ocr', lambda image: TEXTS['normal'])
    monkeypatch.setattr(extraction, '_run_image_ocr_tokens', lambda image, page:
        (extraction._StructuredOcrToken(TEXTS['normal'], 1, 1, 1, 20, 5, 99, (1, 1, 1, 5)),))
    p = pipeline(tmp_path)
    result = p.process_bytes(original, 'application/pdf', 'embedded')
    assert result['units'][0]['status'] == 'medical_pending'
    p.ai.analyze_receipt.assert_not_called()


def test_render_failure_keeps_page_observations_without_accounting(tmp_path, local_ocr, monkeypatch):
    render = pdf._render_png
    count = 0
    def fail_second(page, *args):
        nonlocal count
        count += 1
        if count == 2:
            raise RuntimeError('PRIVATE_RENDER_FAILURE')
        return render(page, *args)
    monkeypatch.setattr(pdf, '_render_png', fail_second)
    p = pipeline(tmp_path)
    result = p.process_bytes(synthetic_pdf(['normal'] * 3), 'application/pdf', 'render')
    assert [p['classification'] for p in result['pages']] == ['normal', 'sensitive_unknown', 'normal']
    assert result['pages'][1]['extraction_status'] == 'pdf_render_failed'
    assert result['status'] == 'grouping_required' and result['units'] == []
    p.ai.analyze_receipt.assert_not_called()
    assert p.db.append_calls == []


@pytest.mark.parametrize('case', ['pages', 'pixels', 'bytes', 'payload', 'encrypted', 'corrupt'])
def test_document_limits_and_failures_never_partially_send(tmp_path, local_ocr, monkeypatch, case):
    original = synthetic_pdf(['normal'] * 2)
    if case == 'pages': monkeypatch.setattr(pdf, 'MAX_PDF_PAGES', 1)
    if case == 'pixels': monkeypatch.setattr(pdf.bounded, 'MAX_TOTAL_RENDER_PIXELS', 1)
    if case == 'bytes': monkeypatch.setattr(pdf, 'MAX_SOURCE_BYTES', 1)
    if case == 'payload': monkeypatch.setattr(pdf, 'MAX_PAYLOAD_BYTES', 1)
    if case == 'corrupt': original = b'%PDF-corrupt'
    if case == 'encrypted':
        writer = PdfWriter(BytesIO(original)); writer.encrypt('secret')
        out = BytesIO(); writer.write(out); original = out.getvalue()
    p = pipeline(tmp_path)
    result = p.process_bytes(original, 'application/pdf', 'limit')
    assert result['status'] == ('grouping_required' if case in {'pixels', 'payload'} else 'needs_review')
    assert result['units'] == []
    if case in {'pixels', 'payload'}:
        assert len(result['pages']) == 2
        assert all(p['classification'] == 'sensitive_unknown' for p in result['pages'])
    assert not result['all_units_terminal'] and not should_archive_result(result)
    p.ai.analyze_receipt.assert_not_called()
    assert p.db.append_calls == []


def test_replay_and_changed_source_identities(tmp_path, local_ocr):
    data = synthetic_pdf(['normal', 'medical', 'normal'])
    db, ai = Ledger(), FakeAI(_normal_receipt_result())
    first = pipeline(tmp_path, db=db, ai=ai).process_bytes(data, 'application/pdf', 'replay')
    before = list(db.append_calls)
    second = pipeline(tmp_path, db=db, ai=ai).process_bytes(data, 'application/pdf', 'replay')
    assert first == second
    assert db.append_calls == before == []
    assert first['status'] == 'grouping_required' and first['units'] == []
    ai.analyze_receipt.assert_not_called()
    observations = pdf.observe_pdf(data, 'replay')
    # Future candidate identity remains stable, without creating ledger units.
    ids = [u.source_id for u in pdf.SinglePageGrouping().units(observations.pages)]
    assert len(set(ids)) == 3
    changed = pdf.observe_pdf(synthetic_pdf(['normal', 'medical', 'normal'], metadata='changed'), 'replay')
    assert not set(ids) & {u.source_id for u in pdf.SinglePageGrouping().units(changed.pages)}
    renamed = pdf.observe_pdf(data, 'other-source')
    assert not set(ids) & {u.source_id for u in pdf.SinglePageGrouping().units(renamed.pages)}


@pytest.mark.parametrize('failed_sheet', ['レシート', '支出明細', '取込データ'])
def test_partial_ledger_failure_replay_deduplicates(tmp_path, local_ocr, failed_sheet):
    data = synthetic_pdf(['normal'])
    db = Ledger(fail_after_commit=failed_sheet)
    p = pipeline(tmp_path, db=db)
    with pytest.raises(RuntimeError, match='synthetic'):
        p.process_bytes(data, 'application/pdf', 'interrupted')
    result = pipeline(tmp_path, db=db).process_bytes(data, 'application/pdf', 'interrupted')
    assert result['status'] == 'completed'
    for title in ['レシート', '支出明細', '取込データ']:
        ids = [row[0] for sheet, rows in db.append_calls if sheet == title for row in rows]
        assert len(ids) == len(set(ids)) == 1


def test_review_marker_replay_remains_unresolved(tmp_path, local_ocr):
    receipt = _normal_receipt_result().model_copy(update={'date': ''})
    ai, db = FakeAI(receipt), Ledger()
    data = synthetic_pdf(['normal'])
    first = pipeline(tmp_path, ai=ai, db=db).process_bytes(data, 'application/pdf', 'review')
    second = pipeline(tmp_path, ai=ai, db=db).process_bytes(data, 'application/pdf', 'review')
    assert first['units'][0]['status'] == second['units'][0]['status'] == 'needs_review'
    assert not second['all_units_terminal']
    assert ai.analyze_receipt.call_count == 1


@pytest.mark.parametrize('classification', ['medical', 'payroll', 'sensitive_unknown'])
def test_known_sensitive_provenance_blocks_every_page(tmp_path, local_ocr, classification):
    p = pipeline(tmp_path)
    result = p.process_bytes(synthetic_pdf(['normal'] * 2), 'application/pdf', 'restricted',
                             known_source_classification=classification)
    assert all(u['classification'] != 'normal' for u in result['pages'])
    p.ai.analyze_receipt.assert_not_called()


@pytest.mark.parametrize('mime', ['application/pdf', ' Application/PDF ', 'image/png'])
@pytest.mark.parametrize('kinds', [('normal',), ('normal', 'medical')])
def test_adapter_and_permission_boundary_unconditionally_reject_original_pdf(monkeypatch, mime, kinds):
    data = synthetic_pdf(kinds)
    transport = Mock()
    ai = object.__new__(GeminiAI)
    ai.client = SimpleNamespace(interactions=SimpleNamespace(create=transport))
    ai.model = 'synthetic'
    # Even a caller trying to bypass the normal permission function cannot send PDF.
    monkeypatch.setattr('app.gemini_ai.require_receipt_ai_permission', lambda *a, **k: None)
    with pytest.raises(ReceiptPrivacyBlocked):
        ai.analyze_receipt(data, mime, [])
    with pytest.raises(ReceiptPrivacyBlocked):
        require_receipt_ai_permission(data, mime)
    transport.assert_not_called()


def test_pdf_reanalysis_cannot_reach_mock_adapter(tmp_path, local_ocr):
    p = pipeline(tmp_path)
    result = p.reanalyze_bytes(synthetic_pdf(['normal', 'medical']), 'application/pdf', 'reanalyze',
                              destination='https://generativelanguage.googleapis.com')
    assert result['status'] == 'privacy_blocked'
    p.ai.analyze_receipt.assert_not_called()


def test_source_terminality_is_conservative_and_archive_always_disabled():
    for states, expected in [([], False), (['imported', 'medical_pending'], False),
                            (['imported', 'needs_review'], False),
                            (['imported', 'confirmed', 'intentionally_skipped'], True)]:
        result = {'units': [{'status': state} for state in states], 'document_type': 'pdf_page_units'}
        pdf.update_document_status(result)
        assert result['all_units_terminal'] == expected
        assert result['archive_allowed'] is False and not should_archive_result(result)


def test_real_adapter_transport_contains_only_normal_page(tmp_path, local_ocr):
    import base64
    from test_gemini_ai import FakeInteractions
    data = synthetic_pdf(['normal'])
    ai = object.__new__(GeminiAI)
    interactions = FakeInteractions()
    ai.client = SimpleNamespace(interactions=interactions)
    ai.model = 'synthetic'
    result = pipeline(tmp_path, ai=ai).process_bytes(data, 'application/pdf', 'real-adapter')
    assert result['status'] == 'completed'
    media = interactions.request['input'][1]
    assert media['mime_type'] == 'image/png'
    sent = base64.b64decode(media['data'])
    assert sent != data
    assert b'PRIVATE_ATTACHMENT_99' not in sent
    with Image.open(BytesIO(sent)) as image:
        assert kind_of(image) == 'normal' and image.info == {}


def test_replay_cannot_downgrade_previous_sensitive_observation(tmp_path, local_ocr, monkeypatch):
    data = synthetic_pdf(['normal', 'medical'])
    db, ai = Ledger(), FakeAI(_normal_receipt_result())
    pipeline(tmp_path, db=db, ai=ai).process_bytes(data, 'application/pdf', 'sticky')
    monkeypatch.setattr(extraction, '_run_image_ocr', lambda image: TEXTS['normal'])
    monkeypatch.setattr(extraction, '_run_image_ocr_tokens', lambda image, page:
        (extraction._StructuredOcrToken(TEXTS['normal'], page, 1, 1, 20, 5, 99, (1, 1, 1, 5)),))
    result = pipeline(tmp_path, db=db, ai=ai).process_bytes(data, 'application/pdf', 'sticky')
    assert result['pages'][1]['classification'] != 'normal'
    assert result['status'] == 'grouping_required'
    ai.analyze_receipt.assert_not_called()


def test_transport_gate_failure_holds_unit_without_medical_handoff(tmp_path, local_ocr):
    ai = FakeAI(_normal_receipt_result())
    ai.analyze_receipt.side_effect = ReceiptPrivacyBlocked()
    observer = Mock()
    result = pipeline(tmp_path, ai=ai, observer=observer).process_bytes(
        synthetic_pdf(['normal']), 'application/pdf', 'last-gate')
    assert result['units'][0]['status'] == 'privacy_pending'
    assert result['units'][0]['classification'] == 'sensitive_unknown'
    observer.observe.assert_not_called()


def test_corrupt_local_manifest_cannot_authorize_processing(tmp_path, local_ocr):
    data = synthetic_pdf(['normal'])
    p = pipeline(tmp_path)
    p.process_bytes(data, 'application/pdf', 'manifest')
    next((tmp_path / 'units').glob('*.json')).write_text('corrupt')
    p.ai.analyze_receipt.reset_mock()
    with pytest.raises(ValueError, match='pdf_unit_manifest_invalid'):
        p.process_bytes(data, 'application/pdf', 'manifest')
    p.ai.analyze_receipt.assert_not_called()


def test_cli_does_not_pass_pdf_to_ai_mock(tmp_path, monkeypatch, capsys):
    from app import cli
    image = tmp_path / 'sensitive.pdf'
    image.write_bytes(synthetic_pdf(['normal', 'medical']))
    ai = Mock()
    monkeypatch.setattr(cli, 'make', lambda: (object(), Mock(), ai))
    monkeypatch.setattr('sys.argv', ['kakeibo-ai', 'analyze', str(image)])
    cli.main()
    assert 'pdf_requires_page_units' in capsys.readouterr().out
    ai.analyze_receipt.assert_not_called()


def test_legacy_parent_import_cannot_create_a_new_single_page_receipt(tmp_path,local_ocr):
    db=Ledger();db.import_ids=lambda:{'receipt:parent-pdf'}
    p=pipeline(tmp_path,db=db)
    report=p.process_bytes(synthetic_pdf(['normal']),'application/pdf','parent-pdf')
    assert report['units'][0]['status']=='legacy_import_review_required'
    assert not report['all_units_terminal'] and not report['archive_allowed']
    assert not db.append_calls
    p.ai.analyze_receipt.assert_not_called()


def test_drive_cli_hides_sensitive_pdf_filename_and_ids(capsys):
    from app.cli import print_drive_receipt_results
    result = {'document_type': 'pdf_page_units', 'status': 'partially_processed',
              'source_file_id': 'PRIVATE_SOURCE', 'units': [
                  {'page_number': 2, 'classification': 'medical', 'status': 'medical_pending',
                   'unit_id': 'PRIVATE_UNIT'}]}
    print_drive_receipt_results([('PRIVATE_MEDICAL_NAME.pdf', result)])
    output = capsys.readouterr().out
    assert 'PRIVATE_' not in output
    assert 'medical_pending' in output


@pytest.mark.parametrize('failure', ['text', 'tokens'])
def test_failed_medical_observation_stays_sensitive_unknown(tmp_path, local_ocr, monkeypatch, failure):
    if failure == 'text':
        def fail(image):
            raise RuntimeError('PRIVATE_OCR_ERROR')
        monkeypatch.setattr(extraction, '_run_image_ocr', fail)
    else:
        monkeypatch.setattr(extraction, '_run_image_ocr_tokens', lambda image, page: ())
    p = pipeline(tmp_path)
    result = p.process_bytes(synthetic_pdf(['medical'], embedded=(0,)), 'application/pdf', 'incomplete-medical')
    assert result['units'][0]['classification'] == 'sensitive_unknown'
    assert result['units'][0]['status'] == 'privacy_pending'
    p.ai.analyze_receipt.assert_not_called()


@pytest.mark.parametrize('classification', ['medical', 'payroll'])
def test_gated_again_page_is_held_without_medical_observer(tmp_path, local_ocr, monkeypatch, classification):
    from test_receipt_pipeline import _medical_gate, _payroll_gate
    monkeypatch.setattr('app.receipt_pipeline.evaluate_receipt_privacy',
                        lambda *a, **k: _medical_gate() if classification == 'medical' else _payroll_gate())
    observer = Mock()
    p = pipeline(tmp_path, observer=observer)
    result = p.process_bytes(synthetic_pdf(['normal']), 'application/pdf', 'recheck')
    assert result['units'][0]['status'] == ('medical_pending' if classification == 'medical' else 'privacy_pending')
    observer.observe.assert_not_called()
    p.ai.analyze_receipt.assert_not_called()


def test_drive_never_moves_pdf_even_when_all_units_imported(tmp_path, local_ocr, monkeypatch):
    from app import drive_receipts
    data = synthetic_pdf(['normal'])
    service = Mock()
    service.files().list().execute.return_value = {'files': [{
        'id': 'pdf-drive-id', 'name': 'synthetic.pdf', 'mimeType': 'application/pdf',
        'parents': ['synthetic-inbox']}]}
    monkeypatch.setattr(drive_receipts, 'drive_service', lambda: service)
    monkeypatch.setattr(drive_receipts, 'download_drive_file', lambda *a: data)
    result = drive_receipts.process_inbox('synthetic-inbox', pipeline(tmp_path), 'synthetic-processed')
    assert result[0][1]['all_units_terminal']
    service.files().update.assert_not_called()


def test_raster_encoding_change_cannot_duplicate_same_pdf(tmp_path, local_ocr, monkeypatch):
    data = synthetic_pdf(['normal'])
    ai, db = FakeAI(_normal_receipt_result()), Ledger()
    first = pipeline(tmp_path, ai=ai, db=db).process_bytes(data, 'application/pdf', 'encoding')
    original_render = pdf._render_png
    def uncompressed(page, *args):
        with Image.open(BytesIO(original_render(page, *args))) as image:
            output = BytesIO()
            image.save(output, format='PNG', compress_level=0)
            return output.getvalue()
    monkeypatch.setattr(pdf, '_render_png', uncompressed)
    second = pipeline(tmp_path, ai=ai, db=db).process_bytes(data, 'application/pdf', 'encoding')
    assert first['units'][0]['page_hash'] != second['units'][0]['page_hash']
    assert first['units'][0]['unit_id'] == second['units'][0]['unit_id']
    assert second['units'][0]['status'] == 'imported'
    assert second['units'][0]['replayed'] is True
    assert ai.analyze_receipt.call_count == 1


def test_sensitive_evidence_from_completeness_pass_cannot_be_erased(tmp_path, local_ocr, monkeypatch):
    calls = 0
    def drifting_ocr(image):
        nonlocal calls
        calls += 1
        return TEXTS['medical'] if calls == 1 else TEXTS['normal']
    monkeypatch.setattr(extraction, '_run_image_ocr', drifting_ocr)
    p = pipeline(tmp_path)
    result = p.process_bytes(synthetic_pdf(['normal']), 'application/pdf', 'drifting')
    assert result['units'][0]['classification'] != 'normal'
    p.ai.analyze_receipt.assert_not_called()


@pytest.mark.parametrize('kinds', [('normal', 'normal'), ('normal', 'medical'),
    ('medical', 'normal'), ('normal', 'unknown'), ('normal', 'medical', 'normal')])
def test_grouping_required_replay_never_touches_accounting_or_ai(tmp_path, local_ocr, monkeypatch, kinds):
    # Fail on even read access, category loading or lazy AI initialization.
    db, ai, observer = Mock(), Mock(), Mock()
    factory = Mock(side_effect=AssertionError('Gemini initialization forbidden'))
    monkeypatch.setattr(pdf.SinglePageGrouping, 'units',
        Mock(side_effect=AssertionError('Page boundaries cannot establish accounting units')))
    p = ReceiptPipeline(db, ai, medical_review_observer=observer, gemini_factory=factory,
                        pdf_manifest_store=pdf.PdfUnitManifestStore(tmp_path / 'units'))
    data = synthetic_pdf(kinds)
    first = p.process_bytes(data, 'application/pdf', 'grouping-source')
    # Include replay across pipeline lifetimes with the persisted manifest.
    second = ReceiptPipeline(db, ai, medical_review_observer=observer, gemini_factory=factory,
        pdf_manifest_store=p.pdf_manifest_store).process_bytes(data, 'application/pdf', 'grouping-source')
    assert first == second
    assert first['status'] == 'grouping_required' and first['units'] == []
    assert not first['all_units_terminal'] and not first['archive_allowed']
    assert len(first['pages']) == len(kinds)
    assert db.mock_calls == ai.mock_calls == observer.mock_calls == factory.mock_calls == []


def test_two_page_continued_receipt_never_becomes_two_transactions(tmp_path, local_ocr, monkeypatch):
    monkeypatch.setitem(TEXTS, 'normal',
        'レシート 商品 合計 100円 現金 レシート番号 SAME_RECEIPT continued')
    data = synthetic_pdf(['normal', 'normal'], embedded=(0, 1))
    db, ai = Mock(), Mock()
    result = pipeline(tmp_path, ai=ai, db=db).process_bytes(data, 'application/pdf', 'continued-receipt')
    assert [p['classification'] for p in result['pages']] == ['normal', 'normal']
    assert result['status'] == 'grouping_required' and result['units'] == []
    assert db.mock_calls == ai.mock_calls == []


def test_grouping_required_cannot_be_promoted_by_terminal_page_markers():
    result = {'page_count': 2, 'status': 'grouping_required', 'units': [
        {'status': 'imported'}, {'status': 'confirmed'}], 'archive_allowed': True}
    pdf.update_document_status(result)
    assert result['status'] == 'grouping_required'
    assert not result['all_units_terminal'] and not should_archive_result(result)


def test_legacy_multi_page_manifest_never_authorizes_new_write(tmp_path, local_ocr):
    data = synthetic_pdf(['normal', 'medical'])
    observations = pdf.observe_pdf(data, 'legacy-source')
    store = pdf.PdfUnitManifestStore(tmp_path / 'units')
    # Model the original PR #91 partially_processed manifest and ledger IDs.
    old_units = [{**unit.metadata(), 'status': 'imported' if unit.observation.classification == 'normal'
                 else 'medical_pending'} for unit in pdf.SinglePageGrouping().units(observations.pages)]
    store.save({'source_file_id': 'legacy-source', 'source_content_hash': observations.source_content_hash,
                'status': 'partially_processed', 'units': old_units})
    db, ai, observer = Mock(), Mock(), Mock()
    result = ReceiptPipeline(db, ai, medical_review_observer=observer, pdf_manifest_store=store).process_bytes(
        data, 'application/pdf', 'legacy-source')
    assert result['status'] == 'grouping_required' and result['units'] == []
    assert [p['classification'] for p in result['pages']] == ['normal', 'medical']
    assert db.mock_calls == ai.mock_calls == observer.mock_calls == []


def test_single_page_later_privacy_restriction_survives_replay(tmp_path, local_ocr, monkeypatch):
    from test_receipt_pipeline import _medical_gate
    data = synthetic_pdf(['normal'])
    db, ai, observer = Mock(), Mock(), Mock()
    db.import_ids.return_value = set()
    with monkeypatch.context() as patch:
        patch.setattr('app.receipt_pipeline.evaluate_receipt_privacy', lambda *a, **k: _medical_gate())
        first = pipeline(tmp_path, ai=ai, db=db, observer=observer).process_bytes(
            data, 'application/pdf', 'later-medical')
    assert first['units'][0]['classification'] == 'medical'
    db.reset_mock()
    second = pipeline(tmp_path, ai=ai, db=db, observer=observer).process_bytes(
        data, 'application/pdf', 'later-medical')
    assert second['pages'][0]['classification'] != 'normal'
    assert db.mock_calls == ai.mock_calls == observer.mock_calls == []


def test_drive_grouping_required_never_moves_source(tmp_path, local_ocr, monkeypatch):
    from app import drive_receipts
    data = synthetic_pdf(['normal', 'normal'])
    service = Mock()
    service.files().list().execute.return_value = {'files': [{
        'id': 'grouping-drive-id', 'name': 'synthetic.pdf', 'mimeType': 'application/pdf',
        'parents': ['synthetic-inbox']}]}
    monkeypatch.setattr(drive_receipts, 'drive_service', lambda: service)
    monkeypatch.setattr(drive_receipts, 'download_drive_file', lambda *a: data)
    db, ai, observer = Mock(), Mock(), Mock()
    result = drive_receipts.process_inbox('synthetic-inbox',
        pipeline(tmp_path, ai=ai, db=db, observer=observer), 'synthetic-processed')
    assert result[0][1]['status'] == 'grouping_required'
    assert db.mock_calls == ai.mock_calls == observer.mock_calls == []
    service.files().update.assert_not_called()


def test_drive_cli_shows_grouping_page_observations_without_source_data(tmp_path, local_ocr, capsys):
    from app.cli import print_drive_receipt_results
    result = pipeline(tmp_path).process_bytes(synthetic_pdf(['normal', 'medical']),
        'application/pdf', 'PRIVATE_SOURCE')
    print_drive_receipt_results([('PRIVATE_MEDICAL_NAME.pdf', result)])
    output = capsys.readouterr().out
    assert 'PRIVATE_' not in output
    assert 'grouping_required' in output and 'normal' in output and 'medical' in output
    assert 'extraction_status' in output
