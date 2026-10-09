"""Main-only, reference-only manual posting of one explicitly approved Unit."""
import json,os,re,subprocess,time,uuid
from ..drive_run_state import StateError
from ..production_flow import verify_execution_boundary
from ..sheets import SheetsDB
from . import runner as p,unit_runner as u,unit_posting as w
from .readers import Readers
from .posting_live import PostingStore,CardProjection

def boundary(env,head):
    verify_execution_boundary(env,head)
    if (env.get('GITHUB_EVENT_NAME')!='workflow_dispatch' or env.get('KAKEIBO_LEGACY_DISABLED')!='true'
        or env.get('GITHUB_WORKFLOW_REF')!='tmoriuchi1401-source/kakeibo-ai/.github/workflows/receipt-unit-posting-canary.yml@refs/heads/main'
        or env.get('RECEIPT_UNIT_POST_CONFIRM')!='POST_CONFIRMED_RECEIPT_UNIT'
        or env.get('RECEIPT_UNIT_POST_OPERATION') not in {'apply','replay'} or env.get('RUNNER_DEBUG')=='1'
        or env.get('SPREADSHEET_ID')!=p.SID or not p.UUID.fullmatch(env.get('RECEIPT_UNIT_REQUEST_ID',''))
        or any(not u.FILE_ID.fullmatch(env.get(k,'')) for k in ('RECEIPT_UNIT_CONTEXT_ID','RECEIPT_UNIT_POSTING_FILE'))
        or env.get('RECEIPT_UNIT_CONTEXT_ID')==env.get('RECEIPT_UNIT_POSTING_FILE')):
        raise StateError('unit_posting_execution_boundary_required')

class UnitPostingStore(PostingStore):
    validate_state=staticmethod(w.validate_state)
    def __init__(self,info,cfg,fid,context_id,*,http=None):
        if fid in {context_id,*cfg['drive']['baseline_files'],cfg['drive']['inbox']}:
            raise StateError('unit_posting_file_scope_invalid')
        super().__init__(info,cfg,fid,http=http)

def main():
    try:
        env=dict(os.environ);head=subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip();boundary(env,head)
        info=json.loads(env['GOOGLE_SERVICE_ACCOUNT_JSON']);fid=env['RECEIPT_UNIT_CONTEXT_ID'];rid=env['RECEIPT_UNIT_REQUEST_ID']
        cfg=u.context(info,fid)
        readers=Readers(cfg['config'],info,owner_actor_id=cfg['owner_actor_id'],allowed_pages=u.PAGES)
        p.QueuePort(info) # Verify the existing hidden queue identity; no writes.
        store=UnitPostingStore(info,cfg['config'],env['RECEIPT_UNIT_POSTING_FILE'],fid)
        result=w.execute(readers,store,SheetsDB(p.SID),CardProjection(info,readers),rid,fid,
            attempt_id=str(uuid.uuid4()),now=int(time.time()),operation=env['RECEIPT_UNIT_POST_OPERATION'])
        print(json.dumps(result,sort_keys=True))
    except Exception as error:
        category=str(error) if isinstance(error,StateError) and re.fullmatch('[A-Za-z0-9_]{1,100}',str(error)) else 'unit_posting_failed_readback_required'
        print(json.dumps(dict(status='unit_posting_stopped',failure_category=category,automatic_retry=False)))
        raise SystemExit(1) from None

if __name__=='__main__':main()
