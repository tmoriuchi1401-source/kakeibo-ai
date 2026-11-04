"""Invented proof only; no production identities, credentials or network."""
from copy import deepcopy
import hashlib
import json
from types import SimpleNamespace

import pytest

from app import aupay_card_window_preview as preview
from app import aupay_card_incremental_guard as guard
from app import aupay_card_incremental_recovery as recovery
from app.drive_run_state import StateBinding, envelope
from app.production_run import COUNT_KEYS
from test_aupay_card_incremental_recovery import proof, change, packed, ReconciledGitHub
from test_aupay_card_incremental_apply import env


def amazon_proof(sid='synthetic-sheet', folder='synthetic-folder'):
    binding = StateBinding('amazon_gmail', sid, folder, 'synthetic-amazon')
    end = '2026-11-04T09:40:06+09:00'
    summary = dict(run_id='synthetic-amazon-success', status='complete', failure=0, found=0, fetched=0,
        source_window_start='2026-11-04T05:04:03+09:00', source_window_end=end)
    file = packed([
        ('CREATE TABLE recurring_checkpoint (singleton INTEGER,successful_window_end TEXT,run_id TEXT)', ()),
        ('INSERT INTO recurring_checkpoint VALUES (1,?,?)', (end, summary['run_id'])),
        ('CREATE TABLE recurring_runs (run_id TEXT,recorded_at TEXT,status TEXT,window_start TEXT,window_end TEXT,summary_json TEXT)', ()),
        ('INSERT INTO recurring_runs VALUES (?,?,?,?,?,?)', (summary['run_id'], end, 'complete', summary['source_window_start'], end, json.dumps(summary)))])
    import base64
    file['sha256'] = hashlib.sha256(base64.b64decode(file['data'])).hexdigest()
    raw = envelope(binding, {'recurring.sqlite3': file})
    parent_binding = StateBinding('production_run', sid, folder, 'synthetic-parent')
    item = dict(phase='ready', attempt='', error='', last_success=end,
        counts={k:v for k,v in summary.items() if k in COUNT_KEYS and type(v) is int})
    reader = SimpleNamespace(binding=binding, observe=lambda:(raw, {'sha256':'synthetic-native-proof'}))
    return dict(sources=dict(amazon=item)), reader, parent_binding


@pytest.mark.parametrize('explicit', [True, False])
def test_amazon_completed_count_or_independent_native(explicit):
    parent, reader, binding = amazon_proof()
    before = deepcopy(parent)
    if explicit:
        parent['sources']['amazon']['counts']['catch_up_pending'] = 0
        reader.observe = lambda:pytest.fail('explicit_zero_does_not_need_fallback')
    result = preview.amazon_completion(parent, reader, binding)
    assert result == (None if explicit else {'sha256':'synthetic-native-proof'})
    if not explicit: assert parent == before


@pytest.mark.parametrize('bad', ['missing', 'pending_count', 'bool_count', 'native_pending', 'binding',
    'counts', 'stale_parent', 'result_failure', 'result_pending', 'checkpoint', 'missing_result', 'result_status'])
def test_amazon_incomplete_or_mismatched_evidence_rejected(bad):
    parent, reader, binding = amazon_proof()
    if bad == 'missing': reader = None
    if bad == 'pending_count': parent['sources']['amazon']['counts']['catch_up_pending'] = 1
    if bad == 'bool_count': parent['sources']['amazon']['counts']['catch_up_pending'] = False
    if bad == 'binding': reader.binding = StateBinding('amazon_gmail', 'other-sheet', 'synthetic-folder', 'file')
    if bad == 'counts': parent['sources']['amazon']['counts']['found'] = 2
    if bad == 'stale_parent': parent['sources']['amazon']['last_success'] = '2026-11-04T09:40:05+09:00'
    if bad in {'native_pending', 'result_failure', 'result_pending', 'checkpoint', 'missing_result', 'result_status'}:
        value = json.loads(reader.observe()[0])
        if bad == 'native_pending': value.update(phase='pending', attempt='a'*32)
        elif bad == 'checkpoint':
            change(value, 'recurring.sqlite3', "UPDATE recurring_checkpoint SET successful_window_end='2026-11-04T09:40:07+09:00'")
        elif bad == 'missing_result': change(value, 'recurring.sqlite3', 'DELETE FROM recurring_runs')
        else:
            row = recovery._rows(value, 'recurring.sqlite3', 'recurring_runs')[0]
            summary = json.loads(row['summary_json'])
            if bad == 'result_failure': summary['failure'] = 1
            if bad == 'result_pending': summary['catch_up_pending'] = 1
            if bad == 'result_status': summary['status'] = 'failed'
            change(value, 'recurring.sqlite3', 'UPDATE recurring_runs SET summary_json=?', (json.dumps(summary),))
        import base64
        value['files']['recurring.sqlite3']['sha256'] = hashlib.sha256(base64.b64decode(value['files']['recurring.sqlite3']['data'])).hexdigest()
        reader.observe = lambda:(json.dumps(value).encode(), {})
    with pytest.raises(RuntimeError): preview.amazon_completion(parent, reader, binding)


class NoWriteGitHub(ReconciledGitHub):
    def __init__(self):
        super().__init__()
        self.prior.insert(0, dict(id=recovery.NO_WRITE_RUN, head_sha=recovery.NO_WRITE_SHA,
            status='completed', conclusion='failure', run_attempt=1))
        self.no_write_bad = None
    def get(self, path):
        if path == f'actions/runs/{recovery.NO_WRITE_RUN}':
            value = dict(id=recovery.NO_WRITE_RUN, head_sha=recovery.NO_WRITE_SHA, head_branch='main',
                run_attempt=1, status='completed', conclusion='failure', event='workflow_dispatch',
                path='.github/workflows/'+guard.WORKFLOW, created_at=recovery.NO_WRITE_CREATED)
            if self.no_write_bad in value: value[self.no_write_bad] = 'tampered'
            return value
        if path == f'actions/runs/{recovery.NO_WRITE_RUN}/jobs?per_page=100':
            job = dict(id=recovery.NO_WRITE_JOB, run_id=recovery.NO_WRITE_RUN, name='apply',
                status='completed', conclusion='failure', steps=[dict(number=n, name=name,
                    status='completed', conclusion=c) for n,name,c in (
                        (5,'Live main and run guard with job-start validated and flags','success'),
                        (6,'Install reviewed hash-pinned wheels before Google credentials','success'),
                        (7,'One checkpoint Apply with protected reviews and exact readback','failure'))])
            if self.no_write_bad == 'job': job['id'] += 1
            if self.no_write_bad == 'step': job['steps'][2]['conclusion'] = 'success'
            return dict(total_count=1, jobs=[job])
        return super().get(path)


@pytest.mark.parametrize('scheduled', [False, True])
@pytest.mark.parametrize('bad', [None, 'head_sha', 'run_attempt', 'head_branch', 'created_at', 'job', 'step',
    'other_failure', 'history_missing', 'old_proof', 'checkpoint', 'rerun'])
def test_fixed_no_write_history_manual_and_schedule(tmp_path, scheduled, bad):
    github = NoWriteGitHub()
    values = env() | dict(EXPECTED_CHECKPOINT=recovery.NO_WRITE_CHECKPOINT)
    if scheduled:
        payload = tmp_path/'schedule.json'; payload.write_text(json.dumps(dict(schedule=guard.DAILY_CRON)))
        values.update(GITHUB_EVENT_NAME='schedule', GITHUB_EVENT_PATH=str(payload), SCHEDULE_CRON=guard.DAILY_CRON,
            KAKEIBO_CARD_SCHEDULE_ENABLED='true', CONFIRM='', GITHUB_ACTOR='schedule', GITHUB_TRIGGERING_ACTOR='schedule')
        original = github.get
        def get(path):
            value = original(path)
            if path == 'actions/runs/17': value['event'] = 'schedule'
            return value
        github.get = get
    github.no_write_bad = bad
    if bad == 'other_failure': github.prior.append(dict(id=99, status='completed', conclusion='failure', run_attempt=1))
    if bad == 'history_missing': github.prior = [r for r in github.prior if r['id'] != recovery.NO_WRITE_RUN]
    if bad == 'old_proof': github.bad = 'step'
    if bad == 'checkpoint': values['EXPECTED_CHECKPOINT'] = recovery.RECOVERED_CHECKPOINT
    if bad == 'rerun': values['GITHUB_RUN_ATTEMPT'] = '2'
    cp = values['EXPECTED_CHECKPOINT']
    def call(): return guard.controls(values, 'a'*40, github, **({'expected_checkpoint':cp} if scheduled else {}))
    if bad is None: assert call() is True
    else:
        with pytest.raises((RuntimeError, TypeError)): call()


def no_write_proof(monkeypatch):
    native, parent = proof(monkeypatch)
    change(native, 'recurring.sqlite3', 'ALTER TABLE recurring_checkpoint ADD COLUMN singleton INTEGER DEFAULT 1')
    for column in ('status TEXT', 'window_end TEXT'):
        change(native, 'recurring.sqlite3', 'ALTER TABLE recurring_runs ADD COLUMN '+column)
    change(native, 'recurring.sqlite3', 'UPDATE recurring_runs SET status=?,window_end=?', ('complete', recovery.RECOVERED_CHECKPOINT))
    change(native, 'journal.sqlite3', "ALTER TABLE batch_events ADD COLUMN attempt_id TEXT DEFAULT 'synthetic-attempt'")
    monkeypatch.setattr(recovery, 'JOURNAL_SHA256', recovery._hash(recovery._rows(native, 'journal.sqlite3', 'batch_events')))
    summary = dict(run_id='synthetic-no-write-baseline', status='noop', failure=0, written=0, write_requests=0,
        found=0, fetched=0, source_window_end=recovery.NO_WRITE_CHECKPOINT)
    change(native, 'recurring.sqlite3', 'INSERT INTO recurring_runs VALUES (?,?,?,?)',
        (summary['run_id'],json.dumps(summary),'noop',recovery.NO_WRITE_CHECKPOINT))
    change(native, 'recurring.sqlite3', 'UPDATE recurring_checkpoint SET successful_window_end=?,run_id=?',
        (recovery.NO_WRITE_CHECKPOINT, summary['run_id']))
    parent['sources']['aupay_card'].update(counts={k:v for k,v in summary.items() if k in COUNT_KEYS and type(v) is int})
    tables = {(title,'FORMULA'):[['id','value'],['synthetic-'+str(i), i]] for i,title in enumerate(('支出明細','取込データ','収入明細','レシート'))}
    documents = {k:{'synthetic':k} for k in recovery.NO_WRITE_MONEY_KEYS}
    for name, value in dict(NO_WRITE_NATIVE_SHA256=recovery._hash(native),
        NO_WRITE_PARENT_SHA256=recovery._hash(parent['sources']['aupay_card']),
        NO_WRITE_RESULT_SHA256=recovery._hash(summary), NO_WRITE_FINANCIAL_SHA256=recovery.financial_evidence(tables),
        NO_WRITE_MONEY_SHA256=recovery._hash(documents)).items(): monkeypatch.setattr(recovery,name,value)
    return native, parent, tables, documents


@pytest.mark.parametrize('bad', [None, 'generation', 'parent', 'pending', 'checkpoint', 'UNKNOWN',
    'financial', 'money', 'baseline', 'old_proof'])
def test_no_write_independent_state_and_financial_proof(monkeypatch, bad):
    native,parent,tables,documents = no_write_proof(monkeypatch)
    before = deepcopy((native,parent,tables,documents))
    cp = recovery.NO_WRITE_CHECKPOINT
    if bad == 'generation': native['generation'] += 1
    if bad == 'parent': parent['sources']['aupay_card']['counts']['written'] += 1
    if bad == 'pending': native.update(phase='pending',attempt='a'*32)
    if bad == 'checkpoint': cp = '2026-11-03T14:45:44+09:00'
    if bad == 'UNKNOWN': change(native,'journal.sqlite3',"UPDATE batch_events SET state='outcome_unknown' WHERE stage='final'")
    if bad == 'financial': tables[('支出明細','FORMULA')][1][1] += 1
    if bad == 'money': documents['money'] = {'state':'pending'}
    if bad == 'baseline':
        row = next(r for r in recovery._rows(native,'recurring.sqlite3','recurring_runs') if r['status'] == 'noop')
        summary = json.loads(row['summary_json']); summary['run_id'] = 'synthetic-tampered'
        change(native,'recurring.sqlite3','UPDATE recurring_runs SET summary_json=? WHERE status=?', (json.dumps(summary),'noop'))
    if bad == 'old_proof': monkeypatch.setattr(recovery,'MANIFEST_SHA256','0'*64)
    call = lambda:recovery.verify_no_write_state(native,parent,cp,financial=lambda:tables,
        money=SimpleNamespace(read=lambda k:deepcopy(documents[k])))
    if bad is None:
        call(); assert (native,parent,tables,documents) == before
    else:
        with pytest.raises(RuntimeError): call()


def test_successful_later_checkpoint_retains_both_proofs(monkeypatch):
    native,parent,tables,documents = no_write_proof(monkeypatch)
    end = '2026-11-04T12:00:00+09:00'
    summary = dict(run_id='synthetic-later', status='noop', failure=0, written=0,
        write_requests=0, source_window_end=end)
    change(native,'recurring.sqlite3','INSERT INTO recurring_runs VALUES (?,?,?,?)',
        (summary['run_id'],json.dumps(summary),'noop',end))
    change(native,'recurring.sqlite3','UPDATE recurring_checkpoint SET successful_window_end=?,run_id=?',(end,summary['run_id']))
    parent['sources']['aupay_card'].update(counts={k:v for k,v in summary.items() if k in COUNT_KEYS and type(v) is int})
    recovery.verify_no_write_state(native,parent,end,financial=lambda:pytest.fail('historical_financial_freeze'),money=None)
    change(native,'recurring.sqlite3',"DELETE FROM recurring_runs WHERE run_id='synthetic-no-write-baseline'")
    with pytest.raises(RuntimeError): recovery.verify_no_write_state(native,parent,end,financial=lambda:tables,money=None)
