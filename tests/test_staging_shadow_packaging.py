"""Archive-free checks. Real HTTP/build proof belongs to isolated Docker CI."""
import ast
from pathlib import Path
import re

from staging_shadow.prepare_context import derive_storage
from staging_shadow.memory_cas import Database, LastUpdateOption, FailedPrecondition
from staging_shadow.config import Config
import pytest

ROOT=Path(__file__).resolve().parents[1]


def test_staging_explicit_config_without_credentials():
    assert Config.read({'UNIFIED_STAGING_ENABLED':'true'}).ttl==600
    assert Config.read({'UNIFIED_STAGING_ENABLED':'true'}).origin=='https://staging.invalid'
    with pytest.raises(ValueError):Config.read({})


@pytest.mark.parametrize('name',['HGA_CONFIG_PATH','HGA_OWNER_PATH','HGA_SESSION_KEY_PATH',
    'HGA_REAL_PAGE_CONFIG_PATH','HGA_DRIVE_CREDENTIAL_PATH','HGA_DATABASE',
    'GOOGLE_APPLICATION_CREDENTIALS','GOOGLE_CLOUD_PROJECT','SPREADSHEET_ID',
    'GEMINI_API_KEY','HTTPS_PROXY','BANK_STATE_FILE_ID','ACCOUNTING_POST_ENABLED',
    'KAKEIBO_PRODUCTION_ENABLED','UNIFIED_RECEIPT_CASE_ID'])
def test_production_configuration_rejected_even_empty(name):
    with pytest.raises(ValueError):Config.read({'UNIFIED_STAGING_ENABLED':'true',name:''})


@pytest.mark.parametrize('value',['true','1','yes'])
def test_write_cannot_be_enabled(value):
    with pytest.raises(ValueError):Config.read({'UNIFIED_STAGING_ENABLED':'true','UNIFIED_RECEIPT_WRITE_ENABLED':value})


@pytest.mark.parametrize('origin',['http://staging.invalid','https://staging.invalid/','https://user:pass@staging.invalid',
    'https://staging.invalid?token=synthetic','https://staging.invalid#synthetic'])
def test_origin_must_be_exact_https_without_userinfo_path_query_fragment(origin):
    with pytest.raises(ValueError):Config.read({'UNIFIED_STAGING_ENABLED':'true','HGA_ORIGIN':origin})


def test_staging_dependencies_are_all_version_and_hash_fixed_without_cloud_sdks():
    lines=[l for l in (ROOT/'staging_shadow/requirements.lock').read_text().splitlines() if not l.startswith('#')]
    assert len(lines)==25
    assert all(re.fullmatch(r'[a-z0-9-]+==[a-zA-Z0-9.]+ --hash=sha256:[0-9a-f]{64}',l) for l in lines)
    assert not any('firestore' in l or 'google-cloud' in l or 'google-api-' in l for l in lines)
    text=(ROOT/'staging_shadow/Dockerfile').read_text()
    assert re.search(r'^FROM python:[^\n]+@sha256:[0-9a-f]{64}$',text,re.M)
    assert '--no-index' in text and '--require-hashes' in text
    assert 'USER 10001:10001' in text


def test_storage_derivation_preserves_all_non_import_native_logic():
    sample='from google.api_core.exceptions import FailedPrecondition, NotFound\nfrom google.cloud.firestore_v1 import LastUpdateOption\ndef same(x):\n    return x\n'
    result=derive_storage(sample)
    assert 'staging_shadow.memory_cas' in result and 'google.cloud' not in result
    assert ast.dump(ast.parse(result).body[-1])==ast.dump(ast.parse(sample).body[-1])


def test_memory_CAS_stale_write_rejected_and_authority_collection_absent():
    db=Database();ref=db.collection('pages').document('synthetic')
    ref.create({'value':'synthetic'},retry=None,timeout=10)
    before=ref.get(retry=None,timeout=10)
    ref.update({'value':'after'},option=LastUpdateOption(before.update_time),retry=None,timeout=10)
    with pytest.raises(FailedPrecondition):ref.update({'value':'bad'},option=LastUpdateOption(before.update_time),retry=None,timeout=10)
    assert ref.get(retry=None,timeout=10).get('value')=='after' and db.writes==2
    with pytest.raises(ValueError):db.collection('authorities')


def test_staging_build_has_no_auth_push_or_published_port():
    text=(ROOT/'staging_shadow/ci_build.py').read_text()
    assert "'--network=none'" in text and "'--read-only'" in text
    assert "'--require-hashes'" in text and "'--platform=linux/amd64'" in text
    assert not any(s in text for s in ('gcloud','--publish','docker login','docker push','build-push-action'))
