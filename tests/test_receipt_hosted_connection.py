"""Offline native adapter/owner RPC tests; all ledgers and OAuth are synthetic."""
from copy import deepcopy
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.drive_run_state import StateBinding, StateError
from app.page_receipt_model import digest
from app.production_ledger import ProductionLedger, initial_ledger
from app.receipt_hosted_review import OwnerReviewPort, open_review, SCOPES
from app.receipt_hosted_writer import NativeReceiptWriter, ProductionReceiptAuthority
from app.sheets import HEADERS
from test_receipt_hosted import setup, RAW, SID, restored
from test_receipt_reimport_production import MemoryTransport

ROOT = Path(__file__).resolve().parents[1]


class Ledger:
    def __init__(self):
        self.rows = {title: [deepcopy(HEADERS[title])] for title in ('レシート', '支出明細', '取込データ')}
        self.calls = 0
        self.fault = None
        self.cats = [('食費', '食品')]

    def categories(self): return deepcopy(self.cats)
    def get_raw(self, region): return deepcopy(self.rows[region.split("'")[1]][1:])
    def sheet_rows(self, region, *, render='UNFORMATTED_VALUE'):
        return deepcopy(self.rows[region.split("'")[1]])

    def append_raw(self, title, rows):
        self.calls += 1
        if self.fault == ('before', self.calls): raise TimeoutError()
        self.rows[title].extend(deepcopy(rows))
        if self.fault == ('after', self.calls): raise TimeoutError()


def native(monkeypatch, *, mismatch=False, apply=True):
    runtime, transport, ai, review, _, gate, args = setup(monkeypatch, mismatch=mismatch, writer=False)
    db = Ledger()
    binding = StateBinding('production_run', 'synthetic-sheet', 'synthetic-folder', 'synthetic-ledger')
    parent = ProductionLedger(MemoryTransport(initial_ledger(binding)), binding)
    parent.begin('receipts', 'a'*32)
    env = dict(GITHUB_ACTIONS='true', GITHUB_REF='refs/heads/main',
        GITHUB_REPOSITORY='tmoriuchi1401-source/kakeibo-ai', KAKEIBO_PRODUCTION_ENABLED='true',
        KAKEIBO_VALIDATED_MAIN_SHA='b'*40, GITHUB_SHA='b'*40, RUNNER_ENVIRONMENT='github-hosted', RUNNER_OS='Linux')
    env.update(WORKFLOW_REF='tmoriuchi1401-source/kakeibo-ai/.github/workflows/kakeibo-production.yml@refs/heads/main',
        WORKFLOW_SHA='b'*40, KAKEIBO_LEGACY_DISABLED='true', RECEIPT_PARENT_ATTEMPT='a'*32)
    authority = ProductionReceiptAuthority(env, parent)
    writer = NativeReceiptWriter(db, authority, clock=lambda: '2026-01-01 00:00:00', readers=db)
    runtime.pipeline.db = db
    runtime.writer, runtime.native_apply = writer, apply
    if apply:
        runtime.owner.financial_apply = True
        whole = deepcopy(runtime.owner.store.value)
        whole['receipt_hosted_v1']['financial_write_enabled'] = True
        runtime.owner.store.save(whole)
    args.update(writer=writer, native_apply=apply)
    return runtime, transport, ai, review, db, args, parent


def test_native_normal_uses_production_authority_and_scoped_real_materializer(monkeypatch):
    runtime, transport, ai, review, db, args, parent = native(monkeypatch)
    result = runtime.process_bytes(RAW, 'image/png', SID)
    assert result['status'] == 'imported' and result['accounting_readback_complete'] is True
    assert db.calls == 3 and review.intakes == 0 and ai.analyze_receipt.call_count == 1
    saved = deepcopy(db.rows)
    restored(runtime, transport, args).process_bytes(RAW, 'image/png', SID)
    assert db.rows == saved and db.calls == 3
    assert runtime.owner.read()['records'][SID]['native_readback']['exact_readback'] is True


def test_native_exception_one_register_reaches_same_writer_and_result(monkeypatch):
    runtime, _, ai, review, db, _, _ = native(monkeypatch, mismatch=True)
    assert runtime.process_bytes(RAW, 'image/png', SID)['status'] == 'needs_review'
    rid = review.register_once()
    result = runtime.register(rid)
    assert result['state'] == '記帳済み' and result['financial_write_count'] == 3
    assert result['exact_readback'] is True and db.calls == 3 and review.finishes == 1
    assert ai.analyze_receipt.call_count == 1


def test_production_off_prepares_native_plan_without_financial_write(monkeypatch):
    runtime, _, ai, review, db, _, _ = native(monkeypatch, apply=False)
    result = runtime.process_bytes(RAW, 'image/png', SID)
    assert result['reason'] == 'financial_write_off' and db.calls == 0 and review.intakes == 0
    assert runtime.owner.read()['records'][SID]['native_plan']['rows']


@pytest.mark.parametrize('fault', [('before',1), ('after',1), ('before',2), ('after',2), ('before',3)])
def test_native_partial_or_unknown_never_fills_missing_rows_or_retries(monkeypatch, fault):
    runtime, transport, ai, review, db, args, _ = native(monkeypatch)
    db.fault = fault
    with pytest.raises(StateError, match='write_unknown'):
        runtime.process_bytes(RAW, 'image/png', SID)
    calls = db.calls
    plan = runtime.owner.store.value['receipt_hosted_v1']['records'][SID]['native_plan']
    assert runtime.writer.reconcile(plan)[0] in {'not_written','partial'}
    assert db.calls == calls
    with pytest.raises(StateError, match='unknown_no_retry'):
        restored(runtime, transport, args)
    assert db.calls == calls and ai.analyze_receipt.call_count == 1


def test_lost_final_append_response_completes_only_by_exact_ledger_reconciliation(monkeypatch):
    runtime, _, _, _, db, _, _ = native(monkeypatch)
    db.fault = ('after',3)
    assert runtime.process_bytes(RAW, 'image/png', SID)['accounting_readback_complete'] is True
    assert db.calls == 3


def test_result_loss_does_not_repeat_native_financial_writes(monkeypatch):
    runtime, transport, _, review, db, args, _ = native(monkeypatch, mismatch=True)
    runtime.process_bytes(RAW, 'image/png', SID)
    rid = review.register_once()
    review.lose_finish = True
    with pytest.raises(TimeoutError): runtime.register(rid)
    plan = runtime.owner.store.value['receipt_hosted_v1']['records'][SID]['native_plan']
    assert runtime.writer.reconcile(plan)[0] == 'written'
    with pytest.raises(StateError, match='unknown_no_retry'): restored(runtime, transport, args)
    assert db.calls == 3 and review.finishes == 1


def test_parent_authority_change_blocks_every_native_append(monkeypatch):
    runtime, _, _, _, db, _, parent = native(monkeypatch)
    parent.fail('receipts')
    with pytest.raises(StateError, match='parent_attempt'):
        runtime.process_bytes(RAW, 'image/png', SID)
    assert db.calls == 0


def test_financial_flag_alone_cannot_adopt_a_different_parent_attempt(monkeypatch):
    runtime, _, _, _, db, _, _ = native(monkeypatch)
    runtime.writer.authority.env['RECEIPT_PARENT_ATTEMPT'] = 'c'*32
    with pytest.raises(StateError, match='parent_attempt'):
        runtime.process_bytes(RAW, 'image/png', SID)
    assert db.calls == 0


def test_one_held_case_does_not_block_independent_normal_source(monkeypatch):
    runtime, _, ai, review, db, args, _ = native(monkeypatch, mismatch=True)
    runtime.process_bytes(RAW, 'image/png', SID)
    rid = review.register_once(amount=90)
    runtime.register(rid)
    from hashlib import sha256
    other = dict(runtime.sources[SID], source_id='synthetic-independent', sha256=sha256(b'another').hexdigest())
    runtime.sources[other['source_id']] = other
    ai.analyze_receipt.return_value.items[0].amount = 100
    assert runtime.process_bytes(b'another', 'image/png', other['source_id'])['status'] == 'imported'
    assert db.calls == 3


def test_frozen_source_checked_before_each_append(monkeypatch):
    runtime, _, _, _, db, _, _ = native(monkeypatch)
    fresh = runtime.verify_source
    fresh.side_effect = lambda source: db.calls < 1
    with pytest.raises(StateError, match='write_unknown'):
        runtime.process_bytes(RAW, 'image/png', SID)
    assert db.calls == 1


def test_native_changed_existing_ledger_requires_reconciliation(monkeypatch):
    runtime, _, _, _, db, _, _ = native(monkeypatch)
    plan = runtime.writer.prepare(runtime.pipeline.ai.analyze_receipt.return_value,
        runtime.sources[SID], '')
    db.rows['取込データ'].append(['another-import'])
    with pytest.raises(StateError, match='existing_ledger_changed'):
        runtime.writer.apply(plan, verify_intent=lambda p: None)
    assert db.calls == 0


def test_owner_rpc_has_one_attempt_no_redirect_and_no_github_material():
    session = Mock()
    session.post.return_value = SimpleNamespace(status_code=200, json=lambda: {
        'done':True, 'response':{'@type':'type.googleapis.com/google.apps.script.v1.ExecutionResponse', 'result':[]}})
    port = OwnerReviewPort(session, 'synthetic-script-id')
    assert port.pending() == []
    args = session.post.call_args
    assert args.kwargs == dict(json={'function':'receiptHostedBackend','parameters':[{'action':'pending'}]},
                               timeout=30,allow_redirects=False)
    session.post.return_value.status_code = 401
    with pytest.raises(StateError, match='rpc_unknown'): port.pending()
    assert session.post.call_count == 2
    session.post.side_effect = TimeoutError()
    with pytest.raises(TimeoutError): port.claim('uuid', [])
    assert session.post.call_count == 3


@pytest.mark.parametrize('explicit_type', [True, False])
def test_existing_owner_oauth_scope_and_exact_receiver_binding(monkeypatch, explicit_type):
    import google.auth.transport.requests as transport
    session = Mock()
    factory = Mock(return_value=session)
    monkeypatch.setattr(transport, 'AuthorizedSession', factory)
    config = dict(script_id='synthetic-script-id', owner_email='synthetic@example.invalid',
                  spreadsheet_id='synthetic-review-book', folder_id='synthetic-folder')
    response = dict(enabled=True,stopped=False,binding=config,financial_write_count=0)
    session.post.return_value = SimpleNamespace(status_code=200,json=lambda:dict(done=True,response={
        '@type':'type.googleapis.com/google.apps.script.v1.ExecutionResponse','result':response}))
    info = dict(type='authorized_user',token_uri='https://oauth2.googleapis.com/token',scopes=list(SCOPES),
                refresh_token='SYNTHETIC',client_id='SYNTHETIC',client_secret='SYNTHETIC')
    if not explicit_type: info.pop('type')  # Existing Credentials.to_json format.
    env = dict(RECEIPT_HOSTED_REVIEW_BINDING=json.dumps(config),GOOGLE_RECEIPT_OWNER_TOKEN_JSON=json.dumps(info))
    manifest = dict(owner_email=config['owner_email'],spreadsheet_id='synthetic-ledger-book')
    assert isinstance(open_review(env,manifest,'synthetic-ledger-book',config['folder_id']),OwnerReviewPort)
    assert factory.call_args.kwargs == {'max_refresh_attempts':0}
    response['binding'] = dict(config,script_id='changed-script-id')
    with pytest.raises(StateError,match='not_ready'):
        open_review(env,manifest,'synthetic-ledger-book',config['folder_id'])
    info['scopes'] = ['https://www.googleapis.com/auth/gmail.readonly']
    env['GOOGLE_RECEIPT_OWNER_TOKEN_JSON'] = json.dumps(info)
    before = factory.call_count
    with pytest.raises(StateError,match='existing_owner_scopes_required'):
        open_review(env,manifest,'synthetic-ledger-book',config['folder_id'])
    assert factory.call_count == before


def test_multipage_reuses_native_unit_engine_without_write_or_archive(monkeypatch):
    from app import pdf_intake_production as pdf
    from test_pdf_intake_runner import rig
    from test_pdf_intake_authority import page, RAW as PDF_RAW
    runner, key, uid, db = rig()
    second = page(1,classification='medical',source_id='newPDFSourceFile01')
    runner.store.register(second)
    runner.store.register(page(3,classification='medical',source_id='newPDFSourceFile01'))
    runner.analyze = Mock(return_value={'status':'units_observed'})
    runner.post_normal = Mock(wraps=runner.post_normal)
    context = SimpleNamespace(store=runner.store,source=lambda sid:PDF_RAW,runner=lambda settings:runner)
    projection = Mock()
    monkeypatch.setattr(pdf,'Projection',lambda c:projection)
    monkeypatch.setattr(pdf,'page_count',lambda raw:3)
    archive = Mock(side_effect=AssertionError('original must stay'))
    monkeypatch.setattr(pdf,'archive_terminal',archive)
    source = dict(source_id='newPDFSourceFile01',sha256=page()['source']['source_content_hash'],page_count=3)
    result = pdf.process(context,None,[source],apply=True,financial_write=False)
    assert db.writes == [] and archive.call_count == 0
    assert runner.post_normal.call_args.kwargs == {'apply':False}
    assert result['written'] == 0 and result['needs_review'] > 0


def test_multipage_conflicting_whole_owner_is_held_without_calling_native(monkeypatch):
    from app import pdf_intake_production as pdf
    runtime, _, _, _, _, _, _ = setup(monkeypatch)
    runtime.process_bytes(RAW,'image/png',SID)
    factory = Mock(side_effect=AssertionError('must not construct competing owner'))
    monkeypatch.setattr(pdf,'Context',factory)
    source = dict(runtime.sources[SID],page_count=2)
    assert runtime.process_multipage(None,[source],{}) == {'needs_review':1}
    assert factory.call_count == 0


def test_native_pdf_ai_unknown_is_restored_without_another_send(monkeypatch):
    from app import page_receipt_readonly as readonly
    from app.pdf_intake_registry import RegistryStore
    from test_pdf_intake_runner import rig
    runner, key, _, db = rig()
    analyzer = Mock(return_value=object())
    runner.analyzer_factory = analyzer
    external = Mock()
    external.run.side_effect = TimeoutError('synthetic unknown')
    monkeypatch.setattr(readonly,'ReadonlyPageReceipts',lambda *args, **kwargs:external)
    with pytest.raises(TimeoutError): runner.analyze(key)
    assert runner.store.load()['pages'][key]['status'] == 'analysis_started'
    runner.store = RegistryStore(runner.store.io,runner.store.anchor)
    with pytest.raises(StateError,match='analysis_unknown_no_retry'): runner.analyze(key)
    assert external.run.call_count == analyzer.call_count == 1 and db.writes == []


def test_new_hosted_pdf_path_cannot_reuse_historical_cloud_proof():
    from app.pdf_intake_production import UnavailableProof
    with pytest.raises(StateError,match='authenticated_connection_required'):
        UnavailableProof().verify({}, {})


def script(commands, *, cases=None, requests=None, ui=None, **kwargs):
    payload = dict(commands=commands, **kwargs)
    for key, value in dict(cases=cases,requests=requests,ui=ui).items():
        if value is not None: payload[key] = value
    result = subprocess.run(['node','apps-script/receipt-validation/Review.test.cjs'], input=json.dumps(payload),
        capture_output=True, text=True, encoding='utf-8', cwd=ROOT, check=True)
    return json.loads(result.stdout)


def hosted(action, **data): return dict(action='hosted',command=dict(action=action,**data))


class ScriptReview:
    """Exercise actual Python -> actual receiver with persistent fake Google."""
    def __init__(self, *, http_ports=False):
        self.state = {}
        self.calls = []
        self.http_ports = http_ports

    def call(self, command):
        self.calls.append(command['action'])
        result = script([command], **self.state, drive_bytes=list(RAW), http_ports=self.http_ports)
        self.state = {k: result[k] for k in ('cases','requests','ui','properties')}
        assert result['gateGets'] == 0
        assert not any('GITHUB_TOKEN' in k for k in result['properties'])
        if result['errors']: raise StateError(result['errors'][0])
        return result['outputs'][0]

    def intake(self, entry, row): return self.call(hosted('intake',entry=entry,row=row))
    def lookup_source(self, sid, hashed): return self.call(hosted('lookup',source_id=sid,sha256=hashed))
    def read(self, rid): return self.call(hosted('read',request_id=rid))
    def pending(self): return self.call(hosted('pending'))
    def claim(self, rid, before): return self.call(hosted('claim',request_id=rid,before=before))
    def finish(self, rid, before, result): return self.call(hosted('finish',request_id=rid,before=before,result=result))
    def rearm(self, rid, before, entry): return self.call(hosted('rearm',request_id=rid,before=before,entry=entry))

    def register_once(self, amount):
        entry = json.loads(self.state['cases'][-1][4])
        row = self.state['ui'][0]
        row[8] = 'register:'+entry['request_id']
        row[11], row[4], row[5] = amount, amount, 100-amount
        self.call(dict(action='register',request_id=entry['request_id']))
        return entry['request_id']


@pytest.mark.parametrize('http_ports', [False, True])
def test_correction_after_rejection_keeps_uuid_snapshot_drafts_and_reaches_native_writer(monkeypatch, http_ports):
    runtime, transport, ai, _, db, args, _ = native(monkeypatch, mismatch=True)
    port = ScriptReview(http_ports=http_ports)
    runtime.review = args['review'] = port
    runtime.process_bytes(RAW, 'image/png', SID)
    first = port.register_once(90)
    old_snapshot = port.read(first)['row'][:7]
    assert runtime.register(first)['state'] == '保留'
    assert db.calls == 0
    assert port.state['ui'][0][11] == 90  # draft is not regenerated
    assert port.state['requests'][1][:7] == old_snapshot
    original_result = deepcopy(port.state['requests'][1])
    second = port.register_once(100)
    assert first != second
    fresh = restored(runtime, transport, args)
    assert fresh.process_pending() == 1
    assert port.read(second)['row'][7] == '記帳済み' and db.calls == 3
    assert port.state['requests'][1] == original_result
    record = fresh.owner.read()['records'][SID]
    assert record['entry_history'][0]['request_id'] == first
    assert len(fresh.owner.read()['requests']) == 2 and ai.analyze_receipt.call_count == 1
    assert port.pending() == []


def test_real_script_hosted_intake_registration_claim_finish_without_pat(monkeypatch):
    runtime, _, _, review, _, _, _ = setup(monkeypatch, mismatch=True)
    runtime.process_bytes(RAW, 'image/png', SID)
    entry, row = review.cases[SID], review.row
    intake = script([hosted('intake',entry=entry,row=row)])
    assert intake['errors'] == [] and intake['gateGets'] == 0
    row[8] = 'register:'+entry['request_id']
    registered = script([dict(action='register',request_id=entry['request_id'])], cases=intake['cases'], ui=[row],
                        drive_bytes=list(RAW))
    assert registered['errors'] == []
    request = registered['requests'][1]
    claimed = script([hosted('claim',request_id=request[0],before=request)],
        cases=intake['cases'], requests=registered['requests'],ui=[row])
    assert claimed['errors'] == [] and claimed['gateGets'] == 0
    assert claimed['requests'][1][7] == '検証中'
    result = dict(request_id=request[0],snapshot_digest=request[6],state='記帳済み',issues=[],
                  posting_authority=False,financial_write_count=3,exact_readback=True,plan_digest='a'*64)
    finished = script([hosted('finish',request_id=request[0],before=claimed['requests'][1],result=result)],
        cases=intake['cases'],requests=claimed['requests'],ui=[row])
    assert finished['errors'] == [] and finished['requests'][1][7] == '記帳済み'


@pytest.mark.parametrize('kwargs', [dict(owner_override='wrong@example.invalid'),dict(busy=True),
    dict(properties={'RECEIPT_REVIEW_STOPPED':'unknown'}),dict(config={'enabled':False})])
def test_hosted_script_wrong_owner_lock_stop_or_disabled_denies_before_mutation(kwargs):
    result = script([hosted('pending')], **kwargs)
    assert result['errors'] and result['writes'] == result['gateGets'] == 0


def test_explicit_hosted_start_needs_owner_existing_trigger_and_never_clears_stop():
    result = script([dict(action='hosted_start')],config={'enabled':False})
    assert result['errors'] == [] and result['gateGets'] == result['writes'] == 0
    assert json.loads(result['properties']['RECEIPT_REVIEW_CONFIG_JSON'])['enabled'] is True
    for fault in (dict(no_trigger=True),dict(owner_override='wrong@example.invalid'),
                  dict(properties={'RECEIPT_REVIEW_STOPPED':'outcome_unconfirmed'})):
        denied = script([dict(action='hosted_start')],config={'enabled':False},**fault)
        assert denied['errors'] and denied['writes'] == 0
        assert json.loads(denied['properties']['RECEIPT_REVIEW_CONFIG_JSON'])['enabled'] is False
        if 'properties' in fault:
            assert denied['properties']['RECEIPT_REVIEW_STOPPED'] == 'outcome_unconfirmed'


def test_result_formula_keeps_bound_rejection_visible_after_rearm_and_native_completion():
    result = script([dict(action='formulas')])
    assert result['errors'] == []
    formula = result['outputs'][0][2]
    assert '前回 ' in formula and '修正後に登録' in formula and '記帳済み（読戻し確認済み）' in formula
    assert 'baseline_digest' in formula and 'snapshot_digest' in formula and 'financial_write_count' in formula
    # Verify generated Sheets syntax as well as JS syntax. Ignore quoted Sheets
    # strings (including doubled quote escapes) while balancing expression ().
    quoted = False
    depth = 0
    for char in formula:
        if char == '"': quoted = not quoted
        if not quoted:
            depth += int(char == '(') - int(char == ')')
            assert depth >= 0
    assert not quoted and depth == 0
