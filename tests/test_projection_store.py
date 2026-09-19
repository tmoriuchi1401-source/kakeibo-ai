import json
from types import SimpleNamespace

import pytest

from app.monthly_projection import ProjectionError
from app.projection_store import DriveProjectionStore, store_from_environment


class Drive:
    def __init__(self):
        self.rows={}
        self.payloads={}
        self.owner={"id":"owner","type":"user","role":"owner"}
        self.folder_grants=[self.owner]
        self.file_grants=[self.owner]
        self.create_count=0
        self.fail_after_create=False

    def files(self):
        return self

    def req(self, value):
        def execute(**kwargs):
            assert kwargs=={"num_retries":0}
            return value
        return SimpleNamespace(execute=execute)

    def list(self, **kwargs):
        name=kwargs["q"].split("name='")[1].split("'")[0]
        return self.req({"files":[r for r in self.rows.values() if r["name"]==name]})

    def get(self, fileId, **kwargs):
        if fileId=="source":
            value={"permissions":[self.owner]}
        elif fileId=="folder":
            value={"mimeType":"application/vnd.google-apps.folder","permissions":self.folder_grants,
                   "capabilities":{"canAddChildren":True}}
        else:
            value={"permissions":self.file_grants}
        return self.req(value)

    def get_media(self, fileId, **kwargs):
        return self.req(self.payloads[fileId])

    def create(self, body, media_body, **kwargs):
        def execute(**kwargs):
            assert kwargs=={"num_retries":0}
            self.create_count+=1
            key=str(self.create_count)
            self.rows[key]={**body,"id":key}
            self.payloads[key]=media_body.getbytes(0,media_body.size())
            if self.fail_after_create:
                self.fail_after_create=False
                raise RuntimeError("private API response")
            return {"id":key}
        return SimpleNamespace(execute=execute)

    def update(self,fileId,media_body,**kwargs):
        self.payloads[fileId]=media_body.getbytes(0,media_body.size())
        return self.req({"id":fileId})


def test_drive_roundtrip_and_unknown_create_response_are_reconciled_without_duplicate():
    drive=Drive()
    store=DriveProjectionStore(drive,"folder","source")
    drive.fail_after_create=True
    with pytest.raises(ProjectionError,match="^projection_drive_write_unknown$"):
        store.write("journal",{"value":1})
    # Next process discovers the successful write. replace_document can compare
    # it, while direct writes update the existing file rather than creating two.
    assert store.read("journal")=={"value":1}
    store.write("journal",{"value":2})
    assert store.read("journal")=={"value":2}
    assert drive.create_count==1


@pytest.mark.parametrize("grant", [{"id":"anyone","type":"anyone"},
                                   {"id":"new-person","type":"user"},
                                   {"id":"domain","type":"domain"}])
def test_new_projection_cannot_expand_source_sharing(grant):
    drive=Drive()
    drive.folder_grants.append(grant)
    with pytest.raises(ProjectionError,match="folder_sharing_mismatch"):
        DriveProjectionStore(drive,"folder","source").write("journal",{})
    assert drive.create_count==0


def test_existing_file_with_new_share_is_not_updated():
    drive=Drive()
    store=DriveProjectionStore(drive,"folder","source")
    store.write("journal",{"value":1})
    drive.file_grants.append({"id":"new-person","type":"user"})
    with pytest.raises(ProjectionError,match="file_sharing_mismatch"):
        store.write("journal",{"value":2})
    assert store.read("journal")=={"value":1}


def test_wrong_source_payload_or_duplicate_files_are_rejected():
    drive=Drive()
    store=DriveProjectionStore(drive,"folder","source")
    store.write("journal",{})
    value=json.loads(drive.payloads["1"])
    value["binding"]="different"
    drive.payloads["1"]=json.dumps(value).encode()
    with pytest.raises(ProjectionError,match="file_invalid"):
        store.read("journal")
    drive.rows["2"]={**drive.rows["1"],"id":"2"}
    with pytest.raises(ProjectionError,match="duplicate_file"):
        store.read("journal")


def test_environment_without_opt_in_never_constructs_live_clients():
    assert store_from_environment("synthetic",{}) is None
    with pytest.raises(ProjectionError,match="validated_main_required"):
        store_from_environment("synthetic",{"KAKEIBO_PROJECTION_FOLDER_ID":"plaintext"})
