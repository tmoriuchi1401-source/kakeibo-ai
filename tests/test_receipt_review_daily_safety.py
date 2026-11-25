"""Focused offline regressions for interrupted scheduling and credential storage."""
import json
from pathlib import Path
from types import SimpleNamespace
import sys

import pytest

from scripts import run_receipt_review_daily as daily


def arguments(tmp_path):
    args = ['daily', '--state-dir', str(tmp_path), '--expected-sha', 'a'*40]
    for name in ('config', 'attempt-db', 'fence-seed', 'owner-token', 'client', 'gemini-key'):
        args.extend(['--'+name, str(tmp_path / name)])
    return args


@pytest.mark.parametrize('interrupted', [False, True])
def test_interruption_or_invalid_config_stops_before_auth(tmp_path, monkeypatch, interrupted):
    monkeypatch.setattr(sys, 'argv', arguments(tmp_path))
    monkeypatch.setattr(daily, 'access_token', lambda *_: pytest.fail('no authorization on interruption'))
    monkeypatch.setattr(daily.subprocess, 'check_output', lambda *_a, **_k: pytest.fail('no git check on interruption'))
    (tmp_path/'config').write_text(json.dumps({'enabled': True, 'financial_write_enabled': not interrupted}))
    if interrupted:
        (tmp_path/'RUNNING.json').write_text('{"retry":false}')
    assert daily.main() == 3
    stop = (tmp_path/'STOP.json').read_bytes()
    assert json.loads(stop)['owner_reconciliation_required'] is True
    assert json.loads(stop)['cleanup'] is None
    assert daily.main() == 3 and (tmp_path/'STOP.json').read_bytes() == stop
    if interrupted:
        assert (tmp_path/'RUNNING.json').read_text() == '{"retry":false}'


@pytest.mark.parametrize('github_ok,script_ok', [(True, True), (False, True), (True, False)])
def test_cleanup_attempts_both_services_once_and_reads_back(monkeypatch, github_ok, script_ok):
    from app import receipt_review_worker as worker
    calls = []
    def gh(args, **kwargs):
        calls.append('gate_off' if '--method' in args else 'gate_read')
        return SimpleNamespace(returncode=0 if github_ok else 1, stdout='false')
    monkeypatch.setattr(daily.subprocess, 'run', gh)
    class Session:
        def post(self, url, *, json, **kwargs):
            name = json['function']; calls.append(name)
            result = {'state':'受付OFF・金融write OFF'} if name == 'receiptReviewStop' else {'pat_present': False}
            return SimpleNamespace(status_code=200 if script_ok else 403,
                json=lambda:dict(done=True, response={'result':result}))
    class Port:
        def __init__(self, *a, **k): pass
        def status(self): calls.append('status'); return dict(enabled=False, financial_write_count=0)
    monkeypatch.setattr(worker, 'ScriptPort', Port)
    result = daily.close_remote(Session(), dict(script_id='synthetic-script', folder_id='synthetic-folder', owner_email='owner@synthetic.invalid'))
    assert result == dict(github_off=github_ok, backend_off=script_ok, pat_copy_removed=script_ok)
    assert calls[:3] == ['gate_off', 'gate_read', 'receiptReviewStop']
    assert calls.count('receiptReviewStop') == 1


def test_daily_refuses_legacy_plaintext_owner_file(tmp_path):
    from scripts.receipt_review_credentials import load_owner
    path = tmp_path/'owner.json'
    path.write_text('{"refresh_token":"SYNTHETIC_ONLY"}')
    with pytest.raises(ValueError, match='protected_owner_credential_required'):
        load_owner(path)


def test_owner_envelope_roundtrip_and_exclusive_save(tmp_path, monkeypatch):
    from scripts import receipt_review_credentials as store
    calls = []
    def protect(raw, *, decrypt=False):
        calls.append(decrypt)
        # This fake verifies envelope/IO only; it is not encryption evidence.
        return raw[::-1]
    monkeypatch.setattr(store, 'protect', protect)
    value = {'refresh_token': 'SYNTHETIC_ONLY'}
    path = tmp_path/'owner.dpapi.json'
    store.save_owner(path, value)
    before = path.read_bytes()
    assert b'SYNTHETIC_ONLY' not in before
    assert store.load_owner(path) == value
    with pytest.raises(FileExistsError): store.save_owner(path, value)
    assert path.read_bytes() == before and calls == [False, True, False]


def test_paginated_inbox_retains_all_but_processes_one():
    from app.drive_receipts import process_review_inbox
    calls, processed = [], []
    def listing(**options):
        calls.append(options.get('pageToken'))
        page = {'files':[dict(id='synthetic-source', mimeType='image/png', version='1')]}
        return SimpleNamespace(execute=lambda: page if 'pageToken' in options else dict(files=[], nextPageToken='second'))
    service = SimpleNamespace(files=lambda:SimpleNamespace(list=listing))
    review = SimpleNamespace(metadata=lambda sid:dict(version='1'),
        process_bytes=lambda raw, mime, sid, **k: processed.append(sid) or dict(status='review_created'))
    assert process_review_inbox('synthetic-folder', review, service=service, download=lambda _:b'synthetic') == [{'status':'review_created'}]
    assert calls == [None, 'second'] and processed == ['synthetic-source']


@pytest.mark.parametrize('outcome', ['success', 'unknown', 'duplicate'])
def test_cycle_only_one_accepted_uuid_and_no_ai_on_backlog(tmp_path, monkeypatch, outcome):
    from scripts import run_receipt_review as runner
    from app import receipt_review_worker as worker, receipt_review_history as history
    import requests
    ids = ['11111111-1111-4111-8111-'+str(i).zfill(12) for i in range(3)]
    seen = []
    class Port:
        def __init__(self, *a, **k): pass
        def status(self):
            return dict(enabled=True, stopped=False, processing_count=0, case_count=100, request_count=100)
        def pending(self): return ids[:]
    class History:
        def __init__(self, *a): pass
        def duplicate(self, *a): return False
    def execute(rid, *, fence, **kwargs):
        seen.append(rid); fence.consume(rid)
        return dict(state='結果不明・照合待ち' if outcome == 'unknown' else '保留' if outcome == 'duplicate' else '検証済み・未記帳',
                    issues=['existing_receipt_duplicate'] if outcome == 'duplicate' else [])
    monkeypatch.setattr(worker, 'ScriptPort', Port)
    monkeypatch.setattr(worker, 'execute_review', execute)
    monkeypatch.setattr(history, 'ReceiptHistory', History)
    monkeypatch.setenv('RECEIPT_REVIEW_OWNER_ACCESS_TOKEN', 'SYNTHETIC')
    cfg = dict(schema='receipt-review-runner-b1', enabled=True, financial_write_enabled=False,
        script_id='synthetic-script', folder_id='synthetic-folder', owner_email='owner@synthetic.invalid',
        categories=[['food','groceries']], production_spreadsheet_id='synthetic-production',
        review_spreadsheet_id='synthetic-review', model='synthetic-model', max_open_case_count=50)
    path = tmp_path/'config.json'; path.write_text(json.dumps(cfg))
    monkeypatch.setattr(sys, 'argv', ['runner','--config',str(path),'--attempt-db',str(tmp_path/'attempts.sqlite'),
                                    '--diagnostic',str(tmp_path/'cycle.jsonl'),'cycle'])
    if outcome == 'success':
        assert runner.main() == 0
    else:
        with pytest.raises(RuntimeError, match='backend_'): runner.main()
    assert seen == ids[:1]
    fence = worker.AttemptFence(tmp_path/'attempts.sqlite')
    assert fence.attempted(ids[0]) and not fence.attempted(ids[1])
