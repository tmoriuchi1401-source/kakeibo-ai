from hashlib import sha256
from types import SimpleNamespace
import pytest
from app.drive_run_state import StateError
from app.receipt_plan.drive import RealPageDrive,SOURCE,HASH,PROCESSED

def drive(parents,*,mime='application/pdf'):
    # Test only the location gate, independent of credentials and network.
    value=object.__new__(RealPageDrive)
    value.config={'inbox':'receipt-inbox','folder':'private-state'}
    value.request=lambda fid,**kw:{'id':fid,'etag':'"version"','mimeType':mime,'parents':[{'id':p} for p in parents]}
    return value

@pytest.mark.parametrize('parent',['receipt-inbox',PROCESSED])
def test_same_fixed_source_is_readable_in_existing_lifecycle(parent):
    assert drive([parent]).metadata(SOURCE)['parents']==[{'id':parent}]

@pytest.mark.parametrize('parents',[[],['other'],['receipt-inbox','other'],[PROCESSED,'other']])
def test_arbitrary_location_does_not_authorize_original(parents):
    with pytest.raises(StateError):drive(parents).metadata(SOURCE)

def test_processed_never_authorizes_a_state_file_or_pdf_copy():
    with pytest.raises(StateError):drive([PROCESSED]).metadata('another-source-copy')
    with pytest.raises(StateError):drive([PROCESSED],mime='application/json').metadata('authority-file')

def test_lifecycle_does_not_relax_content_identity():
    value=drive([PROCESSED]);value.read=lambda fid:(b'changed-original','"version"')
    with pytest.raises(StateError,match='real_page_source_changed'):value.source(SOURCE)
    with pytest.raises(StateError,match='real_page_source_forbidden'):value.source('copy')

def test_deployed_bundle_preserves_same_lifecycle_gate():
    import importlib.util
    from pathlib import Path
    root=Path(__file__).resolve().parents[1]
    spec=importlib.util.spec_from_file_location('bundle',root/'auth-session/compose.py')
    bundle=importlib.util.module_from_spec(spec);spec.loader.exec_module(bundle)
    patch=(root/'auth-session/existing-service.patch').read_text('utf8')
    assert "PROCESSED='"+PROCESSED+"'" in patch
    assert "locations=([{'id':folder}],[{'id':PROCESSED}]) if fid==SOURCE" in patch
    assert bundle.MANIFEST['changed_files']['services/human_general/real_page.py']!=bundle.MANIFEST['baseline_files']['services/human_general/real_page.py']
