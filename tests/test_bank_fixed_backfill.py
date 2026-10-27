from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import bank_fixed_backfill as fixed
from app.aupay_card_recurring import SqliteRecurringRunState
from app.bank_pdf_pipeline import NormalizedBankTransaction, SOURCE
from app.bank_income import INCOME_HEADERS
from app.sheets import HEADERS

NOW = datetime(2026, 10, 27, tzinfo=timezone.utc)
HEAD = 'a' * 40
RULES = dict(confirmed_internal_transfers=frozenset(), confirmed_non_own_classifications=frozenset())


@pytest.fixture(autouse=True)
def workspace_temp_directory(tmp_path, monkeypatch):
    monkeypatch.setattr(fixed.tempfile, 'tempdir', str(tmp_path))


def transaction(name, amount, description):
    h = hashlib.sha256(name.encode()).hexdigest()
    return NormalizedBankTransaction(SOURCE, 'jibun-primary', '2025-01-01', description, amount,
        1, 1, 'bankpdf:au-jibun:jibun-primary:' + h[:24], h, 'deposit' if amount > 0 else 'withdrawal')


class DB:
    sid = 'target'
    def __init__(self):
        self.data = {n: [] for n in fixed.RANGES}
        self.calls = []
        self.fail_income = False
        self.fail_expense = False
        for n in range(185):
            i = 'protected-' + str(n)
            self.data['取込データ'].append([i, '', 'synthetic', i, '2025-01-01', 'protected', -100, '銀行口座', 'bank_non_expense', '', 'h', ''])
    def get(self, rng):
        sheet, span = rng.split('!')
        if span in {'A1:L1', 'A1:M1', 'A1:J1'}:
            return [INCOME_HEADERS if sheet == '収入明細' else HEADERS[sheet]]
        return deepcopy(self.data[sheet])
    def sheet_titles(self): return list(self.data)
    def categories(self): return [('その他', '未分類')]
    def append_raw(self, sheet, rows):
        self.calls.append(('append_raw', sheet, deepcopy(rows)))
        self.data[sheet].extend(deepcopy(rows))
        if sheet == '収入明細' and self.fail_income: raise ConnectionError('synthetic_unknown')
    def append(self, sheet, rows):
        self.calls.append(('append', sheet, deepcopy(rows)))
        self.data[sheet].extend(deepcopy(rows))
        if sheet == '支出明細' and self.fail_expense: raise ConnectionError('synthetic_partial_failure')
    def update_rows(self, sheet, rows):
        self.calls.append(('update', sheet, deepcopy(rows)))
        for rownum, row in rows: self.data[sheet][rownum - 2] = deepcopy(row)


class Resolver:
    master = SimpleNamespace(fingerprint='master')
    def require_unchanged(self): pass
    def __call__(self, tx): return {'state': 'absent'}


def permission(plan, policy, **changes):
    env = dict(GITHUB_ACTIONS='true', GITHUB_REF='refs/heads/main', GITHUB_REPOSITORY='tmoriuchi1401-source/kakeibo-ai',
        KAKEIBO_PRODUCTION_ENABLED='true', KAKEIBO_VALIDATED_MAIN_SHA=HEAD, GITHUB_SHA=HEAD,
        GITHUB_REPOSITORY_OWNER='owner', GITHUB_ACTOR='owner', GITHUB_TRIGGERING_ACTOR='owner',
        GITHUB_EVENT_NAME='workflow_dispatch', GITHUB_WORKFLOW_REF='tmoriuchi1401-source/kakeibo-ai/.github/workflows/kakeibo-production.yml@refs/heads/main',
        KAKEIBO_SCHEDULE_ENABLED='false', BANK_INCOME_WRITE_ENABLED='true', BANK_REVIEW_ENABLED='true')
    env.update(changes)
    permit = dict(schema=1, kind=plan['kind'], plan_sha256=fixed.digest(plan), expected_main_sha=HEAD,
        expires_at=(NOW + timedelta(days=1)).isoformat(), operations=list(fixed.OPERATIONS))
    return fixed.authorize(plan, permit, policy, env, HEAD, NOW)


@pytest.fixture
def scenario(tmp_path):
    db = DB()
    txs = [transaction('income', 1000, '利息'), transaction('expense', -2000, '口座振替公共サービス'),
           transaction('transfer', -3000, '振替'), transaction('hold', 4000, '未確認')]
    kinds = ['income', 'expense', 'nonposting', 'hold']
    items = {}
    for tx, kind in zip(txs, kinds):
        items[tx.source_row_identity] = dict(pdf='pdf', transaction=fixed.asdict(tx), disposition=kind,
            classification='expense' if kind == 'expense' else 'income' if kind == 'income' else 'transfer' if kind == 'nonposting' else 'needs_review',
            reason=kind, deposit_outcome='confirmed_income' if kind == 'income' else 'needs_review' if kind == 'hold' else None,
            deposit_reason='bank_interest_type' if kind == 'income' else 'deposit_purpose_unconfirmed' if kind == 'hold' else None)
    report = dict(items=items, protected={r[0]: fixed.digest(r) for r in db.data['取込データ']}, target=db.sid,
        folder='folder', limit=100, policy_sha256='policy', rules_sha256=fixed.rule_digest(RULES), master_sha256='master',
        pdfs=[dict(id='pdf', sha256=hashlib.sha256(b'pdf').hexdigest(), identities=sorted(list(items) + [r[0] for r in db.data['取込データ']]))])
    plan = fixed.make_plan(report)
    policy = SimpleNamespace(max_rows=100, max_files=20, expected_spreadsheet_id=db.sid, expected_drive_folder_id='folder',
        income_enabled=True, income_accounts=((SOURCE, 'jibun-primary'),), valid_from=NOW-timedelta(days=1),
        expires_at=NOW+timedelta(days=30), validate=lambda: None)
    state = SqliteRecurringRunState(tmp_path/'state'/'bank-recurring.sqlite3', repo_root=tmp_path/'repo')
    state.record(dict(run_id='initial', status='dry_run_noop', source_window_start=NOW.isoformat(), source_window_end=NOW.isoformat()), advance_checkpoint=False)
    return SimpleNamespace(db=db, txs={t.source_row_identity:t for t in txs}, plan=plan, policy=policy,
        state=state, resolver=Resolver(), protected=deepcopy(db.data['取込データ']), grant=permission(plan, policy))


def execute(s, monkeypatch, **kwargs):
    monkeypatch.setattr(fixed, 'validate_live', lambda *a, **k: (fixed.snapshot(s.db), s.txs))
    return fixed.execute(s.plan, 0, db=s.db, state=s.state, policy=s.policy, rules=RULES,
        resolver=s.resolver, now=NOW, pdf_bytes=lambda _: b'pdf', apply=True, grant=s.grant, **kwargs)


def test_mixed_batch_uses_existing_writers_and_protects_185(scenario, monkeypatch):
    s = scenario
    result = execute(s, monkeypatch)
    assert result['written'] == 2 and result['bounded_rows'] == 4
    assert len(s.db.data['収入明細']) == len(s.db.data['支出明細']) == 1
    assert len(s.db.data['取込データ']) == 188  # held ID gets no write
    assert s.db.data['取込データ'][:185] == s.protected
    nonposting = next(r for r in s.db.data['取込データ'] if r[8] == 'bank_non_expense' and r[0] in s.txs)
    assert nonposting[0] not in {r[6] for r in s.db.data['収入明細']}
    assert nonposting[0] not in {r[10] for r in s.db.data['支出明細']}
    assert s.state.successful_window_end() is None


def test_successful_rerun_is_exact_readback_only(scenario, monkeypatch):
    s = scenario
    execute(s, monkeypatch); before = deepcopy(s.db.data); calls = len(s.db.calls)
    result = execute(s, monkeypatch)
    assert result['written'] == 0 and result['duplicate'] == 4
    assert s.db.data == before and len(s.db.calls) == calls


@pytest.mark.parametrize('failure', ['fail_income', 'fail_expense'])
def test_partial_or_unknown_stops_without_retry_or_rollback(scenario, monkeypatch, failure):
    s = scenario;setattr(s.db, failure, True)
    with pytest.raises(ConnectionError): execute(s, monkeypatch)
    after = deepcopy(s.db.data);calls = len(s.db.calls)
    with pytest.raises(ValueError, match='unknown_do_not_retry'): execute(s, monkeypatch)
    assert s.db.data == after and len(s.db.calls) == calls
    assert s.db.data['取込データ'][:185] == s.protected


def test_duplicate_financial_row_rejected_before_write(scenario, monkeypatch):
    s = scenario;i = next(iter(s.txs))
    s.db.data['収入明細'] = [[fixed.income_id(i), '2025-01-01', 1000, '利息', '利息', 'jibun-primary', i, SOURCE, 'bank_interest_type', 'h']]
    with pytest.raises(ValueError, match='already_present'): execute(s, monkeypatch)
    assert not s.db.calls


def test_outside_fixed_set_and_protected_updates_rejected(scenario):
    s = scenario
    guarded = fixed.FixedWriteDB(s.db, list(s.txs), {}, {}, lambda: None)
    with pytest.raises(ValueError, match='outside_selection'): guarded.append_raw('取込データ', [['outside']])
    with pytest.raises(ValueError, match='update_forbidden'): guarded.update_rows('取込データ', [(2, s.protected[0])])
    with pytest.raises(ValueError, match='update_forbidden'): guarded.update_rows('支出明細', [(2, ['outside'])])
    assert not s.db.calls


def test_separate_permission_cannot_be_omitted(scenario):
    s = scenario
    with pytest.raises(ValueError, match='separate_permission'):
        fixed.execute(s.plan, 0, db=s.db, state=s.state, policy=s.policy, rules=RULES,
            resolver=s.resolver, now=NOW, pdf_bytes=lambda _: b'pdf', apply=True)
    assert not s.db.calls


def test_duplicate_rows_in_single_append_rejected(scenario):
    s = scenario;row = ['new']
    guarded = fixed.FixedWriteDB(s.db, ['new'], {'new': row}, {}, lambda: None)
    with pytest.raises(ValueError, match='outside_selection'): guarded.append_raw('取込データ', [row,row])
    assert not s.db.calls


def test_lower_limit_and_classification_change_invalidate_permission(scenario):
    s = scenario;s.policy.max_rows=3
    with pytest.raises(ValueError, match='one_time_permission'): permission(s.plan,s.policy)
    s.policy.max_rows=100
    changed=deepcopy(s.plan);changed['items'][next(iter(changed['items']))]['disposition']='hold'
    with pytest.raises(ValueError, match='separate_permission'):
        fixed.execute(changed,0,db=s.db,state=s.state,policy=s.policy,rules=RULES,resolver=s.resolver,
            now=NOW,pdf_bytes=lambda _:b'pdf',apply=True,grant=s.grant)


def test_replay_checks_all_saved_columns(scenario, monkeypatch):
    s = scenario;execute(s,monkeypatch)
    s.db.data['収入明細'][0][2] += 1
    calls=len(s.db.calls)
    with pytest.raises(ValueError,match='exact_readback'):execute(s,monkeypatch)
    assert len(s.db.calls)==calls


def test_unresolved_old_income_evidence_stops_without_marking_verified(scenario, monkeypatch):
    s=scenario
    s.state.record(dict(run_id='old-unknown', status='income_append_pending', income_attempt='unknown',
        income_target=s.db.sid, income_sheet='収入明細', income_rows=[],
        source_window_start=NOW.isoformat(), source_window_end=NOW.isoformat()), advance_checkpoint=False)
    with pytest.raises(ValueError,match='unknown_do_not_retry'):execute(s,monkeypatch)
    assert not s.db.calls


def test_previous_batch_readback_is_required_before_later_batch(scenario, monkeypatch):
    s=scenario
    s.plan['batches']=[s.plan['batches'][0][:2],s.plan['batches'][0][2:]]
    s.grant=permission(s.plan,s.policy)
    execute(s,monkeypatch)
    s.db.data['収入明細'][0][2]+=1
    with pytest.raises(ValueError,match='exact_readback'):
        fixed.execute(s.plan,1,db=s.db,state=s.state,policy=s.policy,rules=RULES,resolver=s.resolver,
            now=NOW,pdf_bytes=lambda _:b'pdf',apply=True,grant=s.grant)


@pytest.mark.parametrize('change', [dict(GITHUB_ACTOR='other'), dict(GITHUB_REF='refs/heads/pr'), dict(KAKEIBO_SCHEDULE_ENABLED='true')])
def test_apply_permission_is_owner_validated_main_only(scenario, change):
    with pytest.raises((ValueError, RuntimeError)): permission(scenario.plan, scenario.policy, **change)


def test_shared_id_cap_honors_lower_live_limit(scenario, monkeypatch):
    s = scenario;s.policy.max_rows = 3
    with pytest.raises(ValueError, match='shared_id_limit'): execute(s, monkeypatch)
    assert not s.db.calls


def test_pdf_larger_than_100_ids_splits_without_archiving(scenario):
    s = scenario;report = {k: deepcopy(v) for k,v in s.plan.items() if k in {'target','folder','pdfs','items','protected','limit','policy_sha256','rules_sha256','master_sha256'}}
    template = next(iter(report['items'].values()));report['items'] = {}
    for n in range(203):
        row = deepcopy(template);i = 'id-'+str(n);row['transaction']['source_row_identity'] = i;report['items'][i] = row
    report['pdfs'][0]['identities'] = sorted(list(report['items']) + list(report['protected']))
    plan = fixed.make_plan(report)
    assert [len(b) for b in plan['batches']] == [100, 100, 3]
    assert len(set(i for b in plan['batches'] for i in b)) == 203


def test_membership_and_batch_mutation_rejected(scenario):
    plan = deepcopy(scenario.plan);plan['batches'][0].append('outside')
    with pytest.raises(ValueError, match='membership'): fixed.validate_plan(plan)
    plan = deepcopy(scenario.plan);plan['batches'][0].append(plan['batches'][0][0])
    with pytest.raises(ValueError, match='membership'): fixed.validate_plan(plan)


def test_workflow_keeps_owner_gate_lock_and_no_new_secret():
    text=(Path(__file__).resolve().parents[1]/'.github/workflows/kakeibo-production.yml').read_text()
    assert "inputs.bank_backfill_plan == '' || (github.actor == github.repository_owner && github.triggering_actor == github.repository_owner)" in text
    assert 'group: kakeibo-production' in text
    assert 'BANK_FIXED_BACKFILL_APPROVAL_JSON: ${{ vars.BANK_FIXED_BACKFILL_APPROVAL_JSON }}' in text
    assert '${{ secrets.BANK_FIXED_BACKFILL' not in text


def test_raw_pdf_or_rule_changes_stop_before_writes(scenario):
    s = scenario
    with pytest.raises(ValueError, match='pdf_changed'):
        fixed.validate_live(s.plan,s.db,lambda _: b'changed','policy',RULES,s.resolver)
    with pytest.raises(ValueError, match='configuration_changed'):
        fixed.validate_live(s.plan,s.db,lambda _: b'pdf','policy',dict(RULES,confirmed_internal_transfers={('x','y','z')}),s.resolver)
    assert not s.db.calls


def test_protected_original_change_stops_before_pdf_parse(scenario):
    s = scenario;s.db.data['取込データ'][0][6] = -999
    with pytest.raises(ValueError, match='protected_import_changed'):
        fixed.validate_live(s.plan,s.db,lambda _: b'pdf','policy',RULES,s.resolver)
    assert not s.db.calls


def test_classification_change_stops_after_full_pdf_verification(scenario, monkeypatch):
    s = scenario
    protected = [SimpleNamespace(source_row_identity=i) for i in s.plan['protected']]
    txs = list(s.txs.values()) + protected
    monkeypatch.setattr(fixed.BankPdfPipeline,'parse',lambda *a,**k: SimpleNamespace(transactions=txs,issues=(),balance_consistency_failures=0))
    classes = []
    for tx in txs:
        classes.append(SimpleNamespace(classification=SimpleNamespace(transaction=tx,classification='needs_review',reason='changed'),write_eligibility='withheld'))
    monkeypatch.setattr(fixed,'build_bank_shadow_result',lambda *a,**k:SimpleNamespace(decisions=classes))
    monkeypatch.setattr(fixed,'deposit_decisions',lambda *a,**k:([SimpleNamespace(transaction=t,outcome='needs_review',reason='changed') for t in s.txs.values()],0))
    with pytest.raises(ValueError, match='classification_changed'):
        fixed.validate_live(s.plan,s.db,lambda _:b'pdf','policy',RULES,s.resolver)
    assert not s.db.calls
