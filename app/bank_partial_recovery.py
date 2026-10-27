"""Bound recovery of run 40590186305: keep 100 imports, append six incomes only.

Release bank and release parent are separate manual Actions operations. Financial
completion uses the existing production workflow/permit, after both releases.
No path retries a write, rolls back, appends imports or handles other batches.
"""
from __future__ import annotations

import base64
from contextlib import closing
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
from uuid import uuid4
from zoneinfo import ZoneInfo
import zlib

from . import bank_fixed_backfill as fixed
from .bank_pending_recovery import (encode, sha, require, ReleaseOnlyTransport,
                                    verify_sqlite, RANGES, PDF_FIELDS, LABEL, REPOSITORY)
from .drive_run_state import DurableState, DriveStateTransport, StateBinding, validate
from .production_ledger import ProductionLedger, validate_ledger

WORKFLOW = '.github/workflows/bank-partial-recovery.yml'
PRODUCTION = '.github/workflows/kakeibo-production.yml'
FAILED_RUN = 40590186305
FAILED_HEAD = 'f555f8a59a0d6084263ab1de5347c6af6a0cb45a'
RECORD_SHA256 = 'bdd0583dbcda759471cebe1c64331ac2a0c427acf664e6440ed7c94fe730fd24'
MANIFEST_SHA256 = '23f0eb18ce93243597faabbd14d99f09c0d66bba4cbeeeb6d8ce31d9b4b7be67'
PLAN_SHA256 = '8f64ac790aafb03c605c270a9540db5826ad248630b0eb2db36a26cc05d99135'
SOURCES = ('bank_pdf_drive', 'production_run')
TABLES = ('取込データ', '収入明細', '支出明細', '_手入力受付')


def decode_evidence(text):
    """Existing SA key; this decoder accepts only the NEW, exact partial proof."""
    from .settings import service_account_source
    from cryptography.hazmat.primitives import serialization, hashes
    from cryptography.hazmat.primitives.asymmetric import padding
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    path, info = service_account_source()
    info = info or json.loads(Path(path).read_bytes())
    key = serialization.load_pem_private_key(info['private_key'].encode(), password=None)
    sealed = json.loads(base64.b64decode(text, validate=True))
    b64 = lambda name: base64.b64decode(sealed[name], validate=True)
    aes = key.decrypt(b64('key'), padding.OAEP(mgf=padding.MGF1(hashes.SHA256()),
                                            algorithm=hashes.SHA256(), label=LABEL))
    compressed = AESGCM(aes).decrypt(b64('nonce'), b64('data'), LABEL)
    decoder = zlib.decompressobj()
    plain = decoder.decompress(compressed, 4 * 1024 * 1024)
    require(decoder.eof and not decoder.unused_data and not decoder.unconsumed_tail)
    value = json.loads(plain)
    original = base64.b64decode(value['record'], validate=True)
    require(sha(original) == RECORD_SHA256 and sha(encode(value['manifest'])) == MANIFEST_SHA256)
    key.public_key().verify(base64.b64decode(value['signature'], validate=True), encode(value['attestation']),
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.MAX_LENGTH), hashes.SHA256())
    return json.loads(original), value['manifest'], value['attestation']


def boundary(env, head):
    owner = 'tmoriuchi1401-source'
    refs = {REPOSITORY + '/' + path + '@refs/heads/main' for path in (WORKFLOW, PRODUCTION)}
    require(env.get('GITHUB_ACTIONS') == 'true' and env.get('GITHUB_REPOSITORY') == REPOSITORY
        and env.get('GITHUB_ACTOR') == env.get('GITHUB_TRIGGERING_ACTOR') == owner
        and env.get('GITHUB_REF') == 'refs/heads/main' and env.get('GITHUB_EVENT_NAME') == 'workflow_dispatch'
        and env.get('GITHUB_WORKFLOW_REF') in refs and env.get('GITHUB_RUN_ATTEMPT') == '1'
        and env.get('GITHUB_SHA') == env.get('GITHUB_WORKFLOW_SHA') == env.get('KAKEIBO_VALIDATED_MAIN_SHA') == head
        and env.get('KAKEIBO_SCHEDULE_ENABLED') == 'false' and env.get('KAKEIBO_LEGACY_DISABLED') == 'true'
        and env.get('CATEGORY_BACKFILL_SHEET_RUNNER_ENABLED') == 'false' and env.get('RUNNER_DEBUG') != '1')


def controls(env, attestation):
    """No configurable run, proof or workflow bypass: both entries are fixed."""
    import requests
    boundary(env, env['GITHUB_SHA'])
    age = datetime.now(timezone.utc).timestamp() - attestation['at_epoch']
    writers = attestation['writers']
    require(0 <= age <= 900 and attestation['main_sha'] == env['GITHUB_SHA']
        and attestation['record_sha256'] == RECORD_SHA256 and attestation['configuration_unchanged'] is True
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
    expected_path = env['GITHUB_WORKFLOW_REF'][len(REPOSITORY) + 1:].split('@')[0]
    require(current['path'] == expected_path and current['head_sha'] == env['GITHUB_SHA']
        and current['event'] == 'workflow_dispatch' and current['run_attempt'] == 1 and current['status'] == 'in_progress'
        and current['actor']['login'] == current['triggering_actor']['login'] == 'tmoriuchi1401-source')
    failed = get('actions/runs/' + str(FAILED_RUN))
    require(failed['status'] == 'completed' and failed['conclusion'] == 'failure'
        and failed['run_attempt'] == 1 and failed['head_sha'] == FAILED_HEAD)
    for status in ('in_progress', 'queued', 'waiting', 'pending', 'requested'):
        runs = get('actions/runs?status=' + status + '&per_page=100')
        require(runs['total_count'] <= 100 and all(str(r['id']) == env['GITHUB_RUN_ID'] for r in runs['workflow_runs']))
    require(requests.head('https://hga-auth-synthetic-efekwfyq3q-an.a.run.app', timeout=30,
                          allow_redirects=False).status_code == 503)


def expected_imports(plan, manifest):
    from .bank_pdf_pipeline import NormalizedBankTransaction
    from .canonical_import import materialize_import_row
    stamp = datetime.fromisoformat(manifest['imported_at']).replace(tzinfo=ZoneInfo('Asia/Tokyo'))
    result = {}
    for identity in manifest['selected100']:
        item = plan['items'][identity]
        tx = NormalizedBankTransaction(**item['transaction'])
        result[identity] = materialize_import_row(tx.to_canonical(), imported_at=stamp,
            status={'income': 'bank_income', 'nonposting': 'bank_non_expense'}[item['disposition']])
    require(sha(encode(result)) == manifest['imports_sha256'])
    return result


def evidence(record, manifest, plan):
    require(sha(encode(record)) == RECORD_SHA256 and sha(encode(manifest)) == MANIFEST_SHA256
        and record['run_id'] == FAILED_RUN and record['run_attempt'] == 1
        and record['kind'] == 'fixed_bank_partial_100_reconciliation'
        and record['plan_sha256'] == fixed.digest(plan) == PLAN_SHA256
        and not record['financial_apply_authorized_by_this_record']
        and not record['remote_sqlite_intents_requiring_resolution'])
    require(list(fixed.batch_ids(plan, 0)) == manifest['selected100']
        and len(set(manifest['selected100'])) == 100 and len(manifest['incomes']) == 6
        and sum(r[2] for r in manifest['incomes'].values()) == 4669
        and sum(plan['items'][i]['disposition'] == 'income' for i in manifest['selected100']) == 6
        and sum(plan['items'][i]['disposition'] == 'nonposting' for i in manifest['selected100']) == 94)
    for source, generation in [('bank_pdf_drive', 45), ('production_run', 1290)]:
        require(record['states'][source]['generation_before'] == generation
            and record['states'][source]['generation_required_after'] == generation + 1)
    return expected_imports(plan, manifest)


def raw_tables(db):
    return {name: db.get_raw(rng) for name, rng in zip(TABLES, RANGES)}


def content(db, manifest, imports, *, completed):
    from .bank_income_recurring import _exact_present
    _exact_present(db, '取込データ', list(imports.values()))  # ALL twelve columns, unchanged timestamp included.
    phase = 'after' if completed else 'before'
    require(sha(encode(fixed.snapshot(db))) == manifest['financial_' + phase + '_sha256'])
    require(sha(encode(raw_tables(db))) == manifest['raw_tables_' + phase + '_sha256'])


def observe_release(drive, sheets, record, manifest, sid):
    from .sheets import SheetsDB
    from .bank_review_store import BankReviewStore
    db = SheetsDB(sid, service=sheets)
    raw = sheets.spreadsheets().values().batchGet(spreadsheetId=sid, ranges=RANGES,
        valueRenderOption='UNFORMATTED_VALUE').execute(num_retries=0)
    require(sha(encode(raw)) == manifest['raw_financial_sha256'])
    require(BankReviewStore(db).read().fingerprint == record['master_sha256'])
    for pdf in manifest['pdfs']:
        fid = pdf['metadata']['id']
        metadata = drive.files().get(fileId=fid, fields=PDF_FIELDS, supportsAllDrives=True).execute(num_retries=0)
        payload = drive.files().get_media(fileId=fid, supportsAllDrives=True).execute(num_retries=0)
        require(metadata == pdf['metadata'] and sha(payload) == pdf['sha256'])


def release(operation, record, manifest, plan, transports, bindings, guard, observation, report):
    """One explicit write at most. Never release both states in one operation."""
    evidence(record, manifest, plan)
    require(operation in {'inspect', 'release_bank', 'release_parent'})
    guard()
    observation()
    observed = {}
    for source in SOURCES:
        binding, entry = bindings[source], record['states'][source]
        require((binding.spreadsheet_id, binding.folder_id, binding.file_id)
            == (entry['spreadsheet_id'], entry['folder_id'], entry['file_id']))
        payload = transports[source].read()
        value = (validate if source == 'bank_pdf_drive' else validate_ledger)(payload, binding)
        require(sha(payload) in {entry['observed_raw_sha256'], entry['expected_after_raw_sha256']})
        item = value if source == 'bank_pdf_drive' else value['sources']['bank']
        done = sha(payload) == entry['expected_after_raw_sha256']
        require(value['generation'] == entry['generation_required_after' if done else 'generation_before']
            and item['phase'] == ('ready' if done else 'pending')
            and item['attempt'] == ('' if done else entry['attempt']))
        observed[source] = (payload, value)
    bank_raw, bank = observed['bank_pdf_drive']
    parent_raw, parent = observed['production_run']
    verify_sqlite(bank, record)
    require(all(v['phase'] == 'ready' and not v['error'] for k, v in parent['sources'].items() if k != 'bank'))
    bank_done = sha(bank_raw) == record['states']['bank_pdf_drive']['expected_after_raw_sha256']
    parent_done = sha(parent_raw) == record['states']['production_run']['expected_after_raw_sha256']
    require(not parent_done or bank_done)
    if operation == 'release_bank':
        require(not bank_done and not parent_done)
        source = 'bank_pdf_drive'
    elif operation == 'release_parent':
        require(bank_done and not parent_done and bank['generation'] == 46 and bank['phase'] == 'ready')
        source = 'production_run'
    else:
        report.update(success=True, bank_generation=bank['generation'], parent_generation=parent['generation'],
                      bank_ready=bank_done, parent_bank_ready=parent_done, financial_writes=0)
        return
    entry = record['states'][source]
    require(observed[source][1]['generation'] == entry['generation_before'])
    item = bank if source == 'bank_pdf_drive' else parent['sources']['bank']
    require(item['phase'] == 'pending' and item['attempt'] == entry['attempt'])
    guard()
    require(all(transports[s].read() == observed[s][0] for s in SOURCES))
    report.update(operation=operation, outcome='attempted_unknown')
    transport = ReleaseOnlyTransport(transports[source], entry['expected_after_raw_sha256'])
    kwargs = dict(observed_digest=entry['observed_raw_sha256'], evidence_reference=RECORD_SHA256)
    if source == 'bank_pdf_drive':
        DurableState(transport, bindings[source]).release_after_reconciliation(**kwargs)
    else:
        ProductionLedger(transport, bindings[source]).release_after_reconciliation('bank', **kwargs)
    require(sha(transports[source].read()) == entry['expected_after_raw_sha256'])
    other = 'production_run' if source == 'bank_pdf_drive' else 'bank_pdf_drive'
    require(transports[other].read() == observed[other][0])
    verify_sqlite(validate(transports['bank_pdf_drive'].read(), bindings['bank_pdf_drive']), record)
    observation()
    guard()
    report.update(success=True, outcome='readback_verified_stop', financial_writes=0,
                  generation=entry['generation_required_after'], checkpoint_sqlite_unchanged=True)


def execute(plan, batch, *, db, state, policy, pdf_bytes, rules, resolver, now,
            context, apply=False, grant=None):
    """Existing writer and append-only evidence; imports have NO write capability."""
    from .bank_income import BankIncomePipeline, deposit_decisions
    from .bank_income_recurring import IncomeAppendEvidence, _exact_present
    record, manifest, runtime_guard = context
    require(batch == 0)
    imports = evidence(record, manifest, plan)
    if apply:
        require(type(grant) is fixed._Grant and grant.token is fixed._GRANT
            and grant.plan == fixed.digest(plan) and now < grant.expires)
    runtime_guard()
    before, transactions = fixed.validate_live(plan, db, pdf_bytes, plan['policy_sha256'], rules, resolver)
    incomes = {}
    for identity in manifest['selected100']:
        if plan['items'][identity]['disposition'] == 'income':
            decision = deposit_decisions([transactions[identity]], **rules, meaning_resolver=resolver)[0]
            require(len(decision) == 1 and decision[0].outcome == 'confirmed_income')
            row = decision[0].row()
            incomes[row[0]] = row
    require(incomes == manifest['incomes'])
    incomes = dict(sorted(incomes.items()))
    # Every pending append, including another attempt, blocks this path.
    import sqlite3
    with closing(sqlite3.connect(state.path)) as connection:
        events = [json.loads(r[0]) for r in connection.execute('SELECT summary_json FROM recurring_runs ORDER BY rowid')]
    pending = {}
    relevant = []
    for event in events:
        status = event['status']
        if status.startswith('income_append_'):
            key = ('income', event['income_attempt'])
        elif status.startswith('fixed_backfill_'):
            key = ('fixed', event['fixed_plan'], event['fixed_batch'])
            if event['fixed_plan'] == fixed.digest(plan) and event['fixed_batch'] == 0:
                relevant.append(event)
        else:
            continue
        if status.endswith('_pending'):
            pending[key] = event
        elif status.endswith('_verified'):
            pending.pop(key, None)
    require(not pending)
    result = dict(success=True, bounded_rows=100, income=6, income_yen=4669, expense=0,
                  non_expense=94, review=0, written=0, imports_skipped=100, deposit_imports_created=0)
    if relevant:
        last = relevant[-1]
        require(last['status'] == 'fixed_backfill_verified' and last.get('reconciliation_reference') == RECORD_SHA256
            and last['fixed_rows'] == {'取込データ': imports, '収入明細': incomes, '支出明細': {}})
        content(db, manifest, imports, completed=True)
        _exact_present(db, '収入明細', list(incomes.values()))
        return dict(result, backfill_verified_replay=1, duplicate=100, read_back_verified=True)
    content(db, manifest, imports, completed=False)
    require(not any(event['status'].startswith(('income_append_', 'fixed_backfill_')) for event in events))
    if not apply:
        return result
    summary = dict(run_id=str(uuid4()), source_window_start=now.isoformat(), source_window_end=now.isoformat(),
        status='fixed_backfill_pending', fixed_plan=fixed.digest(plan), fixed_batch=0,
        reconciliation_reference=RECORD_SHA256, partial_origin_run=FAILED_RUN,
        existing_imports_reconciled=100, imported_rows_sha256=manifest['imports_sha256'])
    state.record(summary, advance_checkpoint=False)
    def guard():
        runtime_guard()
        fixed.validate_live(plan, db, pdf_bytes, plan['policy_sha256'], rules, resolver)
        content(db, manifest, imports, completed=False)
    guarded = fixed.FixedWriteDB(db, manifest['selected100'], imports, incomes, guard)
    append_evidence = IncomeAppendEvidence(guarded, state, summary, meaning_resolver=resolver)
    append_evidence.require_existing_history()
    class IncomeOnly:
        sid = db.sid
        def get(self, rng):
            # The existing income writer also reads literal amounts/dates.
            return db.get_raw(rng) if rng.startswith(('取込データ!', '収入明細!')) else db.get(rng)
        def sheet_titles(self): return db.sheet_titles()
        def append_raw(self, sheet, rows):
            require(sheet == '収入明細' and rows == list(incomes.values()))
            append_evidence.append(sheet, rows)
    checkpoint = state.successful_window_end()
    BankIncomePipeline(IncomeOnly(), **rules, meaning_resolver=resolver).apply(
        tuple(row[6] for row in incomes.values()), income_write_enabled=True,
        approved_spreadsheet_id=plan['target'], max_rows=6)
    _exact_present(db, '収入明細', list(incomes.values()))
    content(db, manifest, imports, completed=True)
    runtime_guard()
    fixed.validate_live(plan, db, pdf_bytes, plan['policy_sha256'], rules, resolver)
    require(state.successful_window_end() == checkpoint)
    state.record(dict(summary, run_id=str(uuid4()), status='fixed_backfill_verified',
        fixed_rows={'取込データ': imports, '収入明細': incomes, '支出明細': {}}), advance_checkpoint=False)
    return dict(result, written=6, read_back_verified=True)


def income_context(env, head):
    boundary(env, head)
    require(env.get('KAKEIBO_BANK_PARTIAL_RECOVERY_ENABLED') == 'true')
    require(env['GITHUB_WORKFLOW_REF'] == REPOSITORY + '/' + PRODUCTION + '@refs/heads/main')
    record, manifest, attestation = decode_evidence(env['BANK_FIXED_BACKFILL_RECOVERY'])
    return record, manifest, lambda: controls(env, attestation)


def main():
    report = dict(success=False, outcome='not_attempted', financial_writes=0, automatic_retry=False, rollback=False)
    try:
        env = dict(os.environ)
        head = subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip()
        boundary(env, head)
        require(env.get('KAKEIBO_BANK_PARTIAL_RECOVERY_ENABLED') == 'true')
        require(env['EXPECTED_SHA'] == head and env['GITHUB_WORKFLOW_REF'] == REPOSITORY + '/' + WORKFLOW + '@refs/heads/main')
        record, manifest, attestation = decode_evidence(env['RECOVERY_CAPSULE'])
        require(sha(env['BANK_PDF_RECURRING_AUTHORITY_JSON'].encode()) == record['policy_sha256'])
        from .google_clients import credentials, read_only_drive_service, read_only_sheets_service, download_drive_file
        from .private_state_bindings import decode_environment, unwrap
        from .settings import service_account_source
        from googleapiclient.discovery import build
        env, _ = decode_environment(env)
        path, info = service_account_source()
        info = info or json.loads(Path(path).read_bytes())
        fid = unwrap('BANK_FIXED_BACKFILL_PLAN_FILE_ID', env['BANK_FIXED_BACKFILL_PLAN'], info['private_key'])
        readonly, sheets = read_only_drive_service(), read_only_sheets_service()
        plan = json.loads(download_drive_file(fid, readonly))
        operation = env['RECOVERY_OPERATION']
        # inspect creates no write-scoped credential/client.
        drive = readonly if operation == 'inspect' else build('drive', 'v3',
            credentials=credentials(['https://www.googleapis.com/auth/drive']), cache_discovery=False)
        bindings = {s: StateBinding(s, env['SPREADSHEET_ID'], env['KAKEIBO_STATE_FOLDER_ID'], env[n]) for s, n in
                    [('bank_pdf_drive', 'BANK_STATE_FILE_ID'), ('production_run', 'KAKEIBO_RUN_LEDGER_FILE_ID')]}
        transports = {s: DriveStateTransport(drive, b) for s, b in bindings.items()}
        release(operation, record, manifest, plan, transports, bindings, lambda: controls(env, attestation),
                lambda: observe_release(readonly, sheets, record, manifest, env['SPREADSHEET_ID']), report)
        report.update(run_id=int(env['GITHUB_RUN_ID']), main_sha=head, common_lock='kakeibo-production')
    except Exception:
        report.update(success=False, error='bank_partial_recovery_stopped')
    rendered = json.dumps(report, sort_keys=True)
    print(rendered)
    if os.environ.get('GITHUB_STEP_SUMMARY'):
        with open(os.environ['GITHUB_STEP_SUMMARY'], 'a', encoding='utf-8') as stream:
            stream.write('```json\n' + rendered + '\n```\n')
    if not report['success']:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
