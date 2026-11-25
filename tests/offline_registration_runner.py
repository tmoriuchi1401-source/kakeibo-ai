"""Offline composition for the short runner. No real Google mode exists.

Reuses #159's probe/adapter/checkpoint and exact publisher request validation.
All simulated remote files and keys stay in a caller-provisioned private dir.
"""
from base64 import b64decode
from copy import deepcopy
import json
import os
from pathlib import Path
import signal
import shutil
import subprocess
import sys
import tempfile
import time
import uuid

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
sys.path.insert(0,str(Path(__file__).resolve().parent))

from app.iphone_draft_journal import exclusive
from app.iphone_persistent_drafts import LIMIT,EncryptedDraftIO
from staging_shadow.live_synthetic_sheets import validate_writes
from staging_shadow.registration_runner import Control,ShortRunner,MODE,atomic,fingerprint

SCENARIOS={'normal','native_unknown','publish_lost','publish_before_timeout','worker_timeout'}


def write_json(path,value,*,create=False):
    atomic(path,json.dumps(value,ensure_ascii=True,sort_keys=True).encode(),create=create)


def provision(directory,*,scenario='normal'):
    if scenario not in SCENARIOS:raise ValueError('offline scenario required')
    directory=Path(directory)
    directory.mkdir(parents=False,exist_ok=False)
    from pytest import MonkeyPatch
    from registration_fixtures import Rig
    from sheets_only_fixtures import no_clients
    from test_live_synthetic_sheets import provision as fixture_provision
    m=MonkeyPatch();no_clients(m)
    try:
        with tempfile.TemporaryDirectory(dir=directory) as folder:
            r=Rig(m,Path(folder));raw,sources,profile,acl=fixture_provision(r)
            config=dict(baseline=raw,sources=sources,profile=profile,acl=acl,
                        runner_mode=MODE,scenario=scenario,
                        allow_fault_injection=scenario=='native_unknown')
            write_json(directory/'config.json',config,create=True)
            requested=deepcopy(raw)
            requested['sheets'][0]['data'][0]['rowData'][1]['values'][1]['userEnteredValue']={'stringValue':'登録 1'}
            write_json(directory/'sheet.json',requested,create=True)
            write_json(directory/'sources.json',sources,create=True)
            write_json(directory/'access.json',dict(authorized=True,principal='synthetic-connected-owner'),create=True)
            r.assert_safe()
        Control.provision(directory,'UNINITIALIZED')
    finally:m.undo()


class OfflinePort:
    mode=MODE
    bounded_io=True

    def __init__(self,directory):
        self.root=Path(directory);self.config=json.loads((self.root/'config.json').read_bytes())
        if (self.config.get('runner_mode')!=MODE or self.config.get('scenario') not in SCENARIOS
                or self.config['baseline']['spreadsheetId']!='synthetic-google-book'
                or self.config['profile']['email']!='owner@example.test'
                or self.config.get('allow_checkpoint_archive') or self.config.get('display_cases')):
            raise RuntimeError('offline provision required; live config refused')

    def checkpoint(self):
        path=self.root/'checkpoint.json'
        return fingerprint(path.read_bytes()) if path.exists() else 'UNINITIALIZED'

    def persist_checkpoint(self,budget):
        if budget<=0:raise TimeoutError()
        # #159 already fsyncs file + directory before its worker reply. This
        # local proof is NOT durable state for another GitHub-hosted runner.
        return self.checkpoint()

    def authorized(self):
        access=json.loads((self.root/'access.json').read_bytes())
        return access==dict(authorized=True,principal='synthetic-connected-owner')

    def restored_pending(self):
        path=self.root/'checkpoint.json'
        if not path.exists():return False
        checkpoint=json.loads(path.read_bytes())
        if checkpoint['uncertain'] or checkpoint['projection_unknown']:return True
        for name,key,scope in [('journal','key','native_scope'),('outer','outer_key','outer_scope')]:
            cipher=EncryptedDraftIO(None,checkpoint[key].encode(),checkpoint[scope])
            value=cipher.decode(b64decode(checkpoint[name]['raw']))
            if any(r['status']=='ATTEMPTING' for r in value.get('confirmation',{}).get('requests',{}).values()):return True
            details=value['details']
            for slot in ('sheets_registration','sheets_only_review','iphone_sheet_handoff'):
                if any(r['status'] in {'ATTEMPTING','UNKNOWN'} for r in details.get(slot,{}).get('requests',{}).values()):return True
            if any(r['state'] in {'ATTEMPTING','SENDING'} for r in details.get('live_synthetic_sheet_outbox',{}).values()):return True
        return False

    def capacity(self):
        if len(list(self.root.glob('request-*.json')))>=48:return False
        path=self.root/'checkpoint.json'
        if not path.exists():return True
        data=path.read_bytes()
        if len(data)>1_600_000:return False  # Stop BEFORE the existing 2MB bound.
        value=json.loads(data)
        return all(len(b64decode(value[k]['raw']))<int(LIMIT*0.8) for k in ('journal','outer'))

    def capture(self):
        if not self.authorized():raise PermissionError('synthetic access revoked')
        return dict(raw=json.loads((self.root/'sheet.json').read_bytes()),
                    sources=json.loads((self.root/'sources.json').read_bytes()),
                    profile=deepcopy(self.config['profile']),acl=deepcopy(self.config['acl']))

    def same_inputs(self,a,b):
        def inputs(capture):
            value=deepcopy(capture)
            for row in value['raw']['sheets'][0]['data'][0]['rowData'][1:]:
                row['values'][2].pop('userEnteredValue',None)
                row['values'][1].pop('dataValidation',None)
            return value
        return inputs(a)==inputs(b)

    def backend(self,request,budget):
        # Hard kill of a stuck worker leaves the parent's durable PENDING.
        name='request-'+uuid.uuid4().hex+'.json'
        write_json(self.root/name,request,create=True)
        deadline=time.monotonic()+budget
        command=[sys.executable,str(Path(__file__).resolve()),'worker',str(self.root),
                 '--enable-offline','--request-name',name,'--deadline',str(deadline)]
        p=subprocess.Popen(command,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                           creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        try:stdout,stderr=p.communicate(timeout=budget)
        except subprocess.TimeoutExpired:
            # Windows venv launchers may not have the interpreter's PID. The
            # newly spawned worker reports its own PID bound to this nonce.
            # A late-starting worker also checks the shared monotonic deadline
            # before doing any backend IO, so it cannot run after supervision.
            pidfile=self.root/(name+'.pid')
            if pidfile.exists():
                owner=json.loads(pidfile.read_bytes())
                if owner['request_sha256']==fingerprint((self.root/name).read_bytes()):
                    try:os.kill(owner['pid'],signal.SIGTERM)
                    except ProcessLookupError:pass
            p.kill()
            try:p.communicate(timeout=2)
            except subprocess.TimeoutExpired:pass
            raise TimeoutError('bounded synthetic worker stopped') from None
        if p.returncode or len(stdout)>4096 or len(stderr)>4096:raise RuntimeError('worker stopped')
        response=json.loads((self.root/(name+'.response')).read_bytes())
        if response['request_sha256']!=fingerprint((self.root/name).read_bytes()):raise RuntimeError('worker receipt changed')
        result=response['result']
        if result.get('error'):raise RuntimeError('backend stopped')
        return result

    def poll(self,a,b,budget):
        return self.backend(dict(op='fault_poll' if self.config['scenario']=='native_unknown' else 'poll',
            first=a['raw'],second=b['raw'],sources=a['sources'],second_sources=b['sources'],
            profile=b['profile'],acl=b['acl']),budget)

    def ready(self,key,capture,budget):
        return self.backend(dict(op='ready',id=key,**capture),budget)

    def publish(self,requests,capture,budget):
        if budget<=0:raise TimeoutError()
        if not self.authorized():raise PermissionError()
        if self.config['scenario']=='publish_before_timeout':raise TimeoutError('synthetic send interrupted')
        with exclusive(self.root/'sheet.lock'):
            if self.capture()!=capture:raise RuntimeError('sheet changed after claim')
            raw=deepcopy(capture['raw']);sheet=raw['sheets'][0]
            validate_writes(requests,sheet['properties']['sheetId'])
            for request in requests:
                u=request['updateCells'];p=u['range']
                cell=sheet['data'][0]['rowData'][p['startRowIndex']]['values'][p['startColumnIndex']]
                cell[u['fields']]=deepcopy(u['rows'][0]['values'][0][u['fields']])
            write_json(self.root/'sheet.json',raw)
        if self.config['scenario']=='publish_lost':raise TimeoutError('synthetic response lost AFTER apply')
        return dict(published=True,financial_writes=0,authority_calls=0)

    def inspect_readonly(self):
        # Existing #159 inspect saves its checkpoint. Execute it on a private
        # COPY so a fenced original remains byte-for-byte unchanged.
        with tempfile.TemporaryDirectory(dir=self.root,prefix='lookup-') as folder:
            target=Path(folder)
            for name in ('config.json','checkpoint.json'):
                shutil.copyfile(self.root/name,target/name)
            for p in self.root.glob('archive-*.json'):shutil.copyfile(p,target/p.name)
            copy=OfflinePort(target)
            return copy.backend(dict(op='inspect'),30)


def main():
    import argparse
    parser=argparse.ArgumentParser()
    parser.add_argument('command',choices=['provision','run','inspect','stop','worker'])
    parser.add_argument('directory',type=Path)
    parser.add_argument('--enable-offline',action='store_true',default=False)
    parser.add_argument('--max-seconds',type=float,default=120)
    parser.add_argument('--max-cycles',type=int,default=2)
    parser.add_argument('--scenario',choices=sorted(SCENARIOS),default='normal')
    parser.add_argument('--request-name')
    parser.add_argument('--deadline',type=float)
    args=parser.parse_args();root=args.directory.resolve()
    if args.command=='run':
        result=ShortRunner(root,OfflinePort).run(enabled=args.enable_offline,max_seconds=args.max_seconds,max_cycles=args.max_cycles)
    elif not args.enable_offline:
        result=dict(status='DISABLED',synthetic_only=True,accounting_writes=0,authority_calls=0)
    elif args.command=='provision':
        provision(root,scenario=args.scenario);result=dict(status='PROVISIONED',synthetic_only=True)
    elif args.command=='stop':
        Control(root).read();atomic(root/'STOP',b'stop',create=True);result=dict(status='STOP_REQUESTED',synthetic_only=True)
    elif args.command=='worker':
        import re
        OfflinePort(root)  # Reject every real/broker config before touching it.
        if not re.fullmatch(r'request-[a-f0-9]{32}\.json',args.request_name or ''):raise ValueError('request path')
        data=(root/args.request_name).read_bytes();request=json.loads(data)
        if request['op'] not in {'poll','fault_poll','ready','inspect'}:raise ValueError('runner operation')
        write_json(root/(args.request_name+'.pid'),dict(pid=os.getpid(),request_sha256=fingerprint(data)),create=True)
        if args.deadline is None or time.monotonic()>=args.deadline:raise TimeoutError('worker startup deadline')
        if OfflinePort(root).config['scenario']=='worker_timeout':time.sleep(60)
        from live_google_sheets_probe import run
        with exclusive(root/'probe.lock'):outcome=run(root,request)
        write_json(root/(args.request_name+'.response'),dict(request_sha256=fingerprint(data),result=outcome),create=True)
        result=dict(status='WORKER_COMPLETE',synthetic_only=True)
    else:
        state,_=Control(root).read();observed=OfflinePort(root).inspect_readonly()
        result=dict(status=state['status'],phase=state['phase'],draft_writes=observed['draft_writes'],
                    registrations=[dict(case_id=r['case_id'],status=r['status']) for r in observed['registrations']['requests'].values()],
                    outbox_states=[r['state'] for r in observed['outbox'].values()],financial_writes=0,authority_calls=0,synthetic_only=True)
    print(json.dumps(result,ensure_ascii=True))
    if result.get('status') in {'UNKNOWN','REVOKED','CAPACITY'}:raise SystemExit(3)


if __name__=='__main__':
    try:main()
    except Exception:
        print(json.dumps(dict(status='STOPPED_ERROR',synthetic_only=True,accounting_writes=0,authority_calls=0)))
        raise SystemExit(2)
