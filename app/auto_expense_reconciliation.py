"""Fixed operator connection; existing durable store and release API.

Only the pinned missing-J contract may use the existing financial client.
No append, new database, recursive native archive or automatic resume.
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
POSTED_PREFIX = 'posted-v1:'
POSTED_LINKS_PREFIX = 'posted-links-v1:'
POSTED_LINKS_RECORD_SHA256 = '132d7187f00001b008cc9f69c308eab45cd96157f9790d7b2cf703c9054aefc1'
RESERVATION_PREFIX = 'reservation-v1:'
POSTED_OPERATION = 'confirm_posted_card_expenses'
POSTED_RECORD_SHA256 = '8922970a0c2f96079ea0692ff34334d4576cdc486e5a79f465f02a7b4d463754'
POSTED_SUMMARY = 'CARD-EXPENSE:RECONCILED:46083853434'
SAVED_OPERATION = 'release_saved_archive'
SAVED_ARCHIVE_SHA256 = '8d92edad186defce88564a787083d4b0f4d4f3bbe89001a93bbcf466cabeda2b'
SAVED_NATIVE_PROOF = dict(
    sha256='b828b3a1b784b205924641988d6b4b154bddbd79931468a5a1c14c2f57c3156f',
    metadata_digest='9315b4df16343b1325f298bcee85b2b2dd82d56f20ff63516f234213eec1dbe0')
SAVED_OPERATOR = dict(id=46076915652, head_sha='6453d12f66a0ed8a7879006d817bc21f7d3f9d87',
    run_attempt=1, event='workflow_dispatch', path='.github/workflows/kakeibo-production.yml')


class AutoExpenseReconciliationFailure(RuntimeError):
    def __init__(self, diagnostic):
        super().__init__(diagnostic['error_code'])  # Already allowlisted; preserve existing callers.
        self.diagnostic = diagnostic


def sha(raw): return hashlib.sha256(raw).hexdigest()


def reconciliation_settings(env):
    """Same existing operator fences for every maintenance operation."""
    require(all(env.get(k,default)==value for k,default,value in (
        ('CATEGORY_RULE_AUTO_APPLY_ENABLED','','true'),('KAKEIBO_AMAZON_MONEY_MODE','','confirmed-v1'),
        ('KAKEIBO_BANK_SCHEDULE_ENABLED','','true'),('KAKEIBO_LEGACY_DISABLED','','true'),
        ('KAKEIBO_SCHEDULE_ENABLED','','false'),('CATEGORY_BACKFILL_SHEET_RUNNER_ENABLED','','false'),
        ('KAKEIBO_CORE_SCHEDULE_ENABLED','false','false'),('KAKEIBO_CARD_SCHEDULE_ENABLED','false','false'))),
        'auto_expense_reconciliation_settings_changed')


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
    reconciliation_settings(env)
    tree = subprocess.check_output(['git','rev-parse','HEAD^{tree}'], text=True).strip()
    info = json.loads(env['GOOGLE_SERVICE_ACCOUNT_JSON'])
    key = serialization.load_pem_private_key(info['private_key'].encode(), password=None)
    if env['CARD_EXPENSE_RECONCILIATION'].startswith(RESERVATION_PREFIX):
        return _reservation_from_environment(env,apply=apply,diagnostics=diagnostics,head=head,tree=tree,key=key)
    if env['CARD_EXPENSE_RECONCILIATION'].startswith((POSTED_PREFIX,POSTED_LINKS_PREFIX)):
        return _posted_from_environment(env,apply=apply,diagnostics=diagnostics,head=head,tree=tree,key=key)
    diagnostics.mark('capsule_decode')
    record, att = decode_capsule(env['CARD_EXPENSE_RECONCILIATION'], key, tree,
        datetime.now(timezone.utc), record_sha256=RECORD_SHA256, label=LABEL, scope=SCOPE, max_ttl=900)
    saved = att.get('operation') == SAVED_OPERATION
    require(record['failed_run']['id'] == FAILED_RUN and record['financial_write_authority'] is False
        and att['main_sha'] == head and att['controls_sha256'] == record['settings_without_validated_sha256']
        and att['operation'] == (SAVED_OPERATION if saved and apply else 'release' if apply else 'preview')
        and 0 <= (datetime.now(timezone.utc)-datetime.fromisoformat(att['observed_at'])).total_seconds() <= 900,
        'auto_expense_reconciliation_record_changed')
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


def posted_spec(record):
    """Two reviewed contracts only; no arbitrary-record financial authority."""
    checksum=digest(record)
    if checksum==POSTED_RECORD_SHA256:return POSTED_SUMMARY,2,4531
    require(checksum==POSTED_LINKS_RECORD_SHA256 and record['financial_write_authority'] is False
        and len(record['missing_link_rows'])==10,'auto_expense_reconciliation_record_changed')
    return 'CARD-EXPENSE:RECONCILED:46132550495',18,64116


def posted_summary(record, updates, operator, recorded_at):
    name,count,amount=posted_spec(record)
    return dict(run_id=name,status='noop',source_window_start=record['checkpoint'],
        source_window_end=record['checkpoint'],failure=0,card_aliases=updates,
        expense_contract_sha256=record['business_contract'],expense_rows=count,expense_yen=amount,
        financial_before_sha256=record['financial_before_sha256'],financial_after_sha256=record['financial_sha256'],
        reconciliation_kind=POSTED_OPERATION,record_sha256=digest(record),
        failed_execution=record['failed_run'],replay_allowed=False,financial_write_requests=0,
        native_before_sha256=record['native_before_sha256'],operator=operator,recorded_at=recorded_at)


def posted_financial(record, financial, navigation):
    """Exact saved partial OR complete shape; projected J values grant no writes."""
    from .aupay_card_expense_stage import verify_delta
    _,count,_=posted_spec(record)
    actual=digest({str(k):v for k,v in financial.items()})
    complete=actual==record['financial_sha256']
    require(complete or actual==record.get('partial_financial_sha256'),
        'auto_expense_reconciliation_financial_changed')
    projected=deepcopy(financial)
    for render in ('UNFORMATTED_VALUE','FORMULA'):
        for row in record['plan']['rows']:
            current=projected[('取込データ',render)][row['row_num']-1]
            expected=row['after'] if complete or row['row_num'] not in record['missing_link_rows'] else row['before']
            from .aupay_card_aliases import import_row
            require(import_row(current)==expected,'auto_expense_reconciliation_financial_changed')
            value=list(row['after'])
            while value and value[-1]=='':value.pop()
            projected[('取込データ',render)][row['row_num']-1]=value
    before=deepcopy(projected)
    for render in ('UNFORMATTED_VALUE','FORMULA'):
        expenses=before[('支出明細',render)]
        require(len(expenses)==record['expense_before_rows']+count,'auto_expense_reconciliation_financial_changed')
        del expenses[-count:]
        for row in record['plan']['rows']:
            value=list(row['before'])
            while value and value[-1]=='':value.pop()
            before[('取込データ',render)][row['row_num']-1]=value
    require(digest({str(k):v for k,v in before.items()})==record['financial_before_sha256'],
        'auto_expense_reconciliation_financial_changed')
    verify_delta(before,projected,record['plan'],navigation=navigation)
    return complete,projected


def _posted_from_environment(env, *, apply, diagnostics, head, tree, key):
    """Reviewed posted expenses, fresh signature, bounded J links and state APIs."""
    from .aupay_card_aliases import LiteralRows
    from .aupay_card_reconciliation_entry import decode_capsule
    from .aupay_card_window_preview import GitHub,ReadOnlyState
    from .drive_run_state import StateBinding,DriveStateTransport,DurableState,validate
    from .production_ledger import validate_ledger
    from .google_clients import read_only_drive_service,read_only_sheets_service,drive_service
    from .aupay_card_bank_premises import collect_bank_premises,stamp
    from .aupay_card_first_window_apply import financial_data
    from .aupay_card_expense_stage import navigation_context,selected_plan,verify_delta,verify_side_effects
    from .sheets import SheetsDB
    from .projection_store import DriveProjectionStore
    from .projection_cache import RollingProjectionStore
    from .aupay_card_saved_readback import RepairReadbacks
    diagnostics.mark('capsule_decode')
    links=env['CARD_EXPENSE_RECONCILIATION'].startswith(POSTED_LINKS_PREFIX)
    prefix=POSTED_LINKS_PREFIX if links else POSTED_PREFIX
    record,att=decode_capsule(env['CARD_EXPENSE_RECONCILIATION'][len(prefix):],key,tree,
        datetime.now(timezone.utc),record_sha256=POSTED_LINKS_RECORD_SHA256 if links else POSTED_RECORD_SHA256,
        label=LABEL,scope=SCOPE,max_ttl=900)
    _,posted_count,_=posted_spec(record)
    observed=stamp(att['observed_at']);expires=stamp(att['approval']['expires_at'])
    require(att['operation']==(POSTED_OPERATION if apply else 'preview_posted_card_expenses')
        and att['main_sha']==head and att['controls_sha256']==record['settings_without_validated_sha256']
        and bool(att['approval'].get('approval_reference'))
        and 0<=(datetime.now(timezone.utc)-observed).total_seconds()<=900,
        'auto_expense_reconciliation_saved_approval_required')
    github=GitHub(env);current=int(env['GITHUB_RUN_ID']);deadline=time.monotonic()+900
    operator=dict(id=current,head_sha=head,run_attempt=1,event='workflow_dispatch',path='.github/workflows/kakeibo-production.yml')
    def guard():
        require(time.monotonic()<deadline and datetime.now(timezone.utc)<expires,
                'auto_expense_reconciliation_expired')
        require(github.get('git/ref/heads/main')['object']['sha']==head,'auto_expense_reconciliation_main_changed')
        run=github.get('actions/runs/'+str(current))
        require(all(run.get(k)==v for k,v in operator.items()) and run['status']=='in_progress'
            and stamp(run['created_at'])>=observed.replace(microsecond=0),
            'auto_expense_reconciliation_run_changed')
        history=github.get('actions/workflows/kakeibo-production.yml/runs?event=workflow_dispatch&head_sha='+head+'&per_page=100')
        require(history['total_count']==len(history['workflow_runs'])<=100,'auto_expense_reconciliation_run_inventory_incomplete')
        # A capsule is consumed by its first dispatch, including a prewrite crash.
        # A later explicit approval must bind a fresh observation and saved stage.
        title=RELEASE_TITLE if apply else RELEASE_TITLE.removesuffix('apply')+'preview'
        releases=[v for v in history['workflow_runs'] if v.get('display_title')==title
            and stamp(v['created_at'])>=observed.replace(microsecond=0)]
        require(len(releases)==1 and releases[0]['id']==current,'auto_expense_reconciliation_dispatch_consumed')
        for status in ('in_progress','queued','waiting','pending','requested'):
            active=github.get('actions/runs?status='+status+'&per_page=100')
            require(active['total_count']==len(active['workflow_runs'])<=100
                and all(v['id']==current for v in active['workflow_runs']),'auto_expense_reconciliation_competing_run')
        old=github.get('actions/runs/'+str(record['failed_run']['id']))
        require(all(old.get(k)==v for k,v in record['failed_run'].items()),'auto_expense_reconciliation_failed_run_changed')
    guard();b=record['bindings'];sid=b['spreadsheet_id']
    require((env['SPREADSHEET_ID'],env['KAKEIBO_STATE_FOLDER_ID'],env['AUPAY_CARD_STATE_FILE_ID'],env['KAKEIBO_RUN_LEDGER_FILE_ID'])
        ==(sid,b['folder_id'],b['card_id'],b['parent_id']),'auto_expense_reconciliation_binding_changed')
    cb=StateBinding('au_pay_card_gmail',sid,b['folder_id'],b['card_id'])
    pb=StateBinding('production_run',sid,b['folder_id'],b['parent_id'])
    ro=read_only_drive_service();sheets=read_only_sheets_service()
    card=ReadOnlyState(ro,cb);parent=ReadOnlyState(ro,pb)
    diagnostics.mark('state_revision');native_raw,np=card.observe();parent_raw,pp=parent.observe()
    require(att['state_snapshot']==dict(native=np,parent=pp),'auto_expense_reconciliation_snapshot_changed')
    stage,updates,saved=posted_state(record,validate(native_raw,cb))
    if stage=='pending':
        require(np==dict(sha256=record['native_before_sha256'],metadata_digest=record['native_before_metadata_digest']),
            'auto_expense_reconciliation_native_changed')
    if saved is not None:
        previous=github.get('actions/runs/'+str(saved['operator']['id']))
        require(all(previous.get(k)==v for k,v in saved['operator'].items())
            and previous['status']=='completed' and previous['conclusion'] in {'failure','success'}
            and stamp(previous['created_at'])<=stamp(saved['recorded_at'])<=stamp(previous['updated_at']),
            'auto_expense_reconciliation_failed_run_changed')
    original=base64.b64decode(record['parent_before_raw_b64'],validate=True)
    already_ready=stage=='native_ready' and parent_raw==proposed_release(original,pb,digest(saved))
    diagnostics.mark('bank_premises')
    if parent_raw==original:
        require(pp['metadata_digest']==record['parent_before_metadata_digest'],'auto_expense_reconciliation_parent_changed')
    elif not already_ready:
        proof=collect_bank_premises(record,att['bank_manifest'],record['baseline_parent_metadata'],ro,github,
            datetime.now(timezone.utc),head=head,current_run_id=current,posted_summary=saved)
        require(proof['manifest']['parent_snapshot']==pp,'auto_expense_reconciliation_parent_changed')
    if parent_raw==original or already_ready:
        bank=record['other_native'][0]
        require(sha(ReadOnlyState(ro,StateBinding('bank_pdf_drive',sid,b['folder_id'],bank['file_id'])).read())==bank['sha256'],
            'auto_expense_reconciliation_bank_changed')
    ledger_value=validate_ledger(parent_raw,pb)
    require(all(v['phase']=='ready' and not v['attempt'] and not v['error']
        for k,v in ledger_value['sources'].items() if k!='auto_expense'),'auto_expense_reconciliation_peer_pending')
    documents=RollingProjectionStore(DriveProjectionStore(ro,b['projection_id'],sid,precreated=True))
    db=LiteralRows(SheetsDB(sid,service=sheets))
    def observe():
        diagnostics.mark('financial_preflight');guard()
        financial=financial_data(sheets,sid,include_controls=True)
        docs={k:documents.read(k) for k in record['protected_document_keys']}
        require(digest(docs)==record['documents_sha256'],'auto_expense_reconciliation_documents_changed')
        nav=navigation_context(db)
        require(nav==record['navigation'],'auto_expense_reconciliation_snapshot_changed')
        complete,projected=posted_financial(record,financial,nav)
        before_docs=deepcopy(docs);before_docs['journal']=record['journal_before']
        require(digest(before_docs)==record['documents_before_sha256'],'auto_expense_reconciliation_documents_changed')
        verify_side_effects(dict(navigation=nav,documents=before_docs),dict(navigation=nav,documents=docs))
        class PlanningRows:
            def __getattr__(self,name):return getattr(db,name)
            def get(self,rng):
                if rng=='取込データ!A2:L':return projected[('取込データ','UNFORMATTED_VALUE')][1:]
                if rng=='支出明細!A2:M':return financial[('支出明細','UNFORMATTED_VALUE')][1:]
                return db.get(rng)
        plan=selected_plan(PlanningRows() if links else db,record['plan']['import_ids'],record['plan']['categories_by_id'],
            env.get('CATEGORY_RULE_AUTO_APPLY_ENABLED')=='true',
            record['business_contract'],expected_profile=record['profile_sha256'],env=env)
        require(not plan['rows'] and len(plan['already_posted'])==posted_count,'auto_expense_reconciliation_financial_changed')
        rules=db.get_raw("'カテゴリ自動分類ルール'!A:R")
        require(digest(rules)==record['rules_sha256'],'auto_expense_reconciliation_settings_changed')
        acl={name:ro.files().get(fileId=fid,supportsAllDrives=True,
            fields='id,mimeType,trashed,owners(emailAddress,permissionId),permissions(id,type,role,emailAddress)').execute(num_retries=0)
            for name,fid in [('card',b['card_id']),('parent',b['parent_id']),('folder',b['folder_id']),('ledger',sid)]}
        require(digest(acl)==record['acl_sha256'],'auto_expense_reconciliation_acl_changed')
        return dict(imports=projected[('取込データ','UNFORMATTED_VALUE')][1:],
            expenses=financial[('支出明細','UNFORMATTED_VALUE')][1:],financial_sha256=record['financial_sha256'],
            documents_sha256=record['documents_sha256'],profile_sha256=record['profile_sha256'],
            **(dict(links_complete=complete) if links else {}))
    initial=observe()
    from .aupay_card_aliases import merge_registry,resolve_aliases,validate_owner_expenses
    from .aupay_card_reservation_recovery import summaries
    from .aupay_card_incremental_recovery import preserved_unknown_proofs
    native=validate(native_raw,cb)
    registry=merge_registry(e for row in summaries(native).values() if row.get('status') in {'complete','noop'}
        for e in row.get('card_aliases',[]))
    merged=merge_registry((*registry,*updates))
    resolve_aliases([],initial['imports'],merged);validate_owner_expenses(merged,initial['expenses'])
    # Inspect historical proofs only after posted_state and exact finance checks.
    # This local view grants no readiness; real pending restore/guards stay denied.
    proof_view=deepcopy(native);proof_view.update(phase='ready',attempt='')
    preserved_unknown_proofs(proof_view,ledger_value,record['checkpoint'])
    require(card.observe()==(native_raw,np) and parent.observe()==(parent_raw,pp),
        'auto_expense_reconciliation_snapshot_changed')
    if not apply:
        return dict(success=True,mode='preview_posted',stage=stage,financial_write_requests=0,state_write_requests=0,
            **(dict(already_appended=posted_count,missing_links=0 if initial['links_complete'] else 10) if links else {}))
    if links:
        # This is the only financial operation in confirmation: fixed blank J
        # cells, never expense append/update, N or projection journal writes.
        def link_guard():
            diagnostics.mark('owner_links_preflight');guard()
            require(card.observe()==(native_raw,np) and parent.observe()==(parent_raw,pp),
                'auto_expense_reconciliation_snapshot_changed')
            require(observe()==initial,'auto_expense_reconciliation_snapshot_changed')
        if not initial['links_complete']:
            require(stage=='pending','auto_expense_reconciliation_target_changed')
            from .aupay_card_expense_stage import batch_owner_links
            from .google_clients import sheets_service
            # Full original source, N formulas, finance, documents, ACL, rules,
            # state revision and profile are rechecked immediately before send.
            link_guard()
            batch_owner_links(LiteralRows(SheetsDB(sid,service=sheets_service())),
                [r for r in record['plan']['rows'] if r['row_num'] in record['missing_link_rows']],
                guard=guard,diagnostics=diagnostics)
        final=observe()
        require(final['links_complete'] and card.observe()==(native_raw,np) and parent.observe()==(parent_raw,pp),
            'auto_expense_reconciliation_snapshot_changed')
    def completed_observation():
        value=observe()
        require(not links or value['links_complete'],'auto_expense_reconciliation_financial_changed')
        return value
    pins={prefix+'_before_'+field:value for prefix,proof in [('native',np),('parent',pp)]
        for field,value in proof.items()}
    readbacks=RepairReadbacks(card,parent,pins,guard=guard,deadline=deadline,diagnostics=diagnostics)
    with tempfile.TemporaryDirectory(prefix='operator-posted-expenses-',dir=env.get('RUNNER_TEMP')) as temp:
        result=confirm_posted_expenses(record=record,durable=DurableState(DriveStateTransport(drive_service(),cb),cb),
            ledger=ProductionLedger(DriveStateTransport(drive_service(),pb),pb),state_dir=Path(temp)/'state',
            repo_root=Path(__file__).resolve().parents[1],native_read=readbacks.sessions['native'].read,
            parent_read=readbacks.sessions['parent'].read,observe=completed_observation,guard=guard,operator=operator,
            diagnostics=diagnostics,readbacks=readbacks)
        if links:result.update(financial_write_requests=int(not initial['links_complete']),
            import_link_cells=10 if not initial['links_complete'] else 0,expense_rows_written=0)
        return result


def reservation_preflight(record, native_before, native_pending, parent_before, parent_pending):
    """Exact reservation-only transitions; no transport or financial authority."""
    from .aupay_card_incremental_recovery import validate_reservation_record,reservation_content,_rows,_hash
    from .aupay_card_window_preview import checkpoint
    validate_reservation_record(record)
    for name,before,pending in (('native',native_before,native_pending),('parent',parent_before,parent_pending)):
        require(_hash(before)==record[name+'_before']['sha256'] and _hash(pending)==record[name+'_pending']['sha256'],
            'reservation_snapshot_changed')
        expected=deepcopy(before);expected['generation']+=1
        if name=='native':
            checkpoint(before,record['checkpoint'])
            expected.update(phase='pending',attempt=record['native_pending']['attempt'])
        else:
            require(all(v['phase']=='ready' and not v['attempt'] and not v['error'] for v in before['sources'].values()),
                'reservation_peer_pending')
            expected['sources']['aupay_card'].update(phase='pending',attempt=record['parent_pending']['attempt'],error='')
        require(expected==pending,'reservation_content_changed')
    require(reservation_content(native_before)==record['tables']
        and _rows(native_before,'recurring.sqlite3','recurring_checkpoint')==record['checkpoint_row']
        and any(_hash(json.loads(r['summary_json']))==record['anchor_summary_sha256']
            for r in _rows(native_before,'recurring.sqlite3','recurring_runs')), 'reservation_content_changed')


def confirm_unused_reservation(*, record, native_before, native_pending, parent_before, parent_pending,
                               durable, ledger, native_read, parent_read, observe, guard, operator,
                               key, state_dir, diagnostics):
    """Existing two release APIs; atomically append one compact native receipt.

    A new independent approval is required for every invocation. An uncertain
    write stops immediately; a later approved invocation proves and skips it.
    No financial/review writer is supplied to this function.
    """
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding
    from .drive_run_state import validate,DurableState
    from .production_ledger import validate_ledger,_encode
    from .aupay_card_incremental_recovery import (_hash,_hash_bytes,_rows,RESERVATION_PROTOCOL,
        RESERVATION_PREFIX as ROW_PREFIX,verify_reservation_receipt,reservation_content)
    reservation_preflight(record,native_before,native_pending,parent_before,parent_pending)
    expected_parent=deepcopy(parent_pending);expected_parent['generation']+=1
    expected_parent['sources']['aupay_card'].update(phase='ready',attempt='',error='')
    parent_ready=_encode(expected_parent)
    diagnostics.mark('native_guard');guard();observe()
    raw=native_read();live=validate(raw,durable.binding)
    reservation_stage(record,live,validate_ledger(parent_read(),ledger.binding),native_before=native_before,
        native_pending=native_pending,parent_pending=parent_pending,public_key=key.public_key())
    receipts=[json.loads(r['summary_json'])['reservation_reconciliation']
        for r in _rows(live,'recurring.sqlite3','recurring_runs') if r['run_id'].startswith(ROW_PREFIX)
        and json.loads(r['summary_json']).get('reservation_reconciliation',{}).get('record',{}).get('failed_execution',{}).get('id')
            ==record['failed_execution']['id']]
    native_saves=parent_saves=0
    if live['phase']=='pending':
        require(not receipts and live==native_pending,'reservation_snapshot_changed')
        receipt=dict(protocol=RESERVATION_PROTOCOL,record=deepcopy(record),operator=deepcopy(operator),
            parent_ready_sha256=sha(parent_ready),parent_ready_card_sha256=_hash(expected_parent['sources']['aupay_card']))
        receipt['signature']=base64.b64encode(key.sign(_hash_bytes(receipt),
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()),salt_length=padding.PSS.MAX_LENGTH),hashes.SHA256())).decode()
        verify_reservation_receipt(receipt,key.public_key())
        require(parent_read()==_encode(parent_pending),'reservation_snapshot_changed')
        diagnostics.mark('native_save','native_reservation','write_possible')
        durable.release_after_reconciliation(observed_digest=sha(raw),evidence_reference=_hash(receipt),
            reconciliation_receipt=receipt,observe=lambda event:diagnostics.state_save('native',event))
        native_saves=1
        diagnostics.mark('native_independent_readback')
        require(native_read()==durable._loaded,'reservation_readback_failed')
        live=validate(durable._loaded,durable.binding)
    else:
        receipt=receipts[0]
    reservation_stage(record,live,validate_ledger(parent_read(),ledger.binding),native_before=native_before,
        native_pending=native_pending,parent_pending=parent_pending,public_key=key.public_key())
    guard();observe()
    # Restore from a genuinely new instance and directory, before parent release.
    fresh=DurableState(type('IndependentRead',(),{'read':lambda _:native_read()})(),durable.binding)
    fresh.restore(state_dir)
    require(fresh._loaded==native_read(),'reservation_readback_failed')
    diagnostics.mark('parent_guard');guard();observe()
    require(native_read()==fresh._loaded,'reservation_readback_failed')
    current=parent_read()
    require(current in (_encode(parent_pending),parent_ready),'reservation_snapshot_changed')
    if current!=parent_ready:
        require(ledger.payload==current,'reservation_snapshot_changed')
        diagnostics.mark('parent_completion','parent_completion','write_possible')
        ledger.release_after_reconciliation('aupay_card',observed_digest=sha(current),evidence_reference=_hash(receipt))
        parent_saves=1
    diagnostics.mark('final_readback');guard();observe()
    require(parent_read()==parent_ready and native_read()==fresh._loaded,'reservation_readback_failed')
    require(validate_ledger(parent_ready,ledger.binding)==expected_parent,'reservation_readback_failed')
    diagnostics.mark('complete','parent_completion','readback_confirmed')
    return dict(success=True,mode='confirm_unused_card_reservation',native_state_writes=native_saves,
        parent_state_writes=parent_saves,financial_write_requests=0,review_write_requests=0,
        checkpoint=record['checkpoint'])


def reservation_stage(record, live, parent, *, native_before, native_pending, parent_pending, public_key):
    """Shared Preview/Apply stage validation, with no persistence surface."""
    from .aupay_card_incremental_recovery import (_rows,_hash,RESERVATION_PREFIX as PREFIX,
        verify_reservation_receipt,reservation_content,reservation_summary)
    expected_parent=deepcopy(parent_pending);expected_parent['generation']+=1
    expected_parent['sources']['aupay_card'].update(phase='ready',attempt='',error='')
    require(parent in (parent_pending,expected_parent),'reservation_snapshot_changed')
    if live['phase']=='pending':
        require(live==native_pending and parent==parent_pending,'reservation_snapshot_changed')
        require(not any(json.loads(r['summary_json']).get('reservation_reconciliation',{}).get('record',{}).get('failed_execution',{}).get('id')
            ==record['failed_execution']['id'] for r in _rows(live,'recurring.sqlite3','recurring_runs')),'reservation_receipt_invalid')
        return 'reserved'
    summaries=[json.loads(r['summary_json']) for r in _rows(live,'recurring.sqlite3','recurring_runs')]
    receipts=[s['reservation_reconciliation'] for s in summaries if s.get('reservation_reconciliation',{}).get('record',{})==record]
    require(len(receipts)==1,'reservation_receipt_invalid');receipt=receipts[0]
    verify_reservation_receipt(receipt,public_key)
    require(not live['attempt'] and live['generation']==native_pending['generation']+1
        and reservation_content(live)==reservation_content(native_before)
        and receipt['parent_ready_sha256']==digest(expected_parent)
        and receipt['parent_ready_card_sha256']==digest(expected_parent['sources']['aupay_card'])
        and all(live['files'][f]==native_before['files'][f] for f in live['files'] if f!='recurring.sqlite3'),
        'reservation_content_changed')
    old=_rows(native_pending,'recurring.sqlite3','recurring_runs');new=_rows(live,'recurring.sqlite3','recurring_runs')
    added=[r for r in new if r['run_id']==PREFIX+str(receipt['operator']['id'])]
    require(len(added)==1 and len(new)==len(old)+1 and [r for r in new if r not in added]==old,
        'reservation_content_changed')
    require(reservation_summary(added[0])==receipt,'reservation_receipt_invalid')
    return 'ready' if parent==expected_parent else 'native_ready'


def _reservation_from_environment(env, *, apply, diagnostics, head, tree, key):
    """Existing hosted maintenance input, strict signed reservation-only scope."""
    from .aupay_card_reconciliation_entry import decode_capsule
    from .aupay_card_window_preview import GitHub,ReadOnlyState,CATCHUP_WINDOWS
    from .aupay_card_incremental_recovery import (validate_reservation_record,_hash,_rows,
        CATCHUP_NO_WRITE_NATIVE_SHA256,CATCHUP_CONTENT_ROOT,reservation_content_root,
        CATCHUP_NO_WRITE_CHECKPOINT,CATCHUP_NO_WRITE_FINANCIAL_SHA256,CATCHUP_NO_WRITE_DOCUMENTS_SHA256,
        preserved_unknown_proofs,reservation_receipts,RESERVATION_PREFIX as ROW_PREFIX)
    from .aupay_card_first_window_apply import financial_data
    from .aupay_card_bank_premises import stamp,_job_log
    from .drive_run_state import StateBinding,DurableState,DriveStateTransport,validate
    from .production_ledger import validate_ledger
    from .google_clients import read_only_drive_service,read_only_sheets_service,drive_service
    from .projection_store import DriveProjectionStore
    from .aupay_card_window_evidence import GetOnly
    diagnostics.mark('capsule_decode')
    now=datetime.now(timezone.utc)
    record,att=decode_capsule(env['CARD_EXPENSE_RECONCILIATION'][len(RESERVATION_PREFIX):],key,tree,now,
        label=LABEL,scope='card_unused_reservation_state_only',max_ttl=900,record_validator=validate_reservation_record)
    observed=stamp(att['observed_at']);expires=stamp(att['approval']['expires_at'])
    require(att['operation']==('confirm_unused_card_reservation' if apply else 'preview_unused_card_reservation')
        and att['main_sha']==head and att['financial_write_authority'] is False
        and bool(att['approval'].get('approval_reference')) and 0<=(now-observed).total_seconds()<=900,
        'reservation_approval_invalid')
    github=GitHub(env);current=int(env['GITHUB_RUN_ID']);deadline=time.monotonic()+900
    identity=github.get(f'actions/runs/{current}')
    operator={k:identity[k] for k in ('id','head_sha','head_branch','run_attempt','event','path','created_at')}
    def guard():
        require(time.monotonic()<deadline and datetime.now(timezone.utc)<expires,'auto_expense_reconciliation_expired')
        require(github.get('git/ref/heads/main')['object']['sha']==head,'auto_expense_reconciliation_main_changed')
        run=github.get(f'actions/runs/{current}')
        require(all(run.get(k)==v for k,v in operator.items()) and run['status']=='in_progress'
            and run['head_sha']==head and stamp(run['created_at'])>=observed.replace(microsecond=0)
            and current!=record['failed_execution']['id'],'auto_expense_reconciliation_run_changed')
        history=github.get('actions/workflows/kakeibo-production.yml/runs?event=workflow_dispatch&head_sha='+head+'&per_page=100')
        require(history['total_count']==len(history['workflow_runs'])<=100,'auto_expense_reconciliation_run_inventory_incomplete')
        title=RELEASE_TITLE if apply else RELEASE_TITLE.removesuffix('apply')+'preview'
        matching=[v for v in history['workflow_runs'] if v.get('display_title')==title
            and stamp(v['created_at'])>=observed.replace(microsecond=0)]
        require(len(matching)==1 and matching[0]['id']==current,'auto_expense_reconciliation_dispatch_consumed')
        for status in ('in_progress','queued','waiting','pending','requested'):
            active=github.get('actions/runs?status='+status+'&per_page=100')
            require(active['total_count']==len(active['workflow_runs'])<=100
                and all(v['id']==current for v in active['workflow_runs']),'auto_expense_reconciliation_competing_run')
    guard();b=record['bindings'];sid=b['spreadsheet_id']
    from .private_state_bindings import unwrap
    projection_id=unwrap('KAKEIBO_PROJECTION_FOLDER_ID',env['KAKEIBO_PROJECTION_FOLDER_ID'],
        json.loads(env['GOOGLE_SERVICE_ACCOUNT_JSON'])['private_key'])
    require(tuple(env[n] for n in ('SPREADSHEET_ID','KAKEIBO_STATE_FOLDER_ID','AUPAY_CARD_STATE_FILE_ID',
        'KAKEIBO_RUN_LEDGER_FILE_ID'))==tuple(b[n] for n in
        ('spreadsheet_id','folder_id','card_id','parent_id')) and projection_id==b['projection_id'],
        'auto_expense_reconciliation_binding_changed')
    ro=read_only_drive_service();sheets=GetOnly(read_only_sheets_service(),deadline)
    cb=StateBinding('au_pay_card_gmail',sid,b['folder_id'],b['card_id']);pb=StateBinding('production_run',sid,b['folder_id'],b['parent_id'])
    card=ReadOnlyState(GetOnly(ro,deadline),cb);parent=ReadOnlyState(GetOnly(ro,deadline),pb)
    diagnostics.mark('state_revision');nr,np=card.observe();pr,pp=parent.observe()
    require(att['state_snapshot']==dict(native=np,parent=pp),'reservation_snapshot_changed')
    failure=github.get('actions/runs/'+str(record['failed_execution']['id']))
    require(all(failure.get(k)==v for k,v in record['failed_execution'].items()),'reservation_failure_unproven')
    comparison=github.get(f"compare/{failure['head_sha']}...{head}")
    require(comparison['status'] in {'ahead','identical'} and comparison['behind_by']==0,'reservation_failure_unproven')
    jobs=github.get('actions/runs/'+str(failure['id'])+'/jobs?per_page=100')
    require(jobs['total_count']==len(jobs['jobs'])==1 and jobs['jobs'][0]['id']==record['failed_job_id']
        and jobs['jobs'][0]['conclusion']=='failure' and jobs['jobs'][0]['status']=='completed', 'reservation_failure_unproven')
    def revisions(fid):
        refs=[];token=None
        for _ in range(32):
            guard()
            page=ro.revisions().list(fileId=fid,pageSize=1000,pageToken=token,
                fields='nextPageToken,revisions(id,modifiedTime,size,md5Checksum)').execute(num_retries=0)
            refs.extend(page.get('revisions',[]));token=page.get('nextPageToken')
            if not token:break
        require(not token and len({v['id'] for v in refs})==len(refs),'reservation_revision_unproven')
        return refs
    snapshots={}
    for label,binding in (('native',cb),('parent',pb)):
        refs=revisions(binding.file_id)
        positions=[]
        for stage in ('before','pending'):
            node=record[label+'_'+stage];matches=[i for i,v in enumerate(refs) if v==node['revision']]
            require(len(matches)==1,'reservation_revision_unproven');positions.append(matches[0])
            raw=ro.revisions().get_media(fileId=binding.file_id,revisionId=node['revision']['id']).execute(num_retries=0)
            require(sha(raw)==node['sha256'] and str(len(raw))==str(node['revision']['size'])
                and hashlib.md5(raw).hexdigest()==node['revision']['md5Checksum'],'reservation_revision_unproven')
            snapshots[label+'_'+stage]=(validate(raw,binding) if label=='native' else validate_ledger(raw,binding))
        require(positions[1]==positions[0]+1 and len(refs)-positions[1] in (1,2),'reservation_revision_unproven')
        require(stamp(record[label+'_before']['revision']['modifiedTime'])<stamp(failure['created_at']),
            'reservation_revision_unproven')
        require(stamp(failure['created_at'])<=stamp(record[label+'_pending']['revision']['modifiedTime'])<=stamp(failure['updated_at']),
            'reservation_revision_unproven')
    reservation_preflight(record,**snapshots)
    docs=DriveProjectionStore(GetOnly(ro,deadline),b['projection_id'],sid,precreated=True)
    def observe():
        diagnostics.mark('financial_preflight');guard()
        require(_hash({t+'|'+v:rows for (t,v),rows in financial_data(sheets,sid,include_controls=True).items()})==record['financial_sha256'],
            'reservation_financial_changed')
        protected={k:docs.read(k) for k in record['protected_heads']}
        require(_hash(protected)==record['documents_sha256']
            and all(i not in (protected['money'] or {}).get('records',{}) for i in record['money_ids']), 'reservation_documents_changed')
        for name,node in record['protected_heads'].items():
            require(docs._file(name)==node['file_id'],'reservation_documents_changed')
            refs=revisions(node['file_id'])
            require(refs and refs[-1]==node['revision'] and stamp(refs[-1]['modifiedTime'])<stamp(failure['created_at']),
                'reservation_revision_unproven')
        acl={name:ro.files().get(fileId=fid,supportsAllDrives=True,
            fields='id,mimeType,trashed,owners(emailAddress,permissionId),permissions(id,type,role,emailAddress)').execute(num_retries=0)
            for name,fid in [('card',b['card_id']),('parent',b['parent_id']),('folder',b['folder_id']),('ledger',sid)]}
        require(_hash(acl)==record['acl_sha256'],'reservation_acl_changed')
    observe()
    if record['evidence_mode']=='legacy_c2':
        require(record['checkpoint']==CATCHUP_NO_WRITE_CHECKPOINT
            and record['native_before']['sha256']==CATCHUP_NO_WRITE_NATIVE_SHA256
            and reservation_content_root(snapshots['native_before'])==CATCHUP_CONTENT_ROOT
            and record['financial_sha256']==CATCHUP_NO_WRITE_FINANCIAL_SHA256
            and record['documents_sha256']==CATCHUP_NO_WRITE_DOCUMENTS_SHA256
            and record['source_contract_sha256']==CATCHUP_WINDOWS.get((record['checkpoint'],record['window_end']))
            and len(record['money_ids'])>0,'reservation_failure_unproven')
        # The legacy path stopped before its mandatory money-review barrier.
        # Bind the reviewed control-flow version, without adding a failed run ID.
        require(failure['head_sha']=='38fb7e97d43ec4e978e6cfce971558c0546195ce','reservation_failure_unproven')
    else:
        verify_reservation_diagnostic(_job_log(github,record['failed_job_id']),record)
    # Validate all earlier proof rows using a local ready view, never a save.
    before=snapshots['native_before'];parent_before=snapshots['parent_before']
    preserved_unknown_proofs(before,parent_before,record['checkpoint'])
    prior=reservation_receipts(before,parent_before,record['checkpoint'],public_key=key.public_key())
    require(record['previous_receipt_sha256']==(_hash(prior[-1]) if prior else ''),'reservation_receipt_chain_changed')
    reservation_stage(record,validate(nr,cb),validate_ledger(pr,pb),native_before=before,
        native_pending=snapshots['native_pending'],parent_pending=snapshots['parent_pending'],public_key=key.public_key())
    if validate(nr,cb)['phase']=='ready':
        saved=[json.loads(r['summary_json'])['reservation_reconciliation'] for r in _rows(validate(nr,cb),'recurring.sqlite3','recurring_runs')
            if json.loads(r['summary_json']).get('reservation_reconciliation',{}).get('record',{})==record][0]
        previous=github.get('actions/runs/'+str(saved['operator']['id']))
        require(all(previous.get(k)==v for k,v in saved['operator'].items()) and previous['status']=='completed'
            and previous['conclusion'] in {'success','failure'} and previous['id']!=current,
            'reservation_failure_unproven')
    require(card.observe()==(nr,np) and parent.observe()==(pr,pp),'reservation_snapshot_changed')
    if not apply:return dict(success=True,mode='preview_unused_card_reservation',financial_write_requests=0,state_write_requests=0)
    with tempfile.TemporaryDirectory(prefix='card-unused-operator-',dir=env.get('RUNNER_TEMP')) as temp:
        return confirm_unused_reservation(record=record,**snapshots,
            durable=DurableState(DriveStateTransport(drive_service(),cb),cb),
            ledger=ProductionLedger(DriveStateTransport(drive_service(),pb),pb),native_read=card.read,parent_read=parent.read,
            observe=observe,guard=guard,operator=operator,key=key,state_dir=Path(temp)/'state',diagnostics=diagnostics)


def verify_reservation_diagnostic(log, record):
    """Only completed, reviewed prewrite diagnostics; missing/unknown denies."""
    values=[]
    for line in log.splitlines():
        start=line.find('{')
        if start<0:continue
        try:values.append(json.loads(line[start:]))
        except (ValueError,TypeError):pass
    expected=dict(native_before=record['native_before']['sha256'],parent_before=record['parent_before']['sha256'],
        checkpoint=record['checkpoint'],window_end=record['window_end'],source_contract=record['source_contract_sha256'],
        financial=record['financial_sha256'],documents=record['documents_sha256'])
    matches=[v for v in values if v.get('error')=='incremental_apply_stopped_no_retry'
        and v.get('reservation_evidence')==expected and v.get('diagnostic',{}).get('effects',{}).get('financial')=='not_reached'
        and v.get('diagnostic',{}).get('effects',{}).get('money','not_started')=='not_started'
        and v.get('diagnostic',{}).get('effects',{}).get('native_reservation') in {'write_possible','readback_confirmed'}
        and v.get('diagnostic',{}).get('last_stage') in
            {'native_write','native_transport_readback','native_reserved','money_guard','money_preflight'}]
    require(len(matches)==1,'reservation_failure_unproven')


def posted_state(record, native):
    """Recognize only the three saved boundaries; never manufacture readiness."""
    from .aupay_card_incremental_recovery import _rows
    from .aupay_card_reservation_recovery import summaries
    from .aupay_card_aliases import merge_registry
    from .aupay_card_expense_stage import owner_updates,contract_sha256
    name,count,amount=posted_spec(record)
    require(record['financial_write_authority'] is False,
            'auto_expense_reconciliation_record_changed')
    require(contract_sha256(record['plan'])==record['business_contract']
        and (record['plan']['expense_rows'],record['plan']['expense_yen'])==(count,amount),
        'auto_expense_reconciliation_record_changed')
    rows=_rows(native,'recurring.sqlite3','recurring_runs');saved=summaries(native).get(name)
    old=[r for r in rows if r['run_id']!=name]
    require(digest(old)==record['history_sha256']
        and digest(_rows(native,'recurring.sqlite3','recurring_checkpoint'))==record['checkpoint_sha256']
        and set(native['files'])==set(record['native_file_hashes'])
        and all(v['sha256']==record['native_file_hashes'][k] for k,v in native['files'].items()
                if k!='recurring.sqlite3' or saved is None),'auto_expense_reconciliation_native_changed')
    registry=merge_registry(e for r in old if r['status'] in {'complete','noop'}
        for e in json.loads(r['summary_json']).get('card_aliases',[]))
    updates=owner_updates(record['plan'],registry)
    require(digest(updates)==record['owner_updates_sha256'],'auto_expense_reconciliation_record_changed')
    if saved is None:
        require(native['generation']==record['native_generation'] and native['phase']=='pending'
            and native['attempt']==record['native_attempt'],'auto_expense_reconciliation_native_changed')
        return 'pending',updates,None
    validate_posted_summary(record,saved,updates=updates)
    offset=native['generation']-record['native_generation']
    require((offset,native['phase'],native['attempt']) in
        {(1,'pending',record['native_attempt']),(2,'ready','')},'auto_expense_reconciliation_native_changed')
    return ('proof_saved' if offset==1 else 'native_ready'),updates,saved


def validate_posted_summary(record, saved, *, updates=None):
    posted_spec(record)
    updates=saved['card_aliases'] if updates is None else updates
    operator=saved['operator'];stamp=datetime.fromisoformat(saved['recorded_at'])
    require(digest(updates)==record['owner_updates_sha256'] and stamp.tzinfo is not None
        and set(operator)=={'id','head_sha','run_attempt','event','path'} and type(operator['id']) is int
        and operator['id']!=record['failed_run']['id'] and len(operator['head_sha'])==40
        and operator['run_attempt']==1 and operator['event']=='workflow_dispatch'
        and operator['path']=='.github/workflows/kakeibo-production.yml'
        and saved==posted_summary(record,updates,operator,saved['recorded_at']),
        'auto_expense_reconciliation_archive_unconfirmed')


def confirm_posted_expenses(*, record, durable, ledger, state_dir, repo_root, native_read,
                            parent_read, observe, guard, operator, diagnostics, readbacks=None):
    """Existing state APIs only. Saved stages are inspected and never repeated."""
    from .drive_run_state import DurableState,validate,envelope,snapshot
    from .aupay_card_recurring import SqliteRecurringRunState
    from .aupay_card_reconciliation_apply import _pending_cache
    from .aupay_card_aliases import merge_registry,resolve_aliases,validate_owner_expenses
    from .aupay_card_incremental_recovery import preserved_unknown_proofs
    current=native_read();native=validate(current,durable.binding);stage,updates,saved=posted_state(record,native)
    if stage=='pending':require(sha(current)==record['native_before_sha256'],'auto_expense_reconciliation_native_changed')
    parent_raw=parent_read();item=json.loads(parent_raw)['sources']['auto_expense']
    original=json.loads(base64.b64decode(record['parent_before_raw_b64'],validate=True))['sources']['auto_expense']
    closed=dict(original,phase='ready',attempt='',error='')
    require(item==original or stage=='native_ready' and item==closed,'auto_expense_reconciliation_target_changed')
    initial=observe();guard()
    def verify_owners():
        financial=observe();require(financial==initial,'auto_expense_reconciliation_snapshot_changed')
        from .aupay_card_reservation_recovery import summaries
        registry=[e for row in summaries(validate(current,durable.binding)).values()
            if row.get('status') in {'complete','noop'} for e in row.get('card_aliases',[])]
        merged=merge_registry((*registry,*updates))
        resolve_aliases([],financial['imports'],merged);validate_owner_expenses(merged,financial['expenses'])
    def ticket(scope,before):return readbacks.begin(scope,before) if readbacks is not None else None
    def readback(t,read,expected):
        if t is not None:t.verify(expected)
        require(read()==expected,'auto_expense_reconciliation_readback_failed')
    verify_owners()
    if stage!='native_ready':
        _pending_cache(native,state_dir,repo_root)
        state=SqliteRecurringRunState(Path(state_dir)/'recurring.sqlite3',repo_root=repo_root)
        require(state.successful_window_end().isoformat()==record['checkpoint'],'auto_expense_reconciliation_checkpoint_changed')
        durable._pending=current  # Existing explicit reconciliation handoff; restore() remains fail-closed.
        if stage=='pending':
            saved=posted_summary(record,updates,operator,datetime.now(timezone.utc).isoformat())
            state.record(saved,advance_checkpoint=False)
            proposed=envelope(durable.binding,snapshot(Path(state_dir),durable.binding),
                generation=native['generation']+1,phase='pending',attempt=native['attempt'])
            posted_state(record,validate(proposed,durable.binding))
            diagnostics.mark('native_guard');guard();verify_owners()
            require(parent_read()==parent_raw,'auto_expense_reconciliation_parent_changed')
            t=ticket('native',current);diagnostics.mark('native_save','native_reservation','write_possible')
            durable._replace(current,proposed);readback(t,native_read,proposed)
            durable._pending=proposed;current=proposed
            diagnostics.mark('native_independent_readback','native_reservation','readback_confirmed')
        diagnostics.mark('checkpoint_guard');guard();verify_owners()
        require(parent_read()==parent_raw,'auto_expense_reconciliation_parent_changed')
        t=ticket('native',current);diagnostics.mark('checkpoint_commit','checkpoint','write_possible')
        durable.commit(Path(state_dir));current=durable._loaded;readback(t,native_read,current)
        diagnostics.mark('checkpoint_independent_readback','checkpoint','readback_confirmed')
        require(posted_state(record,validate(current,durable.binding))[0]=='native_ready',
                'auto_expense_reconciliation_native_changed')
    # An actual fresh restore, independent of the pending materialization.
    fresh_dir=Path(state_dir).parent/'fresh-ready'
    fresh=DurableState(durable.transport,durable.binding)
    fresh.restore(fresh_dir)
    restored=SqliteRecurringRunState(fresh_dir/'recurring.sqlite3',repo_root=repo_root)
    require(snapshot(fresh_dir,durable.binding)==validate(current,durable.binding)['files']
        and restored.successful_window_end().isoformat()==record['checkpoint'],
        'auto_expense_reconciliation_checkpoint_changed')
    resolve_aliases([],initial['imports'],restored.alias_registry())
    validate_owner_expenses(restored.alias_registry(),initial['expenses'])
    verify_owners();guard()
    preserved_unknown_proofs(validate(current,durable.binding),json.loads(parent_raw),record['checkpoint'])
    if item==original:
        require(parent_read()==parent_raw,'auto_expense_reconciliation_parent_changed')
        expected=proposed_release(parent_raw,ledger.binding,digest(saved))
        from .bank_pending_recovery import ReleaseOnlyTransport
        target=ProductionLedger(ReleaseOnlyTransport(ledger.transport,sha(expected)),ledger.binding)
        t=ticket('parent',parent_raw);guard();diagnostics.mark('parent_completion','parent_completion','write_possible')
        target.release_after_reconciliation('auto_expense',observed_digest=sha(parent_raw),evidence_reference=digest(saved))
        readback(t,parent_read,expected);parent_raw=expected
    diagnostics.mark('final_readback');guard();verify_owners()
    require(native_read()==current and parent_read()==parent_raw,'auto_expense_reconciliation_snapshot_changed')
    preserved_unknown_proofs(validate(current,durable.binding),json.loads(parent_raw),record['checkpoint'])
    diagnostics.mark('complete','parent_completion','readback_confirmed')
    return dict(success=True,mode='confirmed_posted_stop',initial_stage=stage,
        reconciled_expenses=posted_spec(record)[1],financial_write_requests=0,archive_write_requests=0,checkpoint=record['checkpoint'],
        native_after_sha256=sha(current),parent_after_sha256=sha(parent_raw))
