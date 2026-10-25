"""Default-off, protected local one-shot entry; no ambient credentials.

Real execution is a separate operator approval, not enabled by this PR.
Only this whole-process composition is eligible for such approval. Low-level
adapters alone do not establish human authority. Run in a disposable process,
never a notebook/REPL; Python reference clearing is not secure RAM erasure.
"""
import argparse
import json
from pathlib import Path
import secrets
import subprocess
import time
import traceback
from urllib.parse import urlencode

from . import google_a0g as a0
from . import google_a0g_oauth as oauth
from .google_a0g_ledger import Authority, LocalLedger
from .google_a0g_loopback import LoopbackReceiver

REPO = Path(__file__).resolve().parents[1]


def verify_checkout(expected_sha):
    def git(*args):
        return subprocess.check_output(['git',*args],cwd=REPO,stderr=subprocess.DEVNULL,text=True).strip()
    if (git('rev-parse','HEAD')!=expected_sha or git('status','--porcelain','--untracked-files=all')):
        a0.fail('reviewed_clean_checkout_required')


class PrivateBrowser:
    """Explicit reviewed Edge install + InPrivate; never default profile/URL log.

    InPrivate is not an OS erasure guarantee. Operator preflight must verify
    extensions, crash capture, history/session sync and the actual browser.
    All InPrivate windows used for this attempt must be closed by the operator.
    """
    def __init__(self, executable):
        p = Path(executable)
        if not p.is_absolute() or p.name.lower()!='msedge.exe' or not p.is_file():
            a0.fail('reviewed_private_browser_required')
        self._path = p; self._process = None
    def open(self,url):
        self._process = subprocess.Popen([str(self._path),'--inprivate','--new-window',url],
            stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,close_fds=True)
    def close(self):
        # Edge can delegate the window to another process. Do not kill unrelated
        # browser processes; manual closure is an explicit pre/postcondition.
        self._process = None


class RunWires:
    """Durable, exact ordered debit gates; regenerated adapters cannot reset them."""
    def __init__(self,run,contract,sha):
        self._run=run; self._plan=a0.request_plan(contract,expected_sha=sha)
        self._client=contract['preflight']['oauth_client_id']
    def exchange(self,params,**options):
        if params.get('client_id')!=self._client or params.get('grant_type')!='authorization_code':
            a0.fail('wire_exchange_binding')
        self._run.debit('exchange')
        return oauth.token_post(params,**options)
    def start_metadata(self):
        self._run.debit('session')
    def get(self,url,headers,**options):
        ordinal = None
        for i,(base,q) in enumerate(self._plan):
            encoded=urlencode({k:str(v).lower() if type(v) is bool else v for k,v in q.items()})
            if url==base+'?'+encoded: ordinal=i
        if ordinal is None: a0.fail('wire_plan_changed')
        self._run.debit('get',ordinal=ordinal)
        return a0.https_get(url,headers,**options)
    def close(self): self._run=None; self._plan=(); self._client=None


def _clear_exception(exc):
    # No exception locals/body/URL leaves this boundary. clear_frames cannot
    # erase Python string copies, OS memory, page files or browser state.
    if exc.__traceback__ is not None: traceback.clear_frames(exc.__traceback__)
    exc.__traceback__ = None
    exc.__context__ = None
    exc.__cause__ = None


def run_once(*,authority_path,authority_digest,contract_path,expected_sha,enabled=False):
    if enabled is not True: return {'status':'DISABLED','runner_write_allowed':False}
    run=receiver=browser=wires=exchange=grant=callback=result=contract=authority=None
    state=verifier=params=None
    completed=False
    try:
        verify_checkout(expected_sha)
        authority=Authority.load(authority_path,expected_digest=authority_digest,repo_root=REPO,now=time.time())
        d=authority.document
        raw=Path(contract_path).read_bytes()
        if len(raw)>65536: a0.fail('contract_size')
        contract=a0.validate_contract(json.loads(raw),expected_sha=expected_sha)
        if d['candidate_sha']!=expected_sha or d['contract_digest']!=a0.contract_digest(contract,expected_sha=expected_sha):
            a0.fail('authority_contract_binding')
        # Validate browser locally before spending; constructor does not launch it.
        browser=PrivateBrowser(d['browser_executable'])
        ledger=LocalLedger(d['registry_path'],registry_id=d['registry_id'],repo_root=REPO)
        run=ledger.consume(authority,now=time.time())
        # From this point all crash/interruption paths leave both approvals spent.
        handoff=authority.handoff()
        wires=RunWires(run,contract,expected_sha)
        state=secrets.token_urlsafe(32); verifier=secrets.token_urlsafe(64)
        receiver=LoopbackReceiver(contract=contract,expected_sha=expected_sha,state=state,
            seconds=min(120,handoff.expires_at-time.time()))
        params=a0.authorization_parameters(contract,expected_sha=expected_sha,approval=handoff,
            state=state,code_verifier=verifier,redirect_uri=receiver.redirect_uri,now=time.time())
        browser.open('https://accounts.google.com/o/oauth2/v2/auth?'+urlencode(params))
        callback=receiver.receive()
        exchange=oauth.ApprovedCodeExchange(contract,expected_sha=expected_sha,approval=handoff,
            enabled=True,sender=wires.exchange)
        # Desktop is a public client: PKCE exchange uses no persisted client secret.
        grant=exchange.exchange(callback=callback,expected_state=state,code_verifier=verifier,
            redirect_uri=receiver.redirect_uri,client_secret=None)
        callback.clear(); callback=None; state=verifier=params=None
        exchange.close(); exchange=None
        wires.start_metadata()
        result=a0.run_metadata_audit(contract,expected_sha=expected_sha,approval=handoff,
            grant=grant,enabled=True,sender=wires.get)
        completed=True
        return {'status':'METADATA_VERIFIED_NO_GO','runner_write_allowed':False,
                'requests':6,'blockers':result['blockers']}
    except BaseException as exc:
        _clear_exception(exc)
        return {'status':'STOPPED_NO_GO','runner_write_allowed':False}
    finally:
        if callback is not None: callback.clear()
        if receiver is not None:
            try: receiver.close()
            except BaseException: pass
        if browser is not None: browser.close()
        if exchange is not None: exchange.close()
        if grant is not None: grant.close()
        grant=callback=result=contract=authority=state=verifier=params=None
        if wires is not None: wires.close()
        if run is not None:
            try: run.close(completed=completed)
            except BaseException: pass  # durable CONSUMED_UNKNOWN remains spent


def main():
    parser=argparse.ArgumentParser(description='A0-G protected one-shot entry; requires separate human approval')
    parser.add_argument('--authority',required=True)
    parser.add_argument('--authority-digest',required=True)
    parser.add_argument('--contract',required=True)
    parser.add_argument('--sha',required=True)
    parser.add_argument('--execute-approved',action='store_true')
    args=parser.parse_args()
    result=run_once(authority_path=args.authority,authority_digest=args.authority_digest,
        contract_path=args.contract,expected_sha=args.sha,enabled=args.execute_approved)
    print(json.dumps(result))  # Fixed status/blocker codes only, never raw metadata.
    # End the disposable process; no credential-bearing interactive continuation.
    return 0 if result['status']=='METADATA_VERIFIED_NO_GO' else 1


if __name__=='__main__': raise SystemExit(main())
