"""One owner-approved fixed past bank plan, using existing durable bank writers.

Never edits standing authority, its window/checkpoint, existing imports, or PDFs.
The private plan is fetched by wrapped ID; only its commitment is an approval.
"""
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
from uuid import uuid4

from .bank_pdf_pipeline import BankPdfPipeline
from .bank_reconciliation import build_bank_shadow_result
from .bank_income import deposit_decisions, income_id, BankIncomePipeline
from .auto_expense import expense_id
from .canonical_import import materialize_import_row
from .reconciliation import parse_import_rows

KINDS = ('income', 'expense', 'nonposting', 'hold')
OPERATIONS = ('append_import', 'append_income', 'append_expense', 'update_new_import')
CORE_FILES = ('bank_pdf_pipeline.py', 'bank_reconciliation.py', 'bank_income.py',
              'bank_meaning_resolver.py', 'bank_review_decisions.py', 'bank_finalization.py')
_GRANT = object()


@dataclass(frozen=True, init=False)
class _Grant:
    plan: str
    expires: datetime
    token: object


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=True, sort_keys=True).encode()).hexdigest()


def core_digest():
    root = Path(__file__).parent
    return digest({n: hashlib.sha256((root / n).read_bytes().replace(b'\r\n', b'\n')).hexdigest() for n in CORE_FILES})


def rule_digest(rules):
    return digest({k: sorted(v) for k, v in rules.items()})


def decision_record(tx, pdf, classification, deposit):
    c = classification
    disposition = 'hold'
    if tx.signed_amount > 0:
        if deposit.outcome == 'confirmed_income':
            disposition = 'income'
        elif deposit.outcome in {'transfer', 'reimbursement', 'other_non_income'}:
            disposition = 'nonposting'
    elif c.classification.classification == 'expense' and c.write_eligibility == 'preview_candidate':
        disposition = 'expense'
    elif c.classification.classification in {'transfer', 'card_settlement', 'cash_withdrawal', 'reimbursement', 'other_nonwrite'}:
        disposition = 'nonposting'
    return dict(pdf=pdf, transaction=asdict(tx), disposition=disposition,
                classification=c.classification.classification, reason=c.classification.reason,
                deposit_outcome=deposit.outcome if deposit else None,
                deposit_reason=deposit.reason if deposit else None)


def make_plan(report):
    """Owner-local only. No approval or write is created by freezing a report."""
    limit = min(100, report['limit'])
    ordered = sorted(report['items'], key=lambda i: (KINDS.index(report['items'][i]['disposition']), i))
    plan = dict(schema=1, kind='one_time_fixed_bank_backfill', plan_id=str(uuid4()),
                target=report['target'], folder=report['folder'], pdfs=report['pdfs'],
                items=report['items'], protected=report['protected'], limit=limit,
                policy_sha256=report['policy_sha256'], rules_sha256=report['rules_sha256'],
                master_sha256=report['master_sha256'], code_sha256=core_digest(),
                batches=[ordered[i:i + limit] for i in range(0, len(ordered), limit)])
    validate_plan(plan)
    return plan


def validate_plan(plan):
    expected = {'schema', 'kind', 'plan_id', 'target', 'folder', 'pdfs', 'items', 'protected',
                'limit', 'policy_sha256', 'rules_sha256', 'master_sha256', 'code_sha256', 'batches'}
    if (set(plan) != expected or plan['schema'] != 1 or plan['kind'] != 'one_time_fixed_bank_backfill'
            or type(plan['limit']) is not int or not 1 <= plan['limit'] <= 100
            or not plan['items'] or not plan['pdfs'] or not plan['protected']
            or set(plan['items']) & set(plan['protected'])):
        raise ValueError('fixed_bank_plan_invalid')
    all_ids = set(plan['items']) | set(plan['protected'])
    pdf_ids = [p['id'] for p in plan['pdfs']]
    pdf_members = [i for p in plan['pdfs'] for i in p['identities']]
    batches = [i for batch in plan['batches'] for i in batch]
    if (len(pdf_ids) != len(set(pdf_ids)) or set(pdf_members) != all_ids
            or len(pdf_members) != len(set(pdf_members))
            or set(batches) != set(plan['items']) or len(batches) != len(set(batches))
            or any(not b or len(b) > plan['limit'] for b in plan['batches'])
            or any(v['disposition'] not in KINDS or v['pdf'] not in pdf_ids
                   or v['transaction']['source_row_identity'] != i for i, v in plan['items'].items())):
        raise ValueError('fixed_bank_membership_invalid')


def batch_ids(plan, batch):
    validate_plan(plan)
    if type(batch) is not int or not 0 <= batch < len(plan['batches']):
        raise ValueError('fixed_bank_batch_invalid')
    return tuple(plan['batches'][batch])


def authorize(plan, permit, policy, env, head, now):
    """A separate short-lived commitment, never a modified recurring policy."""
    from .production_flow import verify_execution_boundary
    verify_execution_boundary(env, head)
    owner = env.get('GITHUB_REPOSITORY_OWNER')
    if (not owner or env.get('GITHUB_ACTOR') != owner or env.get('GITHUB_TRIGGERING_ACTOR') != owner
            or env.get('GITHUB_EVENT_NAME') != 'workflow_dispatch'
            or env.get('GITHUB_WORKFLOW_REF') != 'tmoriuchi1401-source/kakeibo-ai/.github/workflows/kakeibo-production.yml@refs/heads/main'
            or env.get('KAKEIBO_SCHEDULE_ENABLED') != 'false'
            or env.get('BANK_INCOME_WRITE_ENABLED') != 'true'
            or env.get('BANK_REVIEW_ENABLED') != 'true'):
        raise ValueError('fixed_bank_owner_main_lock_required')
    validate_plan(plan)
    policy.validate()
    if (set(permit) != {'schema', 'kind', 'plan_sha256', 'expected_main_sha', 'expires_at', 'operations'}
            or permit['schema'] != 1 or permit['kind'] != plan['kind']
            or permit['plan_sha256'] != digest(plan) or permit['expected_main_sha'] != head
            or set(permit['operations']) != set(OPERATIONS)
            or not now < datetime.fromisoformat(permit['expires_at'])
            or not policy.valid_from <= now < policy.expires_at
            or plan['target'] != policy.expected_spreadsheet_id or plan['folder'] != policy.expected_drive_folder_id
            or plan['limit'] > policy.max_rows or len(plan['pdfs']) > policy.max_files
            or not policy.income_enabled):
        raise ValueError('fixed_bank_one_time_permission_required')
    grant = object.__new__(_Grant)
    object.__setattr__(grant, 'plan', digest(plan))
    object.__setattr__(grant, 'expires', datetime.fromisoformat(permit['expires_at']))
    object.__setattr__(grant, 'token', _GRANT)
    return grant


RANGES = {'取込データ': 'A2:L', '収入明細': 'A2:J', '支出明細': 'A2:M'}
WIDTHS = {'取込データ': 12, '収入明細': 10, '支出明細': 13}


def normal_row(sheet, row):
    result = (list(row) + [''] * WIDTHS[sheet])[:WIDTHS[sheet]]
    amount = {'取込データ': 6, '収入明細': 2, '支出明細': 4}[sheet]
    result[amount] = int(str(result[amount]).replace(',', ''))
    return result


def snapshot(db):
    result = {}
    for name, span in RANGES.items():
        rows = db.get(name + '!' + span)
        records = {}
        for row in rows:
            if row and row[0]:
                key = str(row[0])
                if key in records:
                    raise ValueError('fixed_bank_duplicate_ledger_id')
                records[key] = normal_row(name, row)
        result[name] = records
    return result


def require_rows(db, rows):
    actual = snapshot(db)
    for sheet, expected in rows.items():
        for identity, row in expected.items():
            if actual[sheet].get(identity) != row:
                raise ValueError('fixed_bank_exact_readback_mismatch')


def validate_live(plan, db, pdf_bytes, policy_sha256, rules, resolver):
    """Full PDFs/493 membership checked even when only a subset is selected."""
    validate_plan(plan)
    if (policy_sha256 != plan['policy_sha256'] or core_digest() != plan['code_sha256']
            or rule_digest(rules) != plan['rules_sha256'] or resolver is None
            or resolver.master.fingerprint != plan['master_sha256']):
        raise ValueError('fixed_bank_configuration_changed')
    resolver.require_unchanged()
    actual = snapshot(db)
    imports = db.get('取込データ!A2:L')
    original = {str(r[0]): r for r in imports if r and r[0]}
    if any(i not in original or digest(original[i]) != h for i, h in plan['protected'].items()):
        raise ValueError('fixed_bank_protected_import_changed')
    # Prior completed batches remain visible for read-back but cannot change the
    # frozen classification by becoming "existing_duplicate" in the planner.
    classification_imports = [r for r in imports if r and str(r[0]) not in plan['items']]
    seen = set()
    transactions = {}
    with tempfile.TemporaryDirectory() as folder:
        for n, pdf in enumerate(plan['pdfs']):
            content = pdf_bytes(pdf['id'])
            if hashlib.sha256(content).hexdigest() != pdf['sha256']:
                raise ValueError('fixed_bank_pdf_changed')
            path = Path(folder) / (str(n) + '.pdf')
            path.write_bytes(content)
            parsed = BankPdfPipeline().parse(path, existing_identities=set(plan['protected']))
            if parsed.issues or parsed.balance_consistency_failures:
                raise ValueError('fixed_bank_pdf_unresolved')
            identities = [tx.source_row_identity for tx in parsed.transactions]
            if sorted(identities) != pdf['identities'] or seen & set(identities):
                raise ValueError('fixed_bank_pdf_membership_changed')
            seen.update(identities)
            opts = dict(rules, meaning_resolver=resolver)
            shadow = build_bank_shadow_result(parsed, parse_import_rows(classification_imports), **opts)
            classes = {x.classification.transaction.source_row_identity: x for x in shadow.decisions}
            deposits = {x.transaction.source_row_identity: x for x in deposit_decisions(parsed.transactions, **opts)[0]}
            for tx in parsed.transactions:
                i = tx.source_row_identity
                transactions[i] = tx
                if i in plan['items'] and decision_record(tx, pdf['id'], classes[i], deposits.get(i)) != plan['items'][i]:
                    raise ValueError('fixed_bank_classification_changed')
    if seen != set(plan['items']) | set(plan['protected']):
        raise ValueError('fixed_bank_target_set_changed')
    return actual, transactions


class FixedWriteDB:
    """Constrain the existing writers to new rows of the selected fixed batch."""
    def __init__(self, db, selected, imports, incomes, guard):
        self.db, self.sid, self.selected = db, db.sid, set(selected)
        self.imports, self.incomes, self.guard = imports, incomes, guard

    def get(self, rng): return self.db.get(rng)
    def sheet_titles(self): return self.db.sheet_titles()
    def categories(self): return self.db.categories()

    def ensure_expense_status_column(self):
        from .sheets import HEADERS
        if self.db.get('支出明細!A1:M1') != [HEADERS['支出明細']]:
            raise ValueError('fixed_bank_expense_schema_changed')

    def append_raw(self, sheet, rows):
        expected = {'取込データ': self.imports, '収入明細': self.incomes}.get(sheet)
        if (expected is None or len(rows) != len({r[0] for r in rows})
                or any(r[0] not in expected or r != expected[r[0]] for r in rows)):
            raise ValueError('fixed_bank_write_outside_selection')
        self.guard()
        if any(r[0] in snapshot(self.db)[sheet] for r in rows):
            raise ValueError('fixed_bank_duplicate_append')
        return self.db.append_raw(sheet, rows)

    def append(self, sheet, rows):
        if not rows: return
        if sheet != '支出明細' or len(rows) != len({r[0] for r in rows}) or any(len(r) != 13 or r[10] not in self.selected
                or r[0] != expense_id(r[10]) or r[10] not in self.imports
                or self.imports[r[10]][8] != 'bank_expense'
                or (r[1], r[2], r[4], r[7], r[8]) != (self.imports[r[10]][4], self.imports[r[10]][5],
                    abs(self.imports[r[10]][6]), '銀行口座', self.imports[r[10]][2]) for r in rows):
            raise ValueError('fixed_bank_expense_outside_selection')
        self.guard()
        if any(r[0] in snapshot(self.db)[sheet] for r in rows):
            raise ValueError('fixed_bank_existing_expense_protected')
        return self.db.append(sheet, rows)

    def update_rows(self, sheet, updates):
        if not updates: return
        if sheet != '取込データ':
            raise ValueError('fixed_bank_existing_row_update_forbidden')
        current = self.db.get('取込データ!A2:L')
        for rownum, row in updates:
            if rownum < 2 or rownum - 2 >= len(current):
                raise ValueError('fixed_bank_row_identity_changed')
            old = list(current[rownum - 2])
            if (row[0] not in self.selected or row[0] not in self.imports or not old or old[0] != row[0]
                    or self.imports[row[0]][8] != 'bank_expense'
                    or normal_row(sheet, old) != self.imports[row[0]]
                    or row[8:10] != ['auto_expense', expense_id(row[0])]
                    or any(normal_row(sheet, row)[k] != self.imports[row[0]][k] for k in range(11) if k not in {8, 9})):
                raise ValueError('fixed_bank_existing_row_update_forbidden')
        self.guard()
        return self.db.update_rows(sheet, updates)


def execute(plan, batch, *, db, state, policy, pdf_bytes, rules, resolver, now, apply=False, grant=None):
    """Called only inside the existing durable bank/production pending gates."""
    from .bank_income_recurring import IncomeAppendEvidence
    from .bank_finalization import BankFinalizationPipeline
    selected = batch_ids(plan, batch)
    if apply and (type(grant) is not _Grant or grant.token is not _GRANT
                  or grant.plan != digest(plan) or not now < grant.expires):
        raise ValueError('fixed_bank_separate_permission_required')
    if len(set(selected)) > min(100, policy.max_rows, plan['limit']):
        raise ValueError('fixed_bank_shared_id_limit')
    # Runtime supplied digest was already checked against the actual protected file.
    guard = lambda: validate_live(plan, db, pdf_bytes, plan['policy_sha256'], rules, resolver)
    before, transactions = guard()
    with sqlite3.connect(state.path) as connection:
        if not connection.execute("SELECT 1 FROM recurring_runs WHERE status IN ('complete','noop','dry_run_ready','dry_run_noop','income_append_verified') LIMIT 1").fetchone():
            raise ValueError('fixed_bank_existing_history_required')
        events = [json.loads(r[0]) for r in connection.execute(
            "SELECT summary_json FROM recurring_runs WHERE status IN ('fixed_backfill_pending','fixed_backfill_verified','income_append_pending','income_append_verified') ORDER BY rowid")]
    pending = {}
    for event in events:
        if event['status'].startswith('income_append_'):
            key = ('income', event['income_attempt'])
        else:
            key = ('fixed', event['fixed_plan'], event['fixed_batch'])
        if event['status'].endswith('_pending'): pending[key] = event
        else: pending.pop(key, None)
    if pending:
        raise ValueError('fixed_bank_unknown_do_not_retry')
    verified = [e for e in events if e['status'] == 'fixed_backfill_verified' and e['fixed_plan'] == digest(plan)]
    for event in verified: require_rows(db, event['fixed_rows'])
    relevant = [e for e in events if e.get('fixed_plan') == digest(plan) and e.get('fixed_batch') == batch]
    relevant = [e for e in relevant if e['status'].startswith('fixed_backfill_')]
    if relevant:
        if relevant[-1]['status'] != 'fixed_backfill_verified':
            raise ValueError('fixed_bank_unknown_do_not_retry')
        require_rows(db, relevant[-1]['fixed_rows'])
        return {'success': True, 'written': 0, 'duplicate': len(selected), 'backfill_verified_replay': 1}
    writable = [i for i in selected if plan['items'][i]['disposition'] != 'hold']
    for i in selected:
        if (i in before['取込データ'] or income_id(i) in before['収入明細'] or expense_id(i) in before['支出明細']
                or any(r[6] == i for r in before['収入明細'].values())
                or any(r[10] == i for r in before['支出明細'].values())):
            raise ValueError('fixed_bank_target_already_present')
    imports, incomes = {}, {}
    expense_ids = []
    for i in writable:
        tx = transactions[i];kind = plan['items'][i]['disposition']
        if (tx.source, tx.account_alias) not in policy.income_accounts:
            raise ValueError('fixed_bank_account_outside_scope')
        imports[i] = materialize_import_row(tx.to_canonical(), imported_at=now,
            status={'income': 'bank_income', 'expense': 'bank_expense', 'nonposting': 'bank_non_expense'}[kind])
        if kind == 'income':
            d = deposit_decisions([tx], **rules, meaning_resolver=resolver)[0]
            if len(d) != 1 or d[0].outcome != 'confirmed_income':
                raise ValueError('fixed_bank_income_not_confirmed')
            incomes[income_id(i)] = d[0].row()
        if kind == 'expense': expense_ids.append(i)
    # Before any write, ask the unchanged finalization classifier about projected
    # imports. It may reduce automation but must never broaden the frozen plan.
    class Projected:
        def get(self, rng):
            return db.get(rng) + list(imports.values()) if rng == '取込データ!A2:L' else db.get(rng)
    if expense_ids:
        check = BankFinalizationPipeline(Projected(), confirmed_internal_transfers=rules['confirmed_internal_transfers']).preview(tuple(expense_ids))
        if check['new_expense'] != len(expense_ids) or check['review'] or check['excluded_link'] or check['non_expense']:
            raise ValueError('fixed_bank_expense_finalization_changed')
    counts = Counter(plan['items'][i]['disposition'] for i in selected)
    result = {'success': True, 'bounded_rows': len(selected), 'income': counts['income'], 'expense': counts['expense'],
              'non_expense': counts['nonposting'], 'review': counts['hold'], 'written': 0, 'write_requests': 0}
    if not apply: return result
    summary = dict(run_id=str(uuid4()), source_window_start=now.isoformat(), source_window_end=now.isoformat(),
                   status='fixed_backfill_pending', fixed_plan=digest(plan), fixed_batch=batch)
    state.record(summary, advance_checkpoint=False)
    guarded = FixedWriteDB(db, selected, imports, incomes, guard)
    evidence = IncomeAppendEvidence(guarded, state, summary, meaning_resolver=resolver)
    evidence.require_existing_history()
    evidence.reconcile(dry_run=False)
    evidence.append('取込データ', list(imports.values()))
    if incomes:
        class IncomeEvidenceDB:
            sid = guarded.sid
            def get(self, rng): return guarded.get(rng)
            def sheet_titles(self): return guarded.sheet_titles()
            def append_raw(self, sheet, rows): return evidence.append(sheet, rows)
        BankIncomePipeline(IncomeEvidenceDB(), **rules, meaning_resolver=resolver).apply(
            tuple(r[6] for r in incomes.values()), income_write_enabled=True,
            approved_spreadsheet_id=plan['target'], max_rows=plan['limit'])
    if expense_ids:
        BankFinalizationPipeline(guarded, confirmed_internal_transfers=rules['confirmed_internal_transfers']).apply(
            tuple(expense_ids), max_rows=plan['limit'])
    after = snapshot(db)
    allowed = {'取込データ': set(imports), '収入明細': set(incomes), '支出明細': {expense_id(i) for i in expense_ids}}
    for sheet in RANGES:
        if (any(after[sheet].get(i) != row for i, row in before[sheet].items())
                or set(after[sheet]) - set(before[sheet]) != allowed[sheet]):
            raise ValueError('fixed_bank_independent_readback_changed')
    # Full column comparisons, not just row counts or link membership.
    expected = {'取込データ': {}, '収入明細': incomes, '支出明細': {}}
    for i, row in imports.items():
        actual = after['取込データ'][i]
        if i not in expense_ids and actual != row:
            raise ValueError('fixed_bank_import_readback_mismatch')
        if i in expense_ids and (actual[:8] != row[:8] or actual[8:11] != ['auto_expense', expense_id(i), row[10]]):
            raise ValueError('fixed_bank_expense_readback_mismatch')
        expected['取込データ'][i] = actual
    for i in expense_ids:
        row = after['支出明細'][expense_id(i)];tx = transactions[i]
        if row[0:3] != [expense_id(i), tx.transaction_date, tx.description] or row[4] != abs(tx.signed_amount) or row[7:11] != ['銀行口座', tx.source, '', i] or row[12] != 'active':
            raise ValueError('fixed_bank_expense_readback_mismatch')
        expected['支出明細'][row[0]] = row
    require_rows(db, expected)
    guard()
    state.record(dict(summary, run_id=str(uuid4()), status='fixed_backfill_verified', fixed_rows=expected), advance_checkpoint=False)
    result.update(written=len(incomes) + len(expense_ids), deposit_imports_created=len(imports), read_back_verified=True)
    return result


def run_from_environment(env, *, mode, scope, bank_apply):
    """Existing main workflow and existing bank state only; no new scope/store."""
    import subprocess
    from .production_flow import verify_execution_boundary, REPO
    from .settings import Settings, service_account_source
    from .google_clients import read_only_drive_service, drive_service, read_only_sheets_service, download_drive_file
    from .sheets import SheetsDB
    from .private_state_bindings import unwrap
    from .bank_recurring_authority import ProtectedBankRecurringAuthorityProvider
    from .bank_meaning_resolver import from_environment
    from .aupay_card_recurring import SqliteRecurringRunState
    from .drive_run_state import StateBinding, DriveStateTransport, DurableState
    from .production_ledger import ProductionLedger
    from .production_run import run_durable_source
    from .bank_income_recurring import require_income_actions
    head = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=REPO, text=True).strip()
    verify_execution_boundary(env, head)
    require_income_actions(REPO, head)
    if scope != 'drive_bank' or mode not in {'preview', 'apply'} or (mode == 'apply' and (not bank_apply or env.get('CONFIRM') != 'APPLY')):
        raise ValueError('fixed_bank_isolated_scope_required')
    if env.get('KAKEIBO_AUTHORITY_RECIPIENT'):
        raise ValueError('fixed_bank_unexpected_report_recipient')
    path, info = service_account_source();info = info or json.loads(Path(path).read_bytes())
    fid = unwrap('BANK_FIXED_BACKFILL_PLAN_FILE_ID', env['BANK_FIXED_BACKFILL_PLAN'], info['private_key'])
    readonly = read_only_drive_service()
    plan = json.loads(download_drive_file(fid, readonly))
    batch_text = env.get('BANK_FIXED_BACKFILL_BATCH', '')
    if not batch_text.isdecimal(): raise ValueError('fixed_bank_batch_required')
    batch = int(batch_text)
    settings = Settings()
    rules = dict(confirmed_internal_transfers=settings.bank_confirmed_internal_transfers(),
                 confirmed_non_own_classifications=settings.bank_confirmed_non_own_classifications())
    permit = json.loads(env.get('BANK_FIXED_BACKFILL_APPROVAL_JSON') or '{}')
    service = drive_service() if mode == 'apply' else readonly
    db = SheetsDB(env['SPREADSHEET_ID'], service=read_only_sheets_service() if mode == 'preview' else None)
    with tempfile.TemporaryDirectory(prefix='fixed-bank-') as temp:
        root = Path(temp);authority = root / 'authority.json'
        authority.write_text(env['BANK_PDF_RECURRING_AUTHORITY_JSON'], encoding='utf-8')
        provider = ProtectedBankRecurringAuthorityProvider(authority, repo_root=REPO);policy = provider.load()
        if hashlib.sha256(authority.read_bytes()).hexdigest() != plan['policy_sha256']:
            raise ValueError('fixed_bank_standing_policy_changed')
        grant = authorize(plan, permit, policy, env, head, datetime.now(timezone.utc))
        ledger_binding = StateBinding('production_run', env['SPREADSHEET_ID'], env['KAKEIBO_STATE_FOLDER_ID'], env['KAKEIBO_RUN_LEDGER_FILE_ID'])
        ledger = ProductionLedger(DriveStateTransport(service, ledger_binding), ledger_binding)
        if any(x['phase'] != 'ready' for x in ledger.value['sources'].values()):
            raise ValueError('fixed_bank_unresolved_stage')
        binding = StateBinding('bank_pdf_drive', env['SPREADSHEET_ID'], env['KAKEIBO_STATE_FOLDER_ID'], env['BANK_STATE_FILE_ID'])
        def pdf_bytes(identity):
            meta = readonly.files().get(fileId=identity, fields='id,mimeType,parents,trashed', supportsAllDrives=True).execute()
            if meta.get('trashed') or meta.get('mimeType') != 'application/pdf' or plan['folder'] not in meta.get('parents', []):
                raise ValueError('fixed_bank_pdf_location_changed')
            return download_drive_file(identity, readonly)
        def callback(local, apply):
            resolver = from_environment(db, legacy_rules=rules, env=env)
            state = SqliteRecurringRunState(local / 'bank-recurring.sqlite3', repo_root=REPO)
            checkpoint = state.successful_window_end()
            result = execute(plan, batch, db=db, state=state, policy=policy, rules=rules, resolver=resolver,
                now=datetime.now(timezone.utc), pdf_bytes=pdf_bytes, apply=apply, grant=grant)
            if state.successful_window_end() != checkpoint:
                raise ValueError('fixed_bank_checkpoint_changed')
            return result
        # Validate the complete selection before persisting financial intent.
        preview_store = DurableState(DriveStateTransport(readonly, binding), binding)
        result = run_durable_source(preview_store, root / 'preview-state', lambda p: callback(p, False), apply=False)
        if mode == 'preview': return result
        ledger.begin('bank', uuid4().hex)
        store = DurableState(DriveStateTransport(service, binding), binding)
        try:
            result = run_durable_source(store, root / 'apply-state', lambda p: callback(p, True), apply=True)
            ledger.complete('bank', result, 0.0)
        except Exception:
            ledger.fail('bank')
            raise
        return result
