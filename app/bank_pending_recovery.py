"""One manual, owner-only reconciliation of run 40501781979; no financial writer."""
from __future__ import annotations

import base64
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import zlib

from .drive_run_state import DurableState, DriveStateTransport, StateBinding, StateError, validate
from .production_ledger import ProductionLedger, validate_ledger

REPOSITORY = 'tmoriuchi1401-source/kakeibo-ai'
WORKFLOW = '.github/workflows/bank-pending-recovery.yml'
RECORD_SHA256 = '489b20341d0d858abfa2c2993a85794a837270f7b33cbd35a0fbc5a3a9af14a5'
MANIFEST_SHA256 = 'f3e1794e5f51c7f1ce5cf93a3b40e691468caa9c9fe988a3185837e685f1b942'
LABEL = b'kakeibo-bank-pending-recovery-v1'
RANGES = ["'取込データ'!A:L", "'収入明細'!A:J", "'支出明細'!A:M", "'_手入力受付'!A:F"]
PDF_FIELDS = 'id,mimeType,parents,trashed,version,md5Checksum,modifiedTime,appProperties,properties'


def encode(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(',', ':')).encode()


def sha(value):
    return hashlib.sha256(value).hexdigest()


def require(condition):
    if not condition:
        raise StateError('bank_pending_recovery_guard_failed')


def boundary(env, head):
    require(env.get('GITHUB_ACTIONS') == 'true'
            and env.get('GITHUB_REPOSITORY') == REPOSITORY
            and env.get('GITHUB_ACTOR') == env.get('GITHUB_TRIGGERING_ACTOR') == REPOSITORY.split('/')[0]
            and env.get('GITHUB_EVENT_NAME') == 'workflow_dispatch'
            and env.get('GITHUB_REF') == 'refs/heads/main'
            and env.get('GITHUB_WORKFLOW_REF') == REPOSITORY + '/' + WORKFLOW + '@refs/heads/main'
            and env.get('GITHUB_WORKFLOW_SHA') == env.get('GITHUB_SHA') == head
            and head == env.get('KAKEIBO_VALIDATED_MAIN_SHA') == env.get('EXPECTED_SHA')
            and env.get('GITHUB_RUN_ATTEMPT') == '1'
            and env.get('KAKEIBO_SCHEDULE_ENABLED') == 'false'
            and env.get('KAKEIBO_LEGACY_DISABLED') == 'true'
            and env.get('CATEGORY_BACKFILL_SHEET_RUNNER_ENABLED') == 'false'
            and env.get('RUNNER_DEBUG', '') != '1')


def decode_capsule(text, key):
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    raw = json.loads(base64.b64decode(text, validate=True))
    b64 = lambda name: base64.b64decode(raw[name], validate=True)
    aes = key.decrypt(b64('key'), padding.OAEP(mgf=padding.MGF1(hashes.SHA256()),
                                             algorithm=hashes.SHA256(), label=LABEL))
    compressed = AESGCM(aes).decrypt(b64('nonce'), b64('data'), LABEL)
    dec = zlib.decompressobj()
    plain = dec.decompress(compressed, 4 * 1024 * 1024)
    require(dec.eof and not dec.unused_data and not dec.unconsumed_tail)
    value = json.loads(plain)
    record_bytes = base64.b64decode(value['record'], validate=True)
    require(sha(record_bytes) == RECORD_SHA256 and sha(encode(value['manifest'])) == MANIFEST_SHA256)
    key.public_key().verify(base64.b64decode(value['signature'], validate=True),
                            encode(value['attestation']),
                            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.MAX_LENGTH),
                            hashes.SHA256())
    return json.loads(record_bytes), value['manifest'], value['attestation']


def controls(env, attestation):
    """Fresh local stop evidence plus live Cloud/main/run checks within the lock."""
    import requests
    age = datetime.now(timezone.utc).timestamp() - attestation['at_epoch']
    writers = attestation['writers']
    require(0 <= age <= 900 and attestation['main_sha'] == env['GITHUB_SHA']
            and attestation['record_sha256'] == RECORD_SHA256
            and writers['KnownWriterCount'] == 0 and writers['Tasks']
            and all(t['State'] == 'Disabled' for t in writers['Tasks']))
    session = requests.Session()
    session.headers.update(Authorization='Bearer ' + env['GH_TOKEN'], Accept='application/vnd.github+json')

    def get(path):
        response = session.get('https://api.github.com/repos/' + REPOSITORY + '/' + path, timeout=30)
        response.raise_for_status()
        return response.json()

    require(get('branches/main')['commit']['sha'] == env['GITHUB_SHA'])
    current = get('actions/runs/' + env['GITHUB_RUN_ID'])
    require(current['path'] == WORKFLOW and current['head_sha'] == env['GITHUB_SHA']
            and current['event'] == 'workflow_dispatch' and current['run_attempt'] == 1
            and current['status'] == 'in_progress'
            and current['actor']['login'] == current['triggering_actor']['login'] == REPOSITORY.split('/')[0])
    failed = get('actions/runs/40501781979')
    require(failed['status'] == 'completed' and failed['conclusion'] == 'failure'
            and failed['run_attempt'] == 1
            and failed['head_sha'] == '89e02e0bdacf8c683846ec6cff351cb623b7bf65')
    for status in ('in_progress', 'queued', 'waiting', 'pending', 'requested'):
        runs = get('actions/runs?status=' + status + '&per_page=100')
        require(runs['total_count'] <= 100
                and all(str(r['id']) == env['GITHUB_RUN_ID'] for r in runs['workflow_runs']))
    require(requests.head('https://hga-auth-synthetic-efekwfyq3q-an.a.run.app',
                          timeout=30, allow_redirects=False).status_code == 503)


def verify_sqlite(bank, record):
    require(bank['files'] == record['sqlite_and_all_native_files_required_unchanged'])
    with closing(sqlite3.connect(':memory:')) as db:
        db.deserialize(base64.b64decode(bank['files']['bank-recurring.sqlite3']['data'], validate=True))
        db.execute('PRAGMA trusted_schema=OFF')
        db.execute('PRAGMA query_only=ON')
        require(db.execute('PRAGMA integrity_check').fetchall() == [('ok',)])
        require([list(r) for r in db.execute('SELECT * FROM recurring_checkpoint')]
                == record['checkpoint_before_and_required_after'])
        require(db.execute("SELECT count(*) FROM recurring_runs WHERE status IN "
                           "('fixed_backfill_pending','fixed_backfill_verified',"
                           "'income_append_pending','income_append_verified')").fetchone()[0] == 0)


def observe(drive, sheets, manifest, sid):
    response = sheets.spreadsheets().values().batchGet(
        spreadsheetId=sid, ranges=RANGES, valueRenderOption='UNFORMATTED_VALUE').execute(num_retries=0)
    require(sha(encode(response)) == manifest['raw_financial_sha256'])
    tables = [x.get('values', []) for x in response['valueRanges'][:3]]
    selected = set(manifest['selected100'])
    derived = {prefix + sha(i.encode())[:24] for i in selected for prefix in ('I-', 'M-')}
    require(len(selected) == 100 and len(manifest['protected185']) == 185
            and not any(r and (r[0] in selected | derived
                               or len(r) > 6 and r[6] in selected
                               or len(r) > 10 and r[10] in selected) for table in tables for r in table))
    require(set(manifest['protected185']) <= {r[0] for r in tables[0] if r})
    for pdf in manifest['pdfs']:
        fid = pdf['metadata']['id']
        metadata = drive.files().get(fileId=fid, fields=PDF_FIELDS,
                                     supportsAllDrives=True).execute(num_retries=0)
        content = drive.files().get_media(fileId=fid, supportsAllDrives=True).execute(num_retries=0)
        require(metadata == pdf['metadata'] and sha(content) == pdf['sha256'])


class ReleaseOnlyTransport:
    """The only write capability accepts one exact, approved post-release payload."""
    def __init__(self, transport, expected):
        self.transport, self.expected, self.attempted = transport, expected, False

    def read(self):
        return self.transport.read()

    def write(self, payload):
        require(not self.attempted and sha(payload) == self.expected)
        self.attempted = True
        self.transport.write(payload)


def recover(record, transports, bindings, guard, observation, report):
    require(record['run_id'] == 40501781979 and record['run_attempt'] == 1
            and record['decision'] == 'GO_FOR_BOUND_PENDING_STATE_RELEASE_ONLY'
            and not record['financial_apply_authorized_by_this_record']
            and not record['unresolved_material_write_results']
            and not record['remote_sqlite_intents_requiring_resolution'])
    old = {}
    for source, generation in (('bank_pdf_drive', 43), ('production_run', 1287)):
        entry = record['states'][source]
        binding = bindings[source]
        require((binding.spreadsheet_id, binding.folder_id, binding.file_id)
                == (entry['spreadsheet_id'], entry['folder_id'], entry['file_id']))
        raw = transports[source].read()
        require(sha(raw) == entry['observed_raw_sha256'])
        value = (validate if source == 'bank_pdf_drive' else validate_ledger)(raw, binding)
        item = value if source == 'bank_pdf_drive' else value['sources']['bank']
        require(value['generation'] == entry['generation_before'] == generation
                and item['phase'] == entry['phase_before'] == 'pending'
                and item['attempt'] == entry['attempt'])
        old[source] = value
    verify_sqlite(old['bank_pdf_drive'], record)
    guard()
    observation()  # Both originals and ALL financial columns verified before either write.
    require(all(sha(transports[s].read()) == record['states'][s]['observed_raw_sha256']
                for s in old))
    for source in ('bank_pdf_drive', 'production_run'):
        guard()
        entry = record['states'][source]
        transport, binding = transports[source], bindings[source]
        wrapped = ReleaseOnlyTransport(transport, entry['expected_after_raw_sha256'])
        if source == 'bank_pdf_drive':
            store = DurableState(wrapped, binding)
        else:
            # A separate API read, not the earlier in-memory state.
            bank = validate(transports['bank_pdf_drive'].read(), bindings['bank_pdf_drive'])
            require(bank['generation'] == 44 and bank['phase'] == 'ready' and bank['attempt'] == '')
            verify_sqlite(bank, record)
            observation()
            store = ProductionLedger(wrapped, binding)
        report[source] = 'attempted_unknown'
        report['stage'] = source + '_release'
        kwargs = dict(observed_digest=entry['observed_raw_sha256'], evidence_reference=RECORD_SHA256)
        if source == 'bank_pdf_drive':
            store.release_after_reconciliation(**kwargs)
        else:
            store.release_after_reconciliation('bank', **kwargs)
        report['stage'] = source + '_readback'
        raw = transport.read()  # Independent API read after the method's own read-back.
        require(sha(raw) == entry['expected_after_raw_sha256'])
        value = (validate if source == 'bank_pdf_drive' else validate_ledger)(raw, binding)
        require(value['generation'] == entry['generation_required_after'])
        if source == 'bank_pdf_drive':
            require(value['generation'] == 44 and value['phase'] == 'ready' and value['attempt'] == '')
            verify_sqlite(value, record)
            require(sha(transports['production_run'].read()) == record['states']['production_run']['observed_raw_sha256'])
        else:
            expected = json.loads(json.dumps(old[source]))
            expected['generation'] = 1288
            expected['sources']['bank'].update(phase='ready', attempt='', error='')
            require(value == expected)
        report[source] = 'ready_verified'
        observation()
    require(sha(transports['bank_pdf_drive'].read()) == record['states']['bank_pdf_drive']['expected_after_raw_sha256'])
    guard()
    report.update(success=True, financial_unchanged=True, checkpoint_sqlite_unchanged=True,
                  bank_generation=44, parent_generation=1288)


def main():
    report = dict(success=False, bank_pdf_drive='not_attempted', production_run='not_attempted',
                  financial_write_calls=0, automatic_retry=False, rollback=False)
    try:
        report['stage'] = 'execution_boundary'
        env = dict(os.environ)
        head = subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip()
        boundary(env, head)
        from .settings import service_account_source
        from .private_state_bindings import decode_environment
        from .google_clients import credentials, read_only_drive_service, read_only_sheets_service
        from cryptography.hazmat.primitives import serialization
        from googleapiclient.discovery import build
        path, info = service_account_source()
        info = info or json.loads(Path(path).read_bytes())
        key = serialization.load_pem_private_key(info['private_key'].encode(), password=None)
        report['stage'] = 'private_evidence'
        record, manifest, attestation = decode_capsule(env['RECOVERY_CAPSULE'], key)
        env, _ = decode_environment(env)
        bindings = {source: StateBinding(source, env['SPREADSHEET_ID'], env['KAKEIBO_STATE_FOLDER_ID'], env[name])
                    for source, name in (('bank_pdf_drive', 'BANK_STATE_FILE_ID'),
                                         ('production_run', 'KAKEIBO_RUN_LEDGER_FILE_ID'))}
        readonly, sheets = read_only_drive_service(), read_only_sheets_service()
        drive = build('drive', 'v3', credentials=credentials(['https://www.googleapis.com/auth/drive']),
                      cache_discovery=False)
        transports = {source: DriveStateTransport(drive, binding) for source, binding in bindings.items()}
        def guard():
            report['stage'] = 'stop_and_concurrency_checks'
            controls(env, attestation)
        def observation():
            report['stage'] = 'financial_pdf_observation'
            observe(readonly, sheets, manifest, env['SPREADSHEET_ID'])
        recover(record, transports, bindings, guard, observation, report)
        report['stage'] = 'complete'
        report.update(run_id=int(env['GITHUB_RUN_ID']), main_sha=head, common_lock='kakeibo-production')
    except Exception:
        report.update(success=False, error='bank_pending_recovery_stopped',
                      partial_or_unknown=report['bank_pdf_drive'] != 'not_attempted')
    rendered = json.dumps(report, sort_keys=True)
    print(rendered)  # Fixed fields only, never exception messages/payloads/bindings.
    if os.environ.get('GITHUB_STEP_SUMMARY'):
        with open(os.environ['GITHUB_STEP_SUMMARY'], 'a', encoding='utf-8') as summary:
            summary.write('```json\n' + rendered + '\n```\n')
    if not report['success']:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
