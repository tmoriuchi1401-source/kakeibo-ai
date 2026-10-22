"""Separate-process synthetic server/worker harness; NEVER a live backend.

SQLite only models the server's atomic update_time precondition in tests.
Each worker restores to a different empty temporary directory. Actual CAS
code is the unchanged hash-pinned FirestoreConditionalState implementation.
"""
from copy import deepcopy
import json
import os
from pathlib import Path
import sqlite3
import sys
import time
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from google.api_core.exceptions import FailedPrecondition
from iphone_persistent_fixtures import Snapshot, repository
from offline_registration_runner import OfflinePort
from staging_shadow.persistent_registration import PersistentControl, run_persistent


class Server:
    def __init__(self, path):self.path = Path(path)
    def connect(self):return sqlite3.connect(self.path, timeout=10)
    def get(self, *, retry, timeout):
        assert retry is None and timeout == 10
        with self.connect() as db:
            payload, version, fault = db.execute('select payload,version,fault from document').fetchone()
        if fault == 'read':raise PermissionError('synthetic revoked')
        return Snapshot(payload, version)
    def update(self, values, *, option, retry, timeout):
        assert retry is None and timeout == 10 and set(values) == {'payload'}
        with self.connect() as db:
            db.execute('begin immediate')
            payload, version, fault = db.execute('select payload,version,fault from document').fetchone()
            if option._last_update_time != Snapshot(payload, version).update_time:
                raise FailedPrecondition('synthetic update_time conflict')
            if fault == 'before':raise TimeoutError('synthetic response lost before commit')
            db.execute('update document set payload=?,version=version+1,writes=writes+1', (values['payload'],))
        if fault == 'after':raise TimeoutError('synthetic response lost after commit')
    def set_fault(self, fault):
        with self.connect() as db:db.execute('update document set fault=?', (fault,))


class Slots:
    def __init__(self, directory):self.root = Path(directory)
    def read(self, generation):
        if (self.root/'revoked').exists():raise PermissionError('synthetic revoked')
        return (self.root/f'{generation}.enc').read_bytes()
    def write(self, generation, raw):
        if (self.root/'revoked').exists():raise PermissionError('synthetic revoked')
        if (self.root/'full').exists():raise OSError('synthetic capacity')
        if (self.root/'before').exists():raise TimeoutError('synthetic pre-save loss')
        (self.root/f'{generation}.enc').write_bytes(raw)
        if (self.root/'corrupt').exists():(self.root/f'{generation}.enc').write_bytes(raw[:-5])
        if (self.root/'after').exists():raise TimeoutError('synthetic post-save loss')
        if (self.root/'crash').exists():os._exit(73)


def control(shared, root):
    shared = Path(shared); config = json.loads((shared/'private.json').read_bytes())
    key = (shared/'key').read_bytes()
    # repository() loads the existing hash-pinned backend, without a client.
    io = repository(Server(shared/'server.sqlite'), key,
        SimpleNamespace(contract_digest='a'*64, snapshot_digest='b'*64)).transport.io
    return PersistentControl(io, Slots(shared/'slots'), key, config, root)


class Port(OfflinePort):
    def __init__(self, directory, remote):
        super().__init__(directory); self.remote = OfflinePort(remote)
    def authorized(self):return self.remote.authorized()
    def capture(self):return self.remote.capture()
    def publish(self, requests, capture, budget):return self.remote.publish(requests, capture, budget)


def main():
    op, shared, root = sys.argv[1:4]; shared = Path(shared); root = Path(root)
    c = control(shared, root)
    if op == 'run':
        result = run_persistent(c, lambda p:Port(p, shared/'remote'), enabled=True, max_seconds=120)
    elif op == 'inspect':
        state, _ = c.restore()
        inspected = Port(root, shared/'remote').inspect_readonly()
        result = dict(status=state['status'], draft_writes=inspected['draft_writes'],
            financial_writes=inspected['financial_writes'], authority_calls=inspected['authority_calls'])
    elif op == 'claim':
        state, tag = c.restore()
        (root/'barrier').write_text('ready')
        until = time.monotonic()+10
        while not (shared/'go').exists():
            if time.monotonic() > until:raise TimeoutError()
            time.sleep(.02)
        c.replace(tag, status='RUNNING', run_id=root.name)
        result = dict(status='CLAIMED')
    elif op == 'change':
        c.restore(); state, tag = c.read()
        _, tag = c.replace(tag, status='RUNNING', run_id='worker')
        c.replace(tag, claim='worker', status='PENDING', pending=dict(phase='poll'))
        value = json.loads((root/'checkpoint.json').read_bytes()); value['test_marker'] = 'changed'
        (root/'checkpoint.json').write_bytes(json.dumps(value).encode())
        h = c.save_checkpoint(30)
        if (shared/'cas-before').exists():Server(shared/'server.sqlite').set_fault('before')
        if (shared/'cas-after').exists():Server(shared/'server.sqlite').set_fault('after')
        c.replace(c.read()[1], claim='worker', status='RUNNING', checkpoint=h, pending=None)
        result = dict(status='SAVED')
    else:raise ValueError('synthetic operation')
    print(json.dumps(result))


if __name__ == '__main__':
    try:main()
    except Exception:
        # Never print private paths, SDK errors, ciphertext or payloads.
        print(json.dumps(dict(status='STOPPED_ERROR')))
        raise SystemExit(2)
