"""Contract against actual DriveStateTransport; no Google client or socket."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from app.drive_run_state import StateError
from staging_shadow.persistent_registration import DriveCheckpointSlots, Slot


class Request:
    def __init__(self, operation):self.operation = operation
    def execute(self, *, num_retries):
        assert num_retries == 0
        return self.operation()


class Service:
    def __init__(self):
        self.data = {'one':b'', 'two':b''}; self.calls=[]; self.fault=None
        self.acl=[dict(id='owner', type='user', role='owner', deleted=False)]
    def files(self):return self
    def permissions(self):return self
    def get(self, *, fileId, fields, supportsAllDrives):
        assert supportsAllDrives is True
        def read():
            self.calls.append(('metadata',fileId))
            if self.fault == 'revoked':raise PermissionError()
            return dict(id=fileId, trashed=False, shared=False,
                mimeType='application/vnd.google-apps.folder' if fileId=='folder' else 'application/json',
                owners=[dict(permissionId='owner')], parents=['other' if self.fault=='moved' else 'folder'],
                size=str(6_000_000 if self.fault=='oversized' else len(self.data.get(fileId,b''))),
                capabilities=dict(canEdit=True))
        return Request(read)
    def list(self, *, fileId, fields, supportsAllDrives):
        return Request(lambda:dict(permissions=deepcopy(self.acl)))
    def get_media(self, *, fileId, supportsAllDrives):
        def read():self.calls.append(('read',fileId));return self.data[fileId]
        return Request(read)
    def update(self, *, fileId, supportsAllDrives, media_body, fields):
        def write():
            self.calls.append(('write',fileId))
            self.data[fileId] = media_body.getbytes(0,media_body.size())
            if self.fault=='lost':raise TimeoutError()
            return dict(id=fileId)
        return Request(write)


def slots(service):
    return DriveCheckpointSlots(service, [Slot('one','folder'),Slot('two','folder')], owner_permission_id='owner')


def test_existing_transport_upload_independent_download_and_no_retry():
    s=Service(); store=slots(s)
    assert store.read(0)==b''
    store.write(0,b'encrypted fixture')
    assert store.read(0)==b'encrypted fixture'
    assert s.calls.count(('write','one'))==1 and s.calls.count(('read','one'))==2
    s.fault='lost'
    with pytest.raises(StateError,match='write_unknown'):store.write(1,b'unknown outcome')
    assert s.calls.count(('write','two'))==1 and s.data['two']==b'unknown outcome'


@pytest.mark.parametrize('fault',['shared','revoked','moved','missing','wrong_owner','oversized'])
def test_private_owner_and_fixed_target_fail_before_write(fault):
    s=Service();store=slots(s)
    if fault=='shared':s.acl.append(dict(id='viewer',type='anyone',role='reader'))
    elif fault=='wrong_owner':s.acl[0]['id']='stranger'
    elif fault=='missing':del s.data['one']
    else:s.fault=fault
    with pytest.raises(StateError):store.read(0)
    assert not any(c[0]=='write' for c in s.calls)


def test_allowlist_never_discovers_or_allocates_a_replacement():
    s=Service()
    with pytest.raises(StateError):DriveCheckpointSlots(s,[Slot('one','folder')]*2,owner_permission_id='owner')
    with pytest.raises(StateError):slots(s).read(2)
    assert s.calls==[]
