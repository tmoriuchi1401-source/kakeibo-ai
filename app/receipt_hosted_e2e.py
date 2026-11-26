"""Explicit hosted-only synthetic Google connection test. No financial/AI API.

Existing owner RPC, receipt state and native prepare are exercised against
precreated, pinned synthetic targets. This module never creates or resets them.
The production main authority is neither imitated nor weakened for this test.
"""
from copy import deepcopy
from hashlib import sha256
from io import BytesIO
import json
import os
import re

from .drive_run_state import DriveStateTransport, StateError
from .models import ReceiptItem, ReceiptResult
from .page_receipt_model import digest
from .receipt_hosted import HostedReceiptPipeline, KEY
from .receipt_hosted_review import open_review, SCOPES
from .receipt_hosted_writer import NativeReceiptWriter
from .receipt_pipeline import ReceiptPipeline
from .receipt_reimport_production import ReimportStore, ResultBinding
from .sheets import HEADERS, SheetsDB, SheetsReadPacer

SCHEMA = 'receipt-hosted-isolated-e2e-v1'
BRANCH = 'refs/heads/codex/issue-207-hosted-receipts'
WORKFLOW = 'tmoriuchi1401-source/kakeibo-ai/.github/workflows/state-cache-inspect.yml@' + BRANCH
PHASES = {'auth', 'preflight', 'intake', 'process', 'restore', 'unknown-create', 'unknown-restore'}
ROLES = ('normal', 'exception', 'unknown', *('unused-' + str(i) for i in range(1, 8)))
CATEGORIES = [('食費', '食品')]
PREFIX = 'KakeiboAI receipt E2E '


def boundary(env, phase):
    if (phase not in PHASES or env.get('GITHUB_ACTIONS') != 'true'
            or env.get('GITHUB_REPOSITORY') != 'tmoriuchi1401-source/kakeibo-ai'
            or env.get('GITHUB_REF') != BRANCH or env.get('GITHUB_EVENT_NAME') != 'workflow_dispatch'
            or env.get('GITHUB_ACTOR') != 'tmoriuchi1401-source'
            or env.get('GITHUB_TRIGGERING_ACTOR') != 'tmoriuchi1401-source'
            or env.get('GITHUB_RUN_ATTEMPT') != '1'
            or env.get('RUNNER_ENVIRONMENT') != 'github-hosted' or env.get('RUNNER_OS') != 'Linux'
            or env.get('GITHUB_WORKFLOW_REF') != WORKFLOW
            or not re.fullmatch('[a-f0-9]{40}', env.get('E2E_EXPECTED_SHA', ''))
            or env.get('GITHUB_SHA') != env['E2E_EXPECTED_SHA']
            or env.get('RUNNER_DEBUG') == '1' or env.get('GEMINI_API_KEY')):
        raise StateError('receipt_e2e_hosted_boundary_required')


def config(env, sa):
    """Use existing encrypted production references only to DENY collisions."""
    from .private_state_bindings import VARIABLES, unwrap
    from .drive_receipts import normalize_folder_id
    value = json.loads(env.get('RECEIPT_HOSTED_E2E_CONFIG_JSON', 'null'))
    fields = {'schema', 'state_file_id', 'state_folder_id', 'ledger_spreadsheet_id',
              'manifest_digest', 'fixture_source_ids', 'review_binding_digest', 'owner_email'}
    if (type(value) is not dict or set(value) != fields or value['schema'] != SCHEMA
            or type(value['fixture_source_ids']) is not list or len(value['fixture_source_ids']) != 10
            or len(set(value['fixture_source_ids'])) != 10
            or any(not re.fullmatch('[A-Za-z0-9_-]{10,150}', x) for x in
                   [value['state_file_id'], value['state_folder_id'], value['ledger_spreadsheet_id'], *value['fixture_source_ids']])
            or any(not re.fullmatch('[a-f0-9]{64}', value[k]) for k in ('manifest_digest', 'review_binding_digest'))):
        raise StateError('receipt_e2e_configuration_invalid')
    review = json.loads(env['RECEIPT_HOSTED_REVIEW_BINDING'])
    if digest(review) != value['review_binding_digest'] or review['owner_email'] != value['owner_email']:
        raise StateError('receipt_e2e_review_binding_changed')
    forbidden = {unwrap(name, env[name], sa['private_key']) for name in VARIABLES}
    old = json.loads(env['RECEIPT_CONFIRMATION_BINDING'])
    forbidden.add(unwrap('RECEIPT_REIMPORT_FILE_ID', old['file'], sa['private_key']))
    forbidden.add(env['SPREADSHEET_ID'])
    forbidden.update(normalize_folder_id(env[k]) for k in ('RECEIPT_DRIVE_FOLDER_ID', 'PROCESSED_DRIVE_FOLDER_ID'))
    selected = {value['state_file_id'], value['state_folder_id'], value['ledger_spreadsheet_id'],
                review['spreadsheet_id'], review['folder_id'], *value['fixture_source_ids']}
    if selected & forbidden or value['state_file_id'] in value['fixture_source_ids']:
        raise StateError('receipt_e2e_production_target_forbidden')
    return value, review


def authenticate_owner(env):
    """Read-only userinfo using the existing grant; no Script or state effects."""
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import AuthorizedSession
    info = json.loads(env.get('GOOGLE_RECEIPT_OWNER_TOKEN_JSON','null'))
    if (type(info) is not dict or info.get('type','authorized_user') != 'authorized_user'
            or info.get('token_uri') != 'https://oauth2.googleapis.com/token'
            or any(type(info.get(k)) is not str or not info[k] for k in
                   ('refresh_token','client_id','client_secret','owner_email','project_id'))
            or not set(SCOPES) <= set(info.get('scopes',[]))):
        raise StateError('receipt_e2e_existing_owner_grant_required')
    session = AuthorizedSession(Credentials.from_authorized_user_info(info,scopes=list(SCOPES)),max_refresh_attempts=0)
    response = session.get('https://www.googleapis.com/oauth2/v2/userinfo',timeout=20,allow_redirects=False)
    if response.status_code != 200:
        raise StateError('receipt_e2e_owner_authentication_rejected')
    actual = response.json()
    if actual.get('email') != info['owner_email'] or actual.get('verified_email') is not True:
        raise StateError('receipt_e2e_owner_identity_mismatch')
    return dict(status='owner_authenticated',owner_match=True,required_scopes_present=True,
                existing_grant_scope_count=len(info['scopes']),hosted_runner=True,
                financial_write_count=0,real_ai_requests=0,google_state_writes=0)


def fixture_bytes(role):
    """Pure synthetic one-page PDF for explicit operator setup, never uploaded here."""
    if role not in ROLES: raise StateError('receipt_e2e_fixture_role_invalid')
    from pypdf import PdfWriter
    from pypdf.generic import DictionaryObject, NameObject, ArrayObject, NumberObject, DecodedStreamObject, TextStringObject
    writer = PdfWriter()
    page = writer.add_blank_page(width=400, height=300)
    cid = DictionaryObject({NameObject('/Type'): NameObject('/Font'), NameObject('/Subtype'): NameObject('/CIDFontType0'),
        NameObject('/BaseFont'): NameObject('/HeiseiKakuGo-W5'), NameObject('/DW'): NumberObject(1000),
        NameObject('/CIDSystemInfo'): DictionaryObject({NameObject('/Registry'): TextStringObject('Adobe'),
            NameObject('/Ordering'): TextStringObject('Japan1'), NameObject('/Supplement'): NumberObject(5)})})
    font = DictionaryObject({NameObject('/Type'): NameObject('/Font'), NameObject('/Subtype'): NameObject('/Type0'),
        NameObject('/BaseFont'): NameObject('/HeiseiKakuGo-W5'), NameObject('/Encoding'): NameObject('/UniJIS-UTF16-H'),
        NameObject('/DescendantFonts'): ArrayObject([writer._add_object(cid)])})
    page[NameObject('/Resources')] = DictionaryObject({NameObject('/Font'): DictionaryObject({NameObject('/F1'): writer._add_object(font)})})
    lines = ['レシート 商品 合計 現金', 'KakeiboAI SYNTHETIC ' + role, '2026-11-26', 'Synthetic item 100', '小計 100円 現金 100円']
    body = 'BT /F1 14 Tf 20 260 Td 22 TL ' + ' '.join('<' + x.encode('utf-16-be').hex() + '> Tj T*' for x in lines) + ' ET'
    stream = DecodedStreamObject(); stream.set_data(body.encode('ascii'))
    page[NameObject('/Contents')] = writer._add_object(stream)
    result = BytesIO(); writer.write(result)
    return result.getvalue()


def initial_fixture(sources, *, owner_email, sa_email, spreadsheet_id, folder_id):
    """Pure payload builder; caller separately verifies/creates isolated targets.

    Ten entries retain the existing ReimportStore manifest contract; only the
    first three are used by this E2E. No real source or historical state copy.
    """
    if len(sources) != 10: raise StateError('receipt_e2e_ten_fixture_sources_required')
    manifest = dict(spreadsheet_id=spreadsheet_id, folder_id=folder_id, owner_email=owner_email, sa_email=sa_email,
        destination='https://generativelanguage.googleapis.com', model='synthetic-local-no-ai-v1', sources=deepcopy(sources))
    value = dict(schema='receipt-reimport-v1', manifest=manifest,
        records={s['source_id']: {'phase':'held'} for s in sources})
    value[KEY] = dict(schema=KEY, enabled=True, financial_write_enabled=False, records={}, requests={}, stopped=False)
    return value


class ReadOnlyFinancialDB:
    """Read-only Google scope AND Python rejection of every accounting write."""
    def __init__(self, db): self.db, self.sid, self.svc = db, db.sid, db.svc
    def categories(self): return self.db.categories()
    def get_raw(self, region): return self.db.get_raw(region)
    def _execute_sheet_read(self, request): return self.db._execute_sheet_read(request)
    def denied(self, *args, **kwargs): raise StateError('receipt_e2e_financial_write_forbidden')
    append = append_raw = update_row_raw = clear = ensure_sheet = update = denied


class SyntheticAnalysis:
    def __init__(self, sources): self.roles = {s['sha256']: role for s, role in zip(sources, ROLES)}; self.calls = 0
    def analyze_receipt(self, raw, mime, categories, **policy):
        role = self.roles.get(sha256(raw).hexdigest())
        if role not in ROLES[:3] or mime != 'application/pdf' or categories != CATEGORIES:
            raise StateError('receipt_e2e_non_fixture_analysis_forbidden')
        self.calls += 1
        if role == 'unknown': raise TimeoutError('synthetic unknown without AI')
        return ReceiptResult(date='2026-11-26', merchant='KakeiboAI SYNTHETIC', total=100,
            items=[ReceiptItem(name='Synthetic item',amount=80 if role == 'exception' else 100,
                               major_category='食費',minor_category='食品')])


def metadata(drive, fid):
    return drive.files().get(fileId=fid, fields='id,name,mimeType,parents,version,trashed,owners(emailAddress),permissions(type,role,emailAddress,deleted)').execute(num_retries=0)


def private_target(drive, fid, *, owner, sa_email, mime, prefix, parent=None):
    value = metadata(drive, fid)
    permissions = {(p.get('type'),p.get('role'),p.get('emailAddress')) for p in value.get('permissions',[]) if not p.get('deleted')}
    if (value.get('id') != fid or value.get('trashed') or value.get('mimeType') != mime
            or not value.get('name','').startswith(prefix)
            or [o.get('emailAddress') for o in value.get('owners',[])] != [owner]
            or permissions != {('user','owner',owner),('user','writer',sa_email)}
            or parent is not None and value.get('parents') != [parent]):
        raise StateError('receipt_e2e_private_target_mismatch')
    return value


def connect(env, phase):
    boundary(env, phase)
    from .settings import service_account_source
    from .google_clients import drive_service, read_only_drive_service, read_only_sheets_service, download_drive_file
    from pathlib import Path
    path, info = service_account_source(); info = info or json.loads(Path(path).read_bytes())
    cfg, review_binding = config(env, info)
    reader = read_only_drive_service()
    owner, sa_email = cfg['owner_email'], info['client_email']
    for fid in {cfg['state_folder_id'],review_binding['folder_id']}:
        private_target(reader,fid,owner=owner,sa_email=sa_email,mime='application/vnd.google-apps.folder',prefix=PREFIX)
    for fid in {cfg['ledger_spreadsheet_id'],review_binding['spreadsheet_id']}:
        private_target(reader,fid,owner=owner,sa_email=sa_email,mime='application/vnd.google-apps.spreadsheet',prefix=PREFIX)
    private_target(reader,cfg['state_file_id'],owner=owner,sa_email=sa_email,mime='application/json',prefix=PREFIX,parent=cfg['state_folder_id'])
    transport = DriveStateTransport(drive_service(),ResultBinding(cfg['state_folder_id'],cfg['state_file_id']))
    store = ReimportStore(transport,cfg['manifest_digest'],cfg['ledger_spreadsheet_id'])
    manifest = store.value['manifest']
    sources = manifest['sources']
    if (manifest.get('owner_email') != owner or manifest.get('sa_email') != sa_email
            or manifest.get('model') != 'synthetic-local-no-ai-v1'
            or manifest.get('folder_id') != review_binding['folder_id']
            or [s['source_id'] for s in sources] != cfg['fixture_source_ids']
            or any(r.get('phase') != 'held' for r in store.value['records'].values())
            or set(store.value) != {'schema','manifest','records',KEY}):
        raise StateError('receipt_e2e_synthetic_state_required')
    for source, role in zip(sources, ROLES):
        meta = private_target(reader,source['source_id'],owner=owner,sa_email=sa_email,mime='application/pdf',prefix=PREFIX+role,parent=review_binding['folder_id'])
        if meta['version'] != source['version']: raise StateError('receipt_e2e_fixture_version_changed')
    def fresh(source):
        saved = next((s for s in sources if s['source_id'] == source['source_id']),None)
        if saved is None or any(source[k] != saved[k] for k in ('source_id','sha256','version','mime_type')):
            raise StateError('receipt_e2e_source_not_pinned')
        before = metadata(reader, source['source_id'])
        raw = download_drive_file(source['source_id'],reader)
        if (sha256(raw).hexdigest() != source['sha256'] or metadata(reader,source['source_id']) != before
                or before['version'] != source['version'] or before.get('parents') != [review_binding['folder_id']]):
            raise StateError('receipt_e2e_fixture_changed')
        return True
    review = open_review(env,manifest,cfg['ledger_spreadsheet_id'],review_binding['folder_id'])
    db = ReadOnlyFinancialDB(SheetsDB(cfg['ledger_spreadsheet_id'],service=read_only_sheets_service(),read_pacer=SheetsReadPacer()))
    def empty_ledger():
        if db.categories() != CATEGORIES: raise StateError('receipt_e2e_category_fixture_changed')
        for title, header in ((t,HEADERS[t]) for t in ('レシート','支出明細','取込データ')):
            rows = db.get_raw("'"+title+"'!A1:Z10000")
            if rows != [header]: raise StateError('receipt_e2e_financial_ledger_not_empty')
    empty_ledger()
    class PlanOnlyAuthority:
        def check(self):
            boundary(env,phase)
            state = store.value[KEY]
            if state['financial_write_enabled'] is not False or state['stopped'] or store.transport.read() != store.payload:
                raise StateError('receipt_e2e_plan_only_state_changed')
            empty_ledger()
    ai = SyntheticAnalysis(sources)
    pipeline = ReceiptPipeline(db,ai,clock=lambda:'2026-11-26 12:00:00')
    writer = NativeReceiptWriter(db,PlanOnlyAuthority(),clock=pipeline.clock)
    kwargs = dict(sources={s['source_id']:s for s in sources[:3]},verify_source=fresh,review=review,writer=writer,native_apply=False)
    return cfg,store,pipeline,kwargs,ai,empty_ledger,lambda s:download_drive_file(s['source_id'],reader)


def execute(env, phase):
    boundary(env,phase)
    if phase == 'auth': return authenticate_owner(env)
    cfg,store,pipeline,kwargs,ai,empty_ledger,load = connect(env,phase)
    ids = cfg['fixture_source_ids']; normal, exception, unknown = ids[:3]
    before = store.payload
    if phase == 'unknown-restore':
        if store.value[KEY]['records'].get(unknown,{}).get('state') != 'ai_started':
            raise StateError('receipt_e2e_unknown_reservation_required')
        try: HostedReceiptPipeline(store,pipeline,**kwargs)
        except StateError as error:
            if str(error) != 'receipt_hosted_unknown_no_retry': raise
        else: raise StateError('receipt_e2e_unknown_replay_not_blocked')
        if ai.calls or store.transport.read() != before: raise StateError('receipt_e2e_unknown_changed')
        return dict(status='unknown_restored_without_resend',financial_write_count=0,real_ai_requests=0,state_digest=sha256(before).hexdigest())
    runtime = HostedReceiptPipeline(store,pipeline,**kwargs)
    if phase == 'intake':
        if runtime.owner.read()['records']: raise StateError('receipt_e2e_intake_already_attempted')
        for sid in (normal,exception):
            source=kwargs['sources'][sid]
            runtime.process_bytes(load(source),source['mime_type'],sid)
        current=runtime.owner.read()['records']
        if (current[normal]['state'] != 'validated_unposted' or 'native_plan' not in current[normal]
                or current[exception]['state'] != 'review'
                or kwargs['review'].lookup_source(normal,kwargs['sources'][normal]['sha256']) is not None):
            raise StateError('receipt_e2e_intake_result_invalid')
    elif phase == 'process':
        if runtime.process_pending() != 1: raise StateError('receipt_e2e_one_registration_required')
    elif phase == 'restore':
        current=runtime.owner.read()['records']
        if current.get(normal,{}).get('state') != 'validated_unposted': raise StateError('receipt_e2e_normal_plan_missing')
        for record in current.values():
            if 'native_plan' in record and kwargs['writer'].reconcile(record['native_plan'])[0] != 'not_written':
                raise StateError('receipt_e2e_unexpected_financial_effect')
        for rid,request in runtime.owner.read()['requests'].items():
            row=kwargs['review'].read(rid)['row']
            if request['state'] != 'complete' or json.loads(row[8]) != request['result']:
                raise StateError('receipt_e2e_result_not_restored')
        if store.transport.read() != before: raise StateError('receipt_e2e_restore_changed_state')
    elif phase == 'unknown-create':
        current=runtime.owner.read()['records']
        if (current.get(normal,{}).get('state') != 'validated_unposted'
                or current.get(exception,{}).get('state') != 'validated_unposted' or kwargs['review'].pending()):
            raise StateError('receipt_e2e_complete_before_unknown')
        source=kwargs['sources'][unknown]
        try: runtime.process_bytes(load(source),source['mime_type'],unknown)
        except TimeoutError: pass
        else: raise StateError('receipt_e2e_unknown_not_reserved')
        if store.value[KEY]['records'][unknown]['state'] != 'ai_started': raise StateError('receipt_e2e_unknown_state_missing')
    elif phase != 'preflight': raise StateError('receipt_e2e_phase_invalid')
    empty_ledger()
    state=store.value[KEY]
    return dict(status='complete',phase=phase,records=len(state['records']),requests=len(state['requests']),
        normal_bypassed_review='entry' not in state['records'].get(normal,{}),
        native_plans=sum('native_plan' in r for r in state['records'].values()),
        review_ready=sum(r['state']=='review' for r in state['records'].values()),
        synthetic_analysis_calls=ai.calls,real_ai_requests=0,financial_write_count=0,
        state_digest=sha256(store.payload).hexdigest())


def main():
    try: result=execute(dict(os.environ),os.environ.get('E2E_PHASE',''))
    except Exception as error:
        code=str(error) if isinstance(error,StateError) and re.fullmatch('[a-z0-9_]+',str(error)) else 'receipt_e2e_failed_closed'
        print(json.dumps(dict(status='failed_closed',error=code,financial_write_count=0,real_ai_requests=0)))
        raise SystemExit(1) from None
    print(json.dumps(result,sort_keys=True))


if __name__ == '__main__': main()
