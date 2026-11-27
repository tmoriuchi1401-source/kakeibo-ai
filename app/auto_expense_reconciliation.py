"""Fixed state-only operator connection; existing durable store and release API.

No financial client, new database, recursive native archive or automatic resume.
"""
import base64
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import tempfile
import time
from uuid import uuid4

from .aupay_card_aliases import digest
from .aupay_card_window_preview import require
from .production_ledger import ProductionLedger

RECORD_SHA256 = '815c1fa78645fe32ec74474fdc14427dbeb68f236ccdc8211ca0b2a60c8e97b0'
LABEL = b'kakeibo-auto-expense-state-only-v1'
SCOPE = 'auto_expense_state_only_reconciliation'
FAILED_RUN = 46013990252
ARCHIVE_RUN = 'OPERATOR-AUTO-EXPENSE:46013990252'
RELEASE_TITLE = 'auto-expense-reconciliation-46013990252-apply'
MAX_ARCHIVE_BYTES = 32768
SAVED_OPERATION = 'release_saved_archive'
SAVED_ARCHIVE_SHA256 = '8d92edad186defce88564a787083d4b0f4d4f3bbe89001a93bbcf466cabeda2b'
SAVED_NATIVE_PROOF = dict(
    sha256='b828b3a1b784b205924641988d6b4b154bddbd79931468a5a1c14c2f57c3156f',
    metadata_digest='9315b4df16343b1325f298bcee85b2b2dd82d56f20ff63516f234213eec1dbe0')
SAVED_OPERATOR = dict(id=46076915652, head_sha='6453d12f66a0ed8a7879006d817bc21f7d3f9d87',
    run_attempt=1, event='workflow_dispatch', path='.github/workflows/kakeibo-production.yml')


class AutoExpenseReconciliationFailure(RuntimeError):
    def __init__(self, diagnostic):
        super().__init__('auto_expense_reconciliation_stopped')
        self.diagnostic = diagnostic


def sha(raw): return hashlib.sha256(raw).hexdigest()


def proposed_release(raw, binding, evidence_reference):
    """Generate the permitted payload through the real library, memory only."""
    class Memory:
        def __init__(self): self.raw = raw
        def read(self): return self.raw
        def write(self, value): self.raw = value
    memory = Memory()
    ProductionLedger(memory, binding).release_after_reconciliation('auto_expense',
        observed_digest=sha(raw), evidence_reference=evidence_reference)
    return memory.raw


def persist_archive(durable, path, record, archive, guard):
    """One small row in the existing append-only history; C1 does not advance."""
    from .aupay_card_recurring import SqliteRecurringRunState
    require(len(json.dumps(archive,ensure_ascii=True).encode())<=MAX_ARCHIVE_BYTES,
            'auto_expense_reconciliation_archive_too_large')
    durable.restore(path)
    state=SqliteRecurringRunState(path/'recurring.sqlite3',repo_root=Path(__file__).resolve().parents[1])
    require(state.successful_window_end().isoformat()==record['checkpoint'],
            'auto_expense_reconciliation_checkpoint_changed')
    guard();durable.begin(uuid4().hex)
    state.record(dict(run_id=ARCHIVE_RUN,status='noop',source_window_start=record['checkpoint'],
        source_window_end=record['checkpoint'],operator_reconciliation=archive,failure=0),advance_checkpoint=False)
    guard();durable.commit(path)
    return durable._loaded


def release_with_archive(*, record, parent_raw, binding, parent_transport,
                         save_archive, read_archive, observe, guard, apply, operator_identity=None):
    """One ordered use of the existing archive and parent APIs, no retries."""
    require(digest(record) == RECORD_SHA256, 'auto_expense_reconciliation_record_changed')
    parent = json.loads(parent_raw)
    item = parent['sources']['auto_expense']
    require(item['phase'] == 'pending' and item['attempt'] == record['failed_attempt']
            and item['error'] == 'source_execution_failed', 'auto_expense_reconciliation_target_changed')
    original = json.loads(base64.b64decode(record['parent_before_raw_b64'], validate=True))
    require(item == original['sources']['auto_expense'], 'auto_expense_reconciliation_target_changed')
    guard(); before = observe()
    archive = dict(schema=1, kind=SCOPE, failed_run=record['failed_run'],
        operator_run=operator_identity, archived_at=datetime.now(timezone.utc).isoformat(),
        original_parent_exact_b64=record['parent_before_raw_b64'],
        original_parent_revision=record['parent_before_revision'],
        reconciled_parent_exact_b64=base64.b64encode(parent_raw).decode(),
        parent_sha256=sha(parent_raw), native_before_sha256=record['native_before_sha256'],
        checkpoint=record['checkpoint'], record_sha256=RECORD_SHA256,
        observation=before, financial_write_reached=False, exception_identity_confirmed=False)
    require(len(json.dumps(archive, ensure_ascii=True).encode()) <= MAX_ARCHIVE_BYTES,
            'auto_expense_reconciliation_archive_too_large')
    reference = digest(archive)
    proposed = proposed_release(parent_raw, binding, reference)
    if not apply:
        require(observe() == before, 'auto_expense_reconciliation_snapshot_changed')
        return dict(success=True, mode='preview', archive_sha256=reference,
            parent_after_sha256=sha(proposed), parent_write_requests=0, financial_write_requests=0)
    # Failure or runner loss in either callback leaves pending. Never auto-resume.
    save_archive(archive)
    require(read_archive() == archive, 'auto_expense_reconciliation_archive_unconfirmed')
    guard()
    require(observe() == before, 'auto_expense_reconciliation_snapshot_changed')
    from .bank_pending_recovery import ReleaseOnlyTransport
    store = ProductionLedger(ReleaseOnlyTransport(parent_transport, sha(proposed)), binding)
    require(store.payload == parent_raw, 'auto_expense_reconciliation_snapshot_changed')
    store.release_after_reconciliation('auto_expense', observed_digest=sha(parent_raw),
                                       evidence_reference=reference)
    guard()
    require(parent_transport.read() == proposed, 'auto_expense_reconciliation_readback_failed')
    require(read_archive() == archive and observe(parent_after=proposed) == before,
            'auto_expense_reconciliation_snapshot_changed')
    return dict(success=True, mode='released_stop', archive_sha256=reference,
        parent_after_sha256=sha(proposed), parent_write_requests=1, financial_write_requests=0)


def validate_saved_archive(record, archive):
    """Accept the already independently reconciled archive, never a new record."""
    require(digest(record) == RECORD_SHA256 and digest(archive) == SAVED_ARCHIVE_SHA256,
            'auto_expense_reconciliation_archive_unconfirmed')
    require(archive['schema'] == 1 and archive['kind'] == SCOPE
        and archive['record_sha256'] == RECORD_SHA256 and archive['failed_run'] == record['failed_run']
        and archive['operator_run'] == SAVED_OPERATOR
        and archive['original_parent_exact_b64'] == record['parent_before_raw_b64']
        and archive['original_parent_revision'] == record['parent_before_revision']
        and archive['native_before_sha256'] == record['native_before_sha256']
        and archive['checkpoint'] == record['checkpoint']
        and archive['parent_sha256'] == sha(base64.b64decode(archive['reconciled_parent_exact_b64'], validate=True))
        and archive['financial_write_reached'] is False and archive['exception_identity_confirmed'] is False
        and archive['observation'] == {k: record[k] for k in
            ('financial_sha256', 'documents_sha256', 'rules_sha256', 'acl_sha256')},
        'auto_expense_reconciliation_archive_unconfirmed')


def release_saved_archive(*, record, archive, parent_raw, binding, parent_transport,
                          read_archive, observe, guard, expected_after_sha256, diagnostics):
    """Only the remaining parent API call; no archive/native writer is accepted."""
    diagnostics.mark('archive_prepare')
    validate_saved_archive(record, archive)
    original = json.loads(base64.b64decode(record['parent_before_raw_b64'], validate=True))
    item = json.loads(parent_raw)['sources']['auto_expense']
    require(item == original['sources']['auto_expense'] and item['phase'] == 'pending'
        and item['attempt'] == record['failed_attempt'] and item['error'] == 'source_execution_failed',
        'auto_expense_reconciliation_target_changed')
    proposed = proposed_release(parent_raw, binding, SAVED_ARCHIVE_SHA256)
    require(sha(proposed) == expected_after_sha256, 'auto_expense_reconciliation_target_changed')
    diagnostics.mark('parent_guard')
    guard()
    require(read_archive() == archive and observe() == archive['observation'],
            'auto_expense_reconciliation_snapshot_changed')
    from .bank_pending_recovery import ReleaseOnlyTransport
    store = ProductionLedger(ReleaseOnlyTransport(parent_transport, expected_after_sha256), binding)
    require(store.payload == parent_raw, 'auto_expense_reconciliation_snapshot_changed')
    # A separate read immediately before the existing API's own prewrite read.
    guard()
    require(read_archive() == archive and observe() == archive['observation'],
            'auto_expense_reconciliation_snapshot_changed')
    guard()
    diagnostics.mark('parent_save', 'parent_completion', 'write_possible')
    store.release_after_reconciliation('auto_expense', observed_digest=sha(parent_raw),
                                      evidence_reference=SAVED_ARCHIVE_SHA256)
    diagnostics.mark('parent_independent_readback')
    guard()
    require(parent_transport.read() == proposed, 'auto_expense_reconciliation_readback_failed')
    require(read_archive() == archive and observe(parent_after=proposed) == archive['observation'],
            'auto_expense_reconciliation_snapshot_changed')
    guard()
    diagnostics.mark('complete', 'parent_completion', 'readback_confirmed')
    return dict(success=True, mode='released_stop', archive_sha256=SAVED_ARCHIVE_SHA256,
        parent_after_sha256=sha(proposed), parent_write_requests=1, native_write_requests=0,
        archive_write_requests=0, financial_write_requests=0)


def run_from_environment(env, *, apply):
    from .aupay_card_reconciliation_diagnostics import ReconciliationDiagnostics
    diagnostics = ReconciliationDiagnostics()
    try:
        return _run_from_environment(env, apply=apply, diagnostics=diagnostics)
    except Exception as error:
        raise AutoExpenseReconciliationFailure(diagnostics.report(error)) from None


def _run_from_environment(env, *, apply, diagnostics):
    from cryptography.hazmat.primitives import serialization
    from .production_flow import verify_execution_boundary
    from .aupay_card_reconciliation_entry import decode_capsule
    from .aupay_card_window_preview import GitHub, ReadOnlyState
    from .drive_run_state import StateBinding, DriveStateTransport, DurableState, validate
    from .google_clients import read_only_drive_service, read_only_sheets_service, drive_service
    from .aupay_card_first_window_apply import financial_data
    from .aupay_card_recurring import SqliteRecurringRunState
    from .aupay_card_reservation_recovery import summaries
    from .aupay_card_incremental_recovery import preserved_unknown_proofs
    from .projection_store import DriveProjectionStore
    from .projection_cache import RollingProjectionStore
    from .aupay_card_bank_premises import collect_bank_premises
    import subprocess

    diagnostics.mark('release_guard')
    head = env.get('GITHUB_SHA', '')
    verify_execution_boundary(env, head)
    require(env.get('GITHUB_EVENT_NAME') == 'workflow_dispatch'
        and env.get('GITHUB_ACTOR') == env.get('GITHUB_TRIGGERING_ACTOR') == 'tmoriuchi1401-source'
        and env.get('GITHUB_RUN_ATTEMPT') == '1' and env.get('RUNNER_ENVIRONMENT') == 'github-hosted'
        and env.get('WORKFLOW_REF') == 'tmoriuchi1401-source/kakeibo-ai/.github/workflows/kakeibo-production.yml@refs/heads/main'
        and env.get('WORKFLOW_SHA') == head and env.get('MANUAL_SCOPE') == 'card_expenses'
        and not env.get('CARD_EXPENSE_REQUEST') and env.get('RUNNER_DEBUG', '0') != '1'
        and not env.get('ACTIONS_STEP_DEBUG') and not env.get('ACTIONS_RUNNER_DEBUG'),
        'auto_expense_reconciliation_owner_required')
    require(not apply or env.get('CONFIRM') == 'APPLY', 'auto_expense_reconciliation_owner_required')
    tree = subprocess.check_output(['git','rev-parse','HEAD^{tree}'], text=True).strip()
    info = json.loads(env['GOOGLE_SERVICE_ACCOUNT_JSON'])
    key = serialization.load_pem_private_key(info['private_key'].encode(), password=None)
    diagnostics.mark('capsule_decode')
    record, att = decode_capsule(env['CARD_EXPENSE_RECONCILIATION'], key, tree,
        datetime.now(timezone.utc), record_sha256=RECORD_SHA256, label=LABEL, scope=SCOPE, max_ttl=900)
    saved = att.get('operation') == SAVED_OPERATION
    require(record['failed_run']['id'] == FAILED_RUN and record['financial_write_authority'] is False
        and att['main_sha'] == head and att['controls_sha256'] == record['settings_without_validated_sha256']
        and att['operation'] == (SAVED_OPERATION if saved and apply else 'release' if apply else 'preview')
        and 0 <= (datetime.now(timezone.utc)-datetime.fromisoformat(att['observed_at'])).total_seconds() <= 900,
        'auto_expense_reconciliation_record_changed')
    require(env.get('CATEGORY_RULE_AUTO_APPLY_ENABLED') == 'true'
        and env.get('KAKEIBO_AMAZON_MONEY_MODE') == 'confirmed-v1'
        and env.get('KAKEIBO_BANK_SCHEDULE_ENABLED') == 'true'
        and env.get('KAKEIBO_CORE_SCHEDULE_ENABLED', 'false') == 'false'
        and env.get('KAKEIBO_CARD_SCHEDULE_ENABLED', 'false') == 'false'
        and env.get('KAKEIBO_LEGACY_DISABLED') == 'true'
        and env.get('KAKEIBO_SCHEDULE_ENABLED') == 'false'
        and env.get('CATEGORY_BACKFILL_SHEET_RUNNER_ENABLED') == 'false',
        'auto_expense_reconciliation_settings_changed')
    if saved:
        premises = att['saved_archive']
        require(set(premises) == {'archive_sha256', 'native', 'operator_run', 'parent', 'parent_after_sha256'}
            and premises['archive_sha256'] == SAVED_ARCHIVE_SHA256
            and premises['native'] == SAVED_NATIVE_PROOF and premises['operator_run'] == SAVED_OPERATOR
            and set(premises['parent']) == {'sha256', 'metadata_digest', 'revision'}
            and bool(att['approval'].get('approval_reference')) and head != SAVED_OPERATOR['head_sha'],
            'auto_expense_reconciliation_saved_approval_required')
    github = GitHub(env); deadline = time.monotonic() + 900
    expires = datetime.fromisoformat(att['approval']['expires_at'])
    def guard():
        require(time.monotonic() < deadline and datetime.now(timezone.utc) < expires,
                'auto_expense_reconciliation_expired')
        require(github.get('git/ref/heads/main')['object']['sha'] == head,
                'auto_expense_reconciliation_main_changed')
        current = int(env['GITHUB_RUN_ID'])
        run = github.get('actions/runs/' + str(current))
        require(run['head_sha'] == head and run['run_attempt'] == 1
            and run['event'] == 'workflow_dispatch' and run['status'] == 'in_progress'
            and run['path'] == '.github/workflows/kakeibo-production.yml',
            'auto_expense_reconciliation_run_changed')
        if apply:
            history=github.get('actions/workflows/kakeibo-production.yml/runs?event=workflow_dispatch&head_sha='+head+'&per_page=100')
            require(history['total_count']==len(history['workflow_runs'])<=100,
                    'auto_expense_reconciliation_run_inventory_incomplete')
            releases=[v for v in history['workflow_runs'] if v.get('display_title')==RELEASE_TITLE]
            require(len(releases)==1 and releases[0]['id']==current,
                    'auto_expense_reconciliation_dispatch_consumed')
        for status in ('in_progress','queued','waiting','pending','requested'):
            runs = github.get('actions/runs?status='+status+'&per_page=100')
            require(runs['total_count'] == len(runs['workflow_runs']) <= 100
                and all(v['id'] == current for v in runs['workflow_runs']),
                'auto_expense_reconciliation_competing_run')
        old = github.get('actions/runs/' + str(FAILED_RUN))
        require(all(old.get(k) == v for k,v in record['failed_run'].items()),
                'auto_expense_reconciliation_failed_run_changed')
        if saved:
            archived = github.get('actions/runs/' + str(SAVED_OPERATOR['id']))
            require(all(archived.get(k) == v for k,v in SAVED_OPERATOR.items())
                and archived['status'] == 'completed' and archived['conclusion'] == 'failure'
                and current != SAVED_OPERATOR['id'], 'auto_expense_reconciliation_failed_run_changed')
    guard()
    b = record['bindings']; sid = b['spreadsheet_id']
    require((env['SPREADSHEET_ID'],env['KAKEIBO_STATE_FOLDER_ID'],env['AUPAY_CARD_STATE_FILE_ID'],env['KAKEIBO_RUN_LEDGER_FILE_ID'])
        == (sid,b['folder_id'],b['card_id'],b['parent_id']), 'auto_expense_reconciliation_binding_changed')
    cb=StateBinding('au_pay_card_gmail',sid,b['folder_id'],b['card_id'])
    pb=StateBinding('production_run',sid,b['folder_id'],b['parent_id'])
    ro=read_only_drive_service(); sheets=read_only_sheets_service()
    card=ReadOnlyState(ro,cb); parent=ReadOnlyState(ro,pb)
    diagnostics.mark('state_revision')
    native_raw,np=card.observe(); parent_raw,pp=parent.observe()
    require(np == (SAVED_NATIVE_PROOF if saved else dict(sha256=record['native_before_sha256'],metadata_digest=record['native_before_metadata_digest'])),
            'auto_expense_reconciliation_native_changed')
    native=validate(native_raw,cb)
    require(native['phase']=='ready' and not native['attempt']
            and ((native['generation']==129 and ARCHIVE_RUN in summaries(native)) if saved else ARCHIVE_RUN not in summaries(native)),
            'auto_expense_reconciliation_no_replay')
    if saved:
        require(pp == {k: premises['parent'][k] for k in ('sha256','metadata_digest')},
                'auto_expense_reconciliation_parent_changed')
        require(ro.files().get(fileId=b['parent_id'],supportsAllDrives=True,fields='headRevisionId')
            .execute(num_retries=0).get('headRevisionId') == premises['parent']['revision'],
            'auto_expense_reconciliation_parent_changed')
        archive=summaries(native)[ARCHIVE_RUN]['operator_reconciliation']
        validate_saved_archive(record,archive)
    diagnostics.mark('bank_premises')
    if sha(parent_raw) != record['parent_before_sha256']:
        proof=collect_bank_premises(record,att['bank_manifest'],record['baseline_parent_metadata'],
            ro,github,datetime.now(timezone.utc),head=head,current_run_id=int(env['GITHUB_RUN_ID']),
            **({'saved_archive':archive} if saved else {}))
        require(proof['manifest']['parent_snapshot']['sha256']==sha(parent_raw),
                'auto_expense_reconciliation_parent_changed')
    else:
        require(pp['metadata_digest']==record['parent_before_metadata_digest'],
                'auto_expense_reconciliation_parent_changed')
        bank=record['other_native'][0]
        require(sha(ReadOnlyState(ro,StateBinding('bank_pdf_drive',sid,b['folder_id'],bank['file_id'])).read())==bank['sha256'],
                'auto_expense_reconciliation_bank_changed')
    from .production_ledger import validate_ledger
    ledger_value=validate_ledger(parent_raw,pb)
    require(all(v['phase']=='ready' and not v['attempt'] and not v['error']
        for k,v in ledger_value['sources'].items() if k!='auto_expense'), 'auto_expense_reconciliation_peer_pending')
    preserved_unknown_proofs(native,ledger_value,record['checkpoint'])
    documents=RollingProjectionStore(DriveProjectionStore(ro,b['projection_id'],sid,precreated=True))
    expected_native=[native_raw]
    def observe(*,parent_after=None):
        diagnostics.mark('financial_preflight')
        guard()
        observed_card, observed_np = card.observe()
        observed_parent, observed_pp = parent.observe()
        require(observed_card==expected_native[0] and observed_parent==(parent_after or parent_raw)
            and (not saved or (observed_np==SAVED_NATIVE_PROOF and (parent_after is not None or observed_pp==pp))),
                'auto_expense_reconciliation_snapshot_changed')
        if saved and parent_after is None:
            require(ro.files().get(fileId=b['parent_id'],supportsAllDrives=True,fields='headRevisionId')
                .execute(num_retries=0).get('headRevisionId')==premises['parent']['revision'],
                'auto_expense_reconciliation_parent_changed')
        tables=financial_data(sheets,sid,include_controls=True)
        require(digest({str(k):v for k,v in tables.items()})==record['financial_sha256'],
                'auto_expense_reconciliation_financial_changed')
        require(digest({k:documents.read(k) for k in record['protected_document_keys']})==record['documents_sha256'],
                'auto_expense_reconciliation_documents_changed')
        rules=sheets.spreadsheets().values().get(spreadsheetId=sid,range="'カテゴリ自動分類ルール'!A:R",valueRenderOption='UNFORMATTED_VALUE').execute(num_retries=0).get('values',[])
        require(digest(rules)==record['rules_sha256'],'auto_expense_reconciliation_settings_changed')
        acl={}
        for name,fid in [('card',b['card_id']),('parent',b['parent_id']),('folder',b['folder_id']),('ledger',sid)]:
            acl[name]=ro.files().get(fileId=fid,supportsAllDrives=True,fields='id,mimeType,trashed,owners(emailAddress,permissionId),permissions(id,type,role,emailAddress)').execute(num_retries=0)
        require(digest(acl)==record['acl_sha256'],'auto_expense_reconciliation_acl_changed')
        # Stable return value excludes our separately checked archive/parent transition.
        return dict(financial_sha256=record['financial_sha256'],documents_sha256=record['documents_sha256'],
                    rules_sha256=record['rules_sha256'],acl_sha256=record['acl_sha256'])
    if saved:
        def read_saved_archive():
            raw, proof = card.observe()
            require(raw==native_raw and proof==SAVED_NATIVE_PROOF,
                    'auto_expense_reconciliation_archive_unconfirmed')
            return summaries(validate(raw,cb))[ARCHIVE_RUN]['operator_reconciliation']
        # The native transport, temp state and persist_archive below are unreachable.
        return release_saved_archive(record=record,archive=archive,parent_raw=parent_raw,binding=pb,
            parent_transport=DriveStateTransport(drive_service(),pb),read_archive=read_saved_archive,
            observe=observe,guard=guard,expected_after_sha256=premises['parent_after_sha256'],diagnostics=diagnostics)
    with tempfile.TemporaryDirectory(prefix='operator-auto-expense-',dir=env.get('RUNNER_TEMP')) as temp:
        path=Path(temp)/'state'
        durable=None
        def save_archive(archive):
            nonlocal durable
            durable=DurableState(DriveStateTransport(drive_service(),cb),cb)
            require(durable.transport.read()==native_raw,'auto_expense_reconciliation_native_changed')
            expected_native[0]=persist_archive(durable,path,record,archive,guard)
        def read_archive():
            raw=card.read();require(raw==expected_native[0],'auto_expense_reconciliation_archive_unconfirmed')
            value=validate(raw,cb);preserved_unknown_proofs(value,ledger_value,record['checkpoint'])
            return summaries(value).get(ARCHIVE_RUN,{}).get('operator_reconciliation')
        transport=DriveStateTransport(drive_service() if apply else ro,pb)
        return release_with_archive(record=record,parent_raw=parent_raw,binding=pb,parent_transport=transport,
            save_archive=save_archive,read_archive=read_archive,observe=observe,guard=guard,apply=apply,
            operator_identity=dict(id=int(env['GITHUB_RUN_ID']),head_sha=head,run_attempt=1,
                event='workflow_dispatch',path='.github/workflows/kakeibo-production.yml'))
