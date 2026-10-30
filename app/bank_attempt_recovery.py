"""Release ONLY run40961220838's bank53/parent1300; never run accounting.

Existing CAS is not atomic. Strict mode requires exclusion of all writers;
the target-only nonexclusive mode requires signed acceptance of that race risk.
A fresh GET is an integrity check, not a lock. Interrupted releases are inspection-only
on re-entry; there is no automatic resume, rollback, or source retry.
"""
from __future__ import annotations

import base64
from collections import Counter
from contextlib import closing, contextmanager
from dataclasses import asdict
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import tempfile
import zlib

from .bank_pending_recovery import encode, sha, require, ReleaseOnlyTransport, LABEL, REPOSITORY
from .drive_run_state import DurableState, DriveStateTransport, StateBinding, envelope, validate
from .production_ledger import ProductionLedger, validate_ledger, _encode

WORKFLOW = '.github/workflows/bank-attempt-40961220838-recovery.yml'
FAILED_RUN = 40961220838
FAILED_HEAD = '6f114abac3d8c3a537febeeea9fdfc07ff9716d9'
RECORD_SHA256 = '21046b4101e6f2948ec7282b4998225af6753ab267348384a907495dd5325d87'
NONEXCLUSIVE_RISK = 'non_atomic_CAS_external_writer_race_accepted'
SOURCES = ('bank_pdf_drive', 'production_run')
TABLES = ('取込データ', '収入明細', '支出明細', '_手入力受付')
RANGES = ("'取込データ'!A:L", "'収入明細'!A:J", "'支出明細'!A:M", "'_手入力受付'!A:F")
PDF_FIELDS = 'id,name,mimeType,createdTime,modifiedTime,parents,appProperties,properties,version,md5Checksum,size'
CHECKS = frozenset({'ENV_SHA', 'SA_CREDENTIALS', 'CAPSULE', 'FIXED_EVIDENCE', 'DRIVE_BINDINGS',
    'GITHUB_API', 'GITHUB_VARIABLES', 'CONTROL_VARIABLES', 'GITHUB_CONTROLS', 'RUNS', 'CLOUD_503',
    'FINANCIAL_READ', 'FINANCIAL_MATCH', 'PDF_MATCH', 'SQLITE_CHECKPOINT', 'STATE_MATCH'})
CONTROL_VARIABLES = ('KAKEIBO_VALIDATED_MAIN_SHA', 'KAKEIBO_SCHEDULE_ENABLED',
    'KAKEIBO_LEGACY_DISABLED', 'CATEGORY_BACKFILL_SHEET_RUNNER_ENABLED')


@contextmanager
def checking(name):
    """Only allowlisted labels/statuses; never stringify an exception or body."""
    require(name in CHECKS)
    print('RECOVERY_CHECK ' + name + ' BEGIN', flush=True)
    try:
        yield
    except Exception as error:
        from requests.exceptions import HTTPError
        code = 'FAIL'
        if isinstance(error, HTTPError) and error.response is not None:
            status = error.response.status_code
            if status in {401, 403, 404, 429, 500, 502, 503, 504}:
                code = 'HTTP_' + str(status)
        print('RECOVERY_CHECK ' + name + ' ' + code, flush=True)
        raise
    print('RECOVERY_CHECK ' + name + ' OK', flush=True)


@checking('FIXED_EVIDENCE')
def evidence(record):
    require(sha(encode(record)) == RECORD_SHA256
        and record['kind'] == 'bank40961220838_state_release_only'
        and record['run_id'] == FAILED_RUN and record['run_attempt'] == 1
        and record['failed_main_sha'] == FAILED_HEAD
        and record['financial_apply_authorized'] is False
        and record['financial_requests'] == record['pdf_requests'] == 'UNKNOWN')
    ids = set(record['transaction_ids'])
    require(len(ids) == len(record['transaction_ids']) == 497
        and len(set(record['duplicate_ids'])) == 50
        and len(set(record['new4_ids'])) == 4
        and len(set(record['unimported6_ids'])) == 6
        and set(record['duplicate_ids']) <= ids and set(record['new4_ids']) <= ids
        and set(record['unimported6_ids']) <= ids and len(record['pdfs']) == 5
        and record['history_count'] == 32
        and record['checkpoint'][0][1] == '2026-10-15T09:08:44+09:00')
    for source, before, after in [('bank_pdf_drive', 53, 54), ('production_run', 1300, 1301)]:
        entry = record['states'][source]
        require(entry['source'] == source and entry['generation_before'] == before
            and entry['generation_after'] == after and re.fullmatch('[0-9a-f]{32}', entry['attempt']))
    require(record['states'][SOURCES[0]]['spreadsheet_id'] == record['states'][SOURCES[1]]['spreadsheet_id']
        and record['states'][SOURCES[0]]['folder_id'] == record['states'][SOURCES[1]]['folder_id']
        and record['states'][SOURCES[0]]['file_id'] != record['states'][SOURCES[1]]['file_id'])


@checking('SQLITE_CHECKPOINT')
def verify_sqlite(bank, record):
    native = base64.b64decode(bank['files']['bank-recurring.sqlite3']['data'], validate=True)
    require(sha(native) == record['sqlite_sha256'])
    with closing(sqlite3.connect(':memory:')) as db:
        db.deserialize(native)
        db.execute('PRAGMA trusted_schema=OFF')
        db.execute('PRAGMA query_only=ON')
        require(db.execute('PRAGMA integrity_check').fetchall() == [('ok',)])
        require([list(row) for row in db.execute('SELECT * FROM recurring_checkpoint')] == record['checkpoint'])
        rows = db.execute('SELECT summary_json FROM recurring_runs ORDER BY rowid').fetchall()
        require(len(rows) == record['history_count'] == 32)
    pending = set()
    for (raw,) in rows:
        event = json.loads(raw)
        status = event.get('status', '')
        if status.startswith('income_append_'):
            key = ('income', event['income_attempt'])
        elif status.startswith('fixed_backfill_'):
            key = ('fixed', event['fixed_plan'], event['fixed_batch'])
        else:
            continue
        if status.endswith('_pending'):
            pending.add(key)
        elif status.endswith('_verified'):
            pending.discard(key)
    require(not pending)


@checking('STATE_MATCH')
def inspect(record, transports, bindings):
    raw, values, done = {}, {}, {}
    for source in SOURCES:
        entry, binding = record['states'][source], bindings[source]
        require(binding.reference == entry['binding_reference']
            and (binding.spreadsheet_id, binding.folder_id, binding.file_id) ==
                (entry['spreadsheet_id'], entry['folder_id'], entry['file_id']))
        raw[source] = transports[source].read()
        digest = sha(raw[source])
        require(digest in {entry['before_sha256'], entry['after_sha256']})
        done[source] = digest == entry['after_sha256']
        value = (validate if source == SOURCES[0] else validate_ledger)(raw[source], binding)
        item = value if source == SOURCES[0] else value['sources']['bank']
        require(value['generation'] == entry['generation_after' if done[source] else 'generation_before']
            and item['phase'] == ('ready' if done[source] else 'pending')
            and item['attempt'] == ('' if done[source] else entry['attempt']))
        if source == SOURCES[1]:
            require(item['error'] == ('' if done[source] else 'source_execution_failed'))
            require(all(v['phase'] == 'ready' and not v['attempt'] and not v['error']
                        for k, v in value['sources'].items() if k != 'bank'))
        values[source] = value
    require(not done[SOURCES[1]] or done[SOURCES[0]])
    verify_sqlite(values[SOURCES[0]], record)
    return raw, values, done


def recover(record, transports, bindings, guard, observation, independent_read, *, dry_run=True, report=None):
    """One invocation: all preconditions -> bank -> GET -> parent -> GET.

    Callbacks are fixed by main, never accepted from workflow inputs. Exceptions
    propagate immediately. All counters are attempts, not success assertions.
    """
    report = report if report is not None else {}
    report.update(stage='preconditions', bank_write_attempts=0, parent_write_attempts=0,
                  financial_write_calls=0, pdf_changes=0, automatic_retry=False, rollback=False)
    require(type(dry_run) is bool)
    evidence(record)
    guard()
    observation()
    old, values, done = inspect(record, transports, bindings)
    if dry_run:
        report.update(success=True, outcome='dry_run', bank_ready=done[SOURCES[0]], parent_ready=done[SOURCES[1]])
        return report
    if all(done.values()):
        report.update(success=True, outcome='already_released_no_write')
        return report
    # Do not silently continue the parent after a prior unknown bank update.
    require(not any(done.values()))
    for source in SOURCES:
        report['stage'] = source + '_before_write'
        guard()  # Recheck the selected policy and competing runs at every boundary.
        observation()
        require(all(transports[s].read() == old[s] for s in SOURCES))
        entry = record['states'][source]
        wrapped = ReleaseOnlyTransport(transports[source], entry['after_sha256'])
        if source == SOURCES[0]:
            proposed = envelope(bindings[source], values[source]['files'], generation=54)
            store = DurableState(wrapped, bindings[source])
            kwargs = {}
        else:
            expected = json.loads(old[source])
            expected['generation'] = 1301
            expected['sources']['bank'].update(phase='ready', attempt='', error='')
            proposed = _encode(expected)
            # Native bank must be ready in a NEW GET immediately before parent.
            require(independent_read(SOURCES[0]) == old[SOURCES[0]])
            store = ProductionLedger(wrapped, bindings[source])
            kwargs = {'source': 'bank'}
        require(sha(proposed) == entry['after_sha256'])
        report['bank_write_attempts' if source == SOURCES[0] else 'parent_write_attempts'] += 1
        store.release_after_reconciliation(observed_digest=entry['before_sha256'], evidence_reference=RECORD_SHA256, **kwargs)
        report['stage'] = source + '_independent_get'
        # Uses a separately constructed read-only transport, never store cache.
        actual = independent_read(source)
        require(actual == proposed)
        other = SOURCES[1] if source == SOURCES[0] else SOURCES[0]
        require(independent_read(other) == old[other])
        old[source] = actual
        verify_sqlite(validate(old[SOURCES[0]], bindings[SOURCES[0]]), record)
        guard()
        observation()
    report.update(stage='complete', success=True, outcome='released_stop', bank_generation=54, parent_generation=1301)
    return report


@checking('FINANCIAL_MATCH')
def verify_financial(raw, record):
    require(sha(encode(raw)) == record['raw_financial_sha256'])
    ids = set(record['transaction_ids'])
    imports = Counter(str(row[0]) for row in raw[TABLES[0]][1:] if row and row[0])
    present = ids & imports.keys()
    require(len(present) == 376 and len(ids - present) == 121 and all(imports[i] == 1 for i in present)
        and len(set(record['duplicate_ids']) & present) == 48
        and not set(record['new4_ids']) & present
        and not set(record['unimported6_ids']) & present)
    for identity in record['unimported6_ids']:
        derived = {'I-' + sha(identity.encode())[:24], 'M-' + sha(identity.encode())[:24]}
        require(not any(row and (str(row[0]) in derived or identity in map(str, row))
                        for sheet in TABLES[1:3] for row in raw[sheet][1:]))


def observe(record, drive, sheets):
    """Read-only clients only. Hashes cover ALL cells, including other IDs."""
    with checking('FINANCIAL_READ'):
        response = sheets.spreadsheets().values().batchGet(spreadsheetId=record['states'][SOURCES[0]]['spreadsheet_id'],
            ranges=list(RANGES), valueRenderOption='UNFORMATTED_VALUE').execute(num_retries=0)
    require(len(response['valueRanges']) == 4)
    raw = dict(zip(TABLES, (x.get('values', []) for x in response['valueRanges']), strict=True))
    verify_financial(raw, record)
    verify_pdfs(record, drive, raw)


@checking('PDF_MATCH')
def verify_pdfs(record, drive, raw):
    inbox = drive.files().list(q=f"'{record['inbox_folder']}' in parents and trashed=false and mimeType='application/pdf'",
        fields='files(id),nextPageToken', pageSize=6, supportsAllDrives=True, includeItemsFromAllDrives=True).execute(num_retries=0)
    require(not inbox.get('nextPageToken') and {p['id'] for p in inbox['files']} == {p['metadata']['id'] for p in record['pdfs']})
    # Verify native parser identities without classification or any writer.
    from .bank_pdf_pipeline import BankPdfPipeline
    occurrences = Counter()
    with tempfile.TemporaryDirectory(prefix='bank-recovery-read-') as directory:
        for index, pdf in enumerate(record['pdfs']):
            fid = pdf['metadata']['id']
            meta = drive.files().get(fileId=fid, fields=PDF_FIELDS, supportsAllDrives=True).execute(num_retries=0)
            data = drive.files().get_media(fileId=fid, supportsAllDrives=True).execute(num_retries=0)
            require(meta == pdf['metadata'] and sha(data) == pdf['sha256'])
            path = Path(directory) / f'{index}.pdf'
            path.write_bytes(data)
            parsed = BankPdfPipeline().parse(path, account_alias=pdf['account_alias'],
                existing_identities={str(r[0]) for r in raw[TABLES[0]][1:] if r and r[0]})
            require(not parsed.issues and not parsed.balance_consistency_failures
                and sha(encode([asdict(tx) for tx in parsed.transactions])) == pdf['transactions_sha256'])
            occurrences.update(tx.source_row_identity for tx in parsed.transactions)
    require(set(occurrences) == set(record['transaction_ids']) and sum(occurrences.values()) == 547
        and {i for i, count in occurrences.items() if count > 1} == set(record['duplicate_ids']))


def boundary(env):
    owner = REPOSITORY.split('/')[0]
    require(env.get('GITHUB_ACTIONS') == 'true' and env.get('GITHUB_REPOSITORY') == REPOSITORY
        and env.get('GITHUB_EVENT_NAME') == 'workflow_dispatch' and env.get('GITHUB_REF') == 'refs/heads/main'
        and env.get('GITHUB_ACTOR') == env.get('GITHUB_TRIGGERING_ACTOR') == owner
        and env.get('GITHUB_RUN_ATTEMPT') == '1'
        and env.get('GITHUB_WORKFLOW_REF') == REPOSITORY + '/' + WORKFLOW + '@refs/heads/main'
        and re.fullmatch('[0-9a-f]{40}', env.get('GITHUB_SHA', ''))
        and env['GITHUB_SHA'] == env.get('GITHUB_WORKFLOW_SHA') == env.get('EXPECTED_SHA') == env.get('KAKEIBO_VALIDATED_MAIN_SHA')
        and env.get('KAKEIBO_SCHEDULE_ENABLED') == 'false' and env.get('KAKEIBO_LEGACY_DISABLED') == 'true'
        and env.get('CATEGORY_BACKFILL_SHEET_RUNNER_ENABLED') == 'false' and env.get('RUNNER_DEBUG') != '1'
        and env.get('RECOVERY_MODE') in {'dry_run', 'release'})
    require(env.get('RECOVERY_EXECUTION_POLICY', 'strict') in {'strict', 'nonexclusive'})
    require(re.fullmatch('[0-9a-f]{32}', env.get('RECOVERY_OPERATION', '')))
    if env['RECOVERY_MODE'] == 'release':
        require(env.get('RECOVERY_CONFIRM') == 'RELEASE_40961220838')


def decode_capsule(text, key):
    """Reuse existing RSA/AES capsule format and SA key; fixed new record hash."""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    require(len(text) <= 60000)
    sealed = json.loads(base64.b64decode(text, validate=True))
    b64 = lambda n: base64.b64decode(sealed[n], validate=True)
    aes = key.decrypt(b64('key'), padding.OAEP(mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=LABEL))
    decoder = zlib.decompressobj()
    plain = decoder.decompress(AESGCM(aes).decrypt(b64('nonce'), b64('data'), LABEL), 1024 * 1024)
    require(decoder.eof and not decoder.unused_data and not decoder.unconsumed_tail)
    value = json.loads(plain)
    record = json.loads(base64.b64decode(value['record'], validate=True))
    evidence(record)
    key.public_key().verify(base64.b64decode(value['signature'], validate=True), encode(value['attestation']),
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.MAX_LENGTH), hashes.SHA256())
    return record, value['attestation']


def stopped_workflows(repo):
    """Exclude other credential-bearing entries, including manual and backup."""
    return {'.github/workflows/' + path.name for path in (repo / '.github/workflows').glob('*.yml')
            if '.github/workflows/' + path.name != WORKFLOW
            and any('secrets.' + name in path.read_text(encoding='utf-8') for name in (
                'GOOGLE_SERVICE_ACCOUNT_JSON', 'GOOGLE_GMAIL_TOKEN_JSON', 'GOOGLE_DRIVE_BACKUP_TOKEN_JSON'))}


@checking('GITHUB_CONTROLS')
def controls(env, attestation, repo, *, get=None, head=None, now=None):
    """Strict exclusion or signed target-only nonexclusive risk acceptance."""
    import requests
    boundary(env)
    stamp = datetime.now(timezone.utc).timestamp() if now is None else now
    require(0 <= stamp - attestation['at_epoch'] <= 900
        and attestation['record_sha256'] == RECORD_SHA256
        and attestation['main_sha'] == env['GITHUB_SHA']
        and attestation['operation_id'] == env['RECOVERY_OPERATION']
        and attestation['mode'] == env['RECOVERY_MODE']
        and attestation['release_authorized'] is (env['RECOVERY_MODE'] == 'release')
        and attestation['configuration_unchanged'] is True)
    policy = env.get('RECOVERY_EXECUTION_POLICY', 'strict')
    require(attestation.get('execution_policy', 'strict') == policy)
    if policy == 'strict':
        require(attestation['external_writers_frozen'] is True
            and attestation['no_dispatch_until_explicit_resume'] is True)
        writers = attestation['writers']
        require(writers['KnownWriterCount'] == 0 and writers['Tasks']
            and all(t['State'] == 'Disabled' for t in writers['Tasks']))
    else:
        require(attestation['nonexclusive_authorized'] is True
            and attestation['residual_risk'] == NONEXCLUSIVE_RISK
            and attestation['target_run_id'] == FAILED_RUN and attestation['target_run_attempt'] == 1
            and attestation['normal_apply_resume_authorized'] is False
            and attestation.get('external_writers_frozen') is not True)
    if get is None:
        session = requests.Session()
        session.headers.update(Authorization='Bearer ' + env['GH_TOKEN'], Accept='application/vnd.github+json')
        def get(path):
            name = ('GITHUB_VARIABLES' if path.startswith('actions/variables') else
                'RUNS' if path.startswith('actions/runs') or '/runs?' in path else 'GITHUB_API')
            with checking(name):
                response = session.get('https://api.github.com/repos/' + REPOSITORY + '/' + path, timeout=30)
                response.raise_for_status()
                return response.json()
    require(get('branches/main')['commit']['sha'] == env['GITHUB_SHA'])
    with checking('CONTROL_VARIABLES'):
        if policy == 'nonexclusive':
            # GITHUB_TOKEN has no Variables permission. Require a fresh signed
            # operator GET snapshot, matching Actions' native vars context.
            # This is a bounded observation, not a lock against later changes.
            require(attestation['control_variables_source'] == 'operator_GET_actions_variables'
                and 0 <= stamp - attestation['controls_observed_at_epoch'] <= 900
                and attestation['controls_observed_at_epoch'] <= attestation['at_epoch'])
            require(attestation['control_variables'] == {k: env[k] for k in CONTROL_VARIABLES})
        else:
            variables = get('actions/variables?per_page=100')
            require(variables['total_count'] <= 100)
            current_vars = {x['name']: x['value'] for x in variables['variables']}
            require(all(current_vars.get(k) == env[k] for k in CONTROL_VARIABLES))
    run = get('actions/runs/' + env['GITHUB_RUN_ID'])
    require(run['path'] == WORKFLOW and run['head_sha'] == env['GITHUB_SHA']
        and run['run_attempt'] == 1 and run['status'] == 'in_progress' and run['event'] == 'workflow_dispatch'
        and run['actor']['login'] == run['triggering_actor']['login'] == REPOSITORY.split('/')[0])
    executions = get('actions/workflows/' + WORKFLOW.rsplit('/', 1)[1] + '/runs?event=workflow_dispatch&per_page=100')
    matching = [r for r in executions['workflow_runs']
                if r['display_title'] == 'bank40961220838-' + env['RECOVERY_OPERATION']]
    require(executions['total_count'] <= 100 and len(matching) == 1
        and str(matching[0]['id']) == env['GITHUB_RUN_ID'])
    failed = get('actions/runs/' + str(FAILED_RUN))
    require(failed['head_sha'] == FAILED_HEAD and failed['run_attempt'] == 1
        and failed['status'] == 'completed' and failed['conclusion'] == 'failure')
    if policy == 'strict':
        workflows = get('actions/workflows?per_page=100')
        require(workflows['total_count'] <= 100)
        states = {x['path']: x['state'] for x in workflows['workflows']}
        require(stopped_workflows(repo) and all(states.get(p) == 'disabled_manually' for p in stopped_workflows(repo)))
    with checking('RUNS'):
        for status in ('in_progress', 'queued', 'waiting', 'pending', 'requested'):
            runs = get('actions/runs?status=' + status + '&per_page=100')
            require(runs['total_count'] <= 100 and all(str(r['id']) == env['GITHUB_RUN_ID'] for r in runs['workflow_runs']))
    head = head or requests.head
    with checking('CLOUD_503'):
        require(head('https://hga-auth-synthetic-efekwfyq3q-an.a.run.app', timeout=30, allow_redirects=False).status_code == 503)


def main():
    report = dict(success=False, production_apply_started=False, financial_write_calls=0, pdf_changes=0,
                  bank_write_attempts=0, parent_write_attempts=0, automatic_retry=False, rollback=False)
    try:
        env, repo = dict(os.environ), Path(__file__).resolve().parents[1]
        with checking('ENV_SHA'):
            boundary(env)
            require(subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=repo, text=True,
                stderr=subprocess.PIPE).strip() == env['GITHUB_SHA'])
        report.update(execution_policy=env.get('RECOVERY_EXECUTION_POLICY', 'strict'),
                      exclusive_execution_claimed=env.get('RECOVERY_EXECUTION_POLICY', 'strict') == 'strict')
        from .settings import service_account_source
        from cryptography.hazmat.primitives import serialization
        with checking('SA_CREDENTIALS'):
            path, info = service_account_source()
            info = info or json.loads(Path(path).read_bytes())
            key = serialization.load_pem_private_key(info['private_key'].encode(), password=None)
        with checking('CAPSULE'):
            record, attestation = decode_capsule(env['RECOVERY_CAPSULE'], key)
        from .private_state_bindings import decode_environment
        with checking('DRIVE_BINDINGS'):
            env, _ = decode_environment(env)
            bindings = {s: StateBinding(s, env['SPREADSHEET_ID'], env['KAKEIBO_STATE_FOLDER_ID'], env[name])
                for s, name in [(SOURCES[0], 'BANK_STATE_FILE_ID'), (SOURCES[1], 'KAKEIBO_RUN_LEDGER_FILE_ID')]}
            require(all(bindings[s].reference == record['states'][s]['binding_reference'] for s in SOURCES))
        from .google_clients import read_only_drive_service, read_only_sheets_service
        from .bank_apply_readonly_inspect import GetOnly
        readonly, sheets = GetOnly(read_only_drive_service()), GetOnly(read_only_sheets_service())
        # The write client is not constructed until signed controls/evidence pass.
        guard = lambda: controls(env, attestation, repo)
        guard()
        if env['RECOVERY_MODE'] == 'release':
            from .google_clients import drive_service
            drive = drive_service()
        else:
            drive = readonly  # Non-GET construction is impossible in dry-run.
        transports = {s: DriveStateTransport(drive, b) for s, b in bindings.items()}
        recover(record, transports, bindings, guard, lambda: observe(record, readonly, sheets),
            lambda s: DriveStateTransport(readonly, bindings[s]).read(),
            dry_run=env['RECOVERY_MODE'] == 'dry_run', report=report)
    except Exception:
        report.update(success=False, outcome='stopped_no_retry', error='bank_attempt_recovery_stopped')
    print(json.dumps(report, sort_keys=True))  # Fixed fields only. No exception/body/IDs.
    if not report['success']:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
