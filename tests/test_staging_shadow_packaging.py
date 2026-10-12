"""Archive-free checks. Real HTTP/build proof belongs to isolated Docker CI."""
import ast
from pathlib import Path
import re
import subprocess
import sys

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


def cloud_env(**extra):
    return dict(UNIFIED_STAGING_ENABLED='true',UNIFIED_STAGING_MODE='cloud-run',
        HGA_ORIGIN='https://synthetic-cloud-run.example.test',PORT='8787',**extra)


def test_binding_is_loopback_by_default_and_cloud_is_explicit():
    config=Config.read({'UNIFIED_STAGING_ENABLED':'true'})
    assert (config.mode,config.bind_host,config.port)==('ci','127.0.0.1',8080)
    config=Config.read(cloud_env(K_SERVICE='synthetic-service',K_REVISION='synthetic-revision'))
    assert (config.mode,config.bind_host,config.port,config.ttl)==('cloud-run','0.0.0.0',8787,600)


@pytest.mark.parametrize('mode',['','cloud','production','CI'])
def test_unknown_mode_fails_closed(mode):
    with pytest.raises(ValueError):Config.read({'UNIFIED_STAGING_ENABLED':'true','UNIFIED_STAGING_MODE':mode})


@pytest.mark.parametrize('name',['K_SERVICE','K_REVISION','K_CONFIGURATION'])
def test_cloud_marker_cannot_implicitly_enable_wildcard(name):
    with pytest.raises(ValueError):Config.read({'UNIFIED_STAGING_ENABLED':'true',name:'synthetic'})


@pytest.mark.parametrize('missing',['HGA_ORIGIN','PORT'])
def test_cloud_mode_requires_explicit_origin_and_port(missing):
    env=cloud_env();del env[missing]
    with pytest.raises(ValueError):Config.read(env)


@pytest.mark.parametrize('port',['','0','80','65536','8080x',' 8080','+8080','８０８０'])
def test_invalid_cloud_port_is_rejected_before_bind(port):
    env=cloud_env();env['PORT']=port
    with pytest.raises(ValueError):Config.read(env)


def test_cloud_mode_rejects_placeholder_origin_and_connection_settings():
    env=cloud_env();env['HGA_ORIGIN']='https://staging.invalid'
    with pytest.raises(ValueError):Config.read(env)
    for name in ('GOOGLE_APPLICATION_CREDENTIALS','HGA_REAL_PAGE_CONFIG_PATH','HTTPS_PROXY','UNIFIED_STAGING_UNSAFE'):
        with pytest.raises(ValueError):Config.read(cloud_env(**{name:''}))


def test_wildcard_bind_resolution_does_not_grant_outbound_or_metadata_access():
    # Execute the actual guard in an isolated process, preserving pytest's sockets.
    tree=ast.parse((ROOT/'staging_shadow/runtime.py').read_text(encoding='utf-8'))
    function=next(node for node in tree.body if isinstance(node,ast.FunctionDef) and node.name=='install_network_guard')
    code='import socket,ipaddress\n'+ast.unparse(function)+'''\n
install_network_guard()
assert socket.getaddrinfo('0.0.0.0',8787)[0][4][0]=='0.0.0.0'
for host in ('0.0.0.0','169.254.169.254','203.0.113.1'):
    for method in ('connect','connect_ex'):
        with socket.socket() as s:
            try:getattr(s,method)((host,443))
            except OSError as e:assert str(e)=='staging_egress_denied'
            else:raise AssertionError('outbound allowed')
try:socket.getaddrinfo('external.example.test',443)
except OSError as e:assert str(e)=='staging_dns_denied'
else:raise AssertionError('external DNS allowed')
'''
    subprocess.run([sys.executable,'-c',code],check=True,capture_output=True,text=True,timeout=10)
