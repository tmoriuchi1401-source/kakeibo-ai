from copy import deepcopy
from dataclasses import asdict, replace
from hashlib import sha256
from types import SimpleNamespace

import pytest

from app.drive_run_state import StateError
from app.iphone_source_observation import observe, revalidate, verify_saved
from app.pdf_page_identity import page_identity
from app.unified_receipt_review import digest


def fixture(purpose='ACCOUNTING_POST'):
    raw = b'synthetic-original-only'; hashed = sha256(raw).hexdigest()
    page = page_identity(hashed, 1, 1)
    sheet = dict(source_id='synthetic-file', sha256=hashed, source_version='drive-revision-ABC', version_scheme='drive_revision_id')
    native = dict(source_file_id=sheet['source_id'], source_content_hash=hashed, source_version='native-42',
        page_numbers=[1], page_identities=[page], receipt_unit_id='unit-1' if purpose == 'ACCOUNTING_POST' else '',
        receipt_index=1 if purpose == 'ACCOUNTING_POST' else 0)
    frame = dict(source_id=sheet['source_id'], sha256=hashed, drive_revision_id=sheet['source_version'],
        drive_numeric_version='19', native_source_version=native['source_version'], source_kind='pdf',
        pages=[dict(number=1, count=1, identity=page)], purpose=purpose,
        receipt_unit_id=native['receipt_unit_id'], receipt_index=native['receipt_index'])
    reader = SimpleNamespace(read_frame=lambda:deepcopy(frame), read_revision_bytes=lambda *a:raw)
    kw = dict(purpose=purpose, input_snapshot=digest(['input']), clock=lambda:100)
    return reader, sheet, native, frame, kw


@pytest.mark.parametrize('purpose', ['ACCOUNTING_POST', 'STRUCTURE_REVIEW'])
@pytest.mark.parametrize('numeric', ['19', None])
def test_independent_versions_bytes_page_and_stage_contract(purpose, numeric):
    reader, s, n, frame, kw = fixture(purpose); frame['drive_numeric_version'] = numeric
    saved = observe(reader, s, n, **kw)
    assert saved.drive_revision_id != saved.native_source_version
    assert saved.drive_numeric_version == numeric
    for stage in ['prepare', 'acceptance', 'execution']:
        assert revalidate(saved, reader, s, n, stage=stage, **kw) == saved
    verify_saved(saved, s, n, kw['purpose'], kw['input_snapshot'])


@pytest.mark.parametrize('attack', ['bytes', 'revision', 'numeric', 'native', 'file', 'hash',
    'page', 'unit', 'index', 'missing', 'empty', 'numeric_type', 'purpose', 'mid_observation'])
def test_changed_or_missing_coobservation_is_unknown(attack):
    reader, s, n, frame, kw = fixture()
    if attack == 'bytes': reader.read_revision_bytes = lambda *a:b'updated'
    elif attack == 'revision': frame['drive_revision_id'] = 'other'
    elif attack == 'numeric': frame['drive_numeric_version'] = 'bad'
    elif attack == 'native': frame['native_source_version'] = 'other'
    elif attack == 'file': frame['source_id'] = 'other'
    elif attack == 'hash': frame['sha256'] = 'f'*64
    elif attack == 'page': frame['pages'][0]['identity'] = 'f'*64
    elif attack == 'unit': frame['receipt_unit_id'] = 'other'
    elif attack == 'index': frame['receipt_index'] = 2
    elif attack == 'missing': frame.pop('native_source_version')
    elif attack == 'empty': frame['native_source_version'] = ''
    elif attack == 'numeric_type': frame['drive_numeric_version'] = 19
    elif attack == 'purpose': frame['purpose'] = 'METADATA_REVIEW'
    else:
        def bytes_read(*a):
            frame['drive_numeric_version'] = '20'
            return b'synthetic-original-only'
        reader.read_revision_bytes = bytes_read
    with pytest.raises(StateError, match='UNKNOWN'): observe(reader, s, n, **kw)


@pytest.mark.parametrize('changed', ['revision_same_hash', 'numeric_same_hash', 'native_same_hash', 'input', 'page_order'])
@pytest.mark.parametrize('stage', ['prepare', 'acceptance', 'execution'])
def test_revalidation_rejects_version_only_updates_and_stale_input(changed, stage):
    reader, s, n, frame, kw = fixture(); saved = observe(reader, s, n, **kw)
    if changed == 'revision_same_hash': frame['drive_revision_id'] = s['source_version'] = 'new-revision'
    elif changed == 'numeric_same_hash': frame['drive_numeric_version'] = '20'
    elif changed == 'native_same_hash': frame['native_source_version'] = n['source_version'] = 'native-43'
    elif changed == 'input': kw['input_snapshot'] = digest(['changed'])
    else: frame['pages'][0]['number'] = 2
    with pytest.raises(StateError, match='UNKNOWN'): revalidate(saved, reader, s, n, stage=stage, **kw)


def test_saved_observation_digest_tampering_rejected_without_reader():
    reader, s, n, frame, kw = fixture(); saved = observe(reader, s, n, **kw)
    with pytest.raises(StateError):
        verify_saved(replace(saved, drive_numeric_version='200'), s, n, kw['purpose'], kw['input_snapshot'])


def test_image_uses_existing_native_image_identity_not_pdf_identity():
    from app.page_receipt_model import SourceRef, stable_page
    reader, s, n, frame, kw = fixture()
    frame['source_kind'] = 'image'
    page = stable_page(SourceRef(source_file_id=s['source_id'], source_content_hash=s['sha256'],
                                page_count=1, source_kind='image'), 1)
    frame['pages'][0]['identity'] = page; n['page_identities'] = [page]
    assert observe(reader, s, n, **kw).pages[0]['identity'] == page


def test_reader_reusing_mutable_frame_cannot_hide_mid_observation_change():
    reader, s, n, frame, kw = fixture()
    reader.read_frame = lambda:frame
    def changed(*args):
        frame['drive_numeric_version'] = '21'
        return b'synthetic-original-only'
    reader.read_revision_bytes = changed
    with pytest.raises(StateError, match='UNKNOWN'): observe(reader, s, n, **kw)
