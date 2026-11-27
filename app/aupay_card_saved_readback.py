"""At most three GET observations of one expected save, never a write retry.

The existing hosted production lock and deadline are mandatory. Ordinary
ReadOnlyState.observe stays single-shot. Pins move only after an exact verified
own save; a retry never changes its expected bytes, hash or revision baseline.
"""
from copy import deepcopy
from datetime import datetime
import hashlib
import time

from .aupay_card_window_preview import Stop, digest

METADATA_FIELDS = frozenset({"id", "parents", "trashed", "mimeType", "version", "modifiedTime", "size", "md5Checksum", "headRevisionId"})
FIELDS = "id,parents,trashed,mimeType,version,modifiedTime,size,md5Checksum,headRevisionId"


def failure(code, fields=()):
    error = Stop(code)
    error.metadata_fields = tuple(sorted(set(fields) & METADATA_FIELDS))
    raise error


def standard(meta):
    return {k: v for k, v in meta.items() if k != "headRevisionId"}


class SavedReadback:
    def __init__(self, reader, initial, *, guard, deadline, diagnostics=None,
                 clock=time.monotonic, sleeper=time.sleep):
        self.reader, self.binding = reader, reader.binding
        self.pin = dict(initial)
        self.guard, self.deadline = guard, deadline
        self.diagnostics, self.clock, self.sleeper = diagnostics, clock, sleeper

    def check(self, *, guard=True):
        if self.clock() >= self.deadline: failure("repair_timeout")
        if guard:self.guard()
        if self.clock() >= self.deadline: failure("repair_timeout")

    def get(self, request):
        self.check(guard=False)
        if request.method != "GET": failure("saved_readback_get_only")
        value = request.execute(num_retries=0)
        self.check(guard=False)
        return value

    @property
    def api(self):
        # The existing GetOnly wrapper exposes the same readonly API. The
        # additional revision calls are locally asserted GET as well.
        return getattr(self.reader.drive, "api", self.reader.drive)

    def metadata(self):
        value = self.get(self.api.files().get(fileId=self.binding.file_id, supportsAllDrives=True, fields=FIELDS))
        bad = []
        for key, wanted in (("id", self.binding.file_id), ("parents", [self.binding.folder_id]),
                            ("mimeType", "application/json"), ("trashed", False)):
            if value.get(key) != wanted: bad.append(key)
        bad += [k for k in METADATA_FIELDS if k not in value]
        if bad: failure("saved_readback_target_changed", bad)
        if set(value) != METADATA_FIELDS: failure("saved_readback_target_changed")
        return value

    def observe(self):
        self.check()
        before = self.metadata()
        raw = self.get(self.api.files().get_media(fileId=self.binding.file_id, supportsAllDrives=True))
        after = self.metadata()
        return before, raw, after

    def read(self):
        # Existing guard strength plus the last acknowledged metadata pin.
        self.check(guard=False)
        raw, proof = self.reader.observe()
        if proof != self.pin: failure("saved_readback_pin_changed")
        self.check(guard=False)
        return raw

    def begin(self, expected_before):
        before, raw, after = self.observe()
        fields = {k for k in METADATA_FIELDS if before.get(k) != after.get(k)}
        if fields: failure("state_changed_during_read", fields)
        if (raw != expected_before or hashlib.sha256(raw).hexdigest() != self.pin["sha256"]
                or digest(standard(after)) != self.pin["metadata_digest"]):
            failure("saved_readback_pin_changed")
        if int(after["size"]) != len(raw) or after["md5Checksum"] != hashlib.md5(raw).hexdigest():
            failure("saved_readback_checksum_changed", ("size", "md5Checksum"))
        return SaveTicket(self, bytes(raw), deepcopy(after))


class SaveTicket:
    def __init__(self, owner, before, baseline):
        self.owner, self.before, self.baseline = owner, before, baseline
        self.used = False

    def revisions(self):
        owner = self.owner
        result, token = [], None
        for _ in range(4):
            page = owner.get(owner.api.revisions().list(fileId=owner.binding.file_id, pageSize=1000,
                pageToken=token, fields="nextPageToken,revisions(id,modifiedTime,size,md5Checksum)"))
            result.extend(page.get("revisions", [])); token = page.get("nextPageToken")
            if not token: break
        if token or len({v["id"] for v in result}) != len(result): failure("saved_readback_revision_changed")
        index = next((i for i, v in enumerate(result) if v["id"] == self.baseline["headRevisionId"]), -1)
        if index < 0: failure("saved_readback_revision_changed", ("headRevisionId",))
        suffix = result[index:]
        if not 1 <= len(suffix) <= 2: failure("saved_readback_revision_changed", ("headRevisionId", "version"))
        return suffix

    def verify(self, expected):
        if self.used or type(expected) is not bytes or expected == self.before:
            failure("saved_readback_ticket_invalid")
        self.used = True
        expected = bytes(expected)  # Frozen once, before the first retry.
        wanted_sha, wanted_md5 = hashlib.sha256(expected).hexdigest(), hashlib.md5(expected).hexdigest()
        wanted_size, base_version = len(expected), int(self.baseline["version"])
        last_version, new_revision, changed = base_version, None, set()
        for attempt in range(1, 4):
            before, raw, after = self.owner.observe()
            if raw != expected or hashlib.sha256(raw).hexdigest() != wanted_sha:
                failure("saved_readback_content_changed")
            suffix = self.revisions()
            revision = suffix[-1] if len(suffix) == 2 else None
            if revision:
                if new_revision is not None and new_revision != revision["id"]:
                    failure("saved_readback_revision_changed", ("headRevisionId",))
                new_revision = revision["id"]
                if int(revision.get("size", -1)) != wanted_size or revision.get("md5Checksum") != wanted_md5:
                    failure("saved_readback_checksum_changed", ("size", "md5Checksum"))
            for meta in (before, after):
                version = int(meta["version"])
                if not last_version <= version <= base_version + 2:
                    failure("saved_readback_revision_changed", ("version",))
                last_version = version
                if meta["headRevisionId"] not in {self.baseline["headRevisionId"], new_revision}:
                    failure("saved_readback_revision_changed", ("headRevisionId",))
                # Only the old value or this exact own-save value may be stale.
                if int(meta["size"]) not in {int(self.baseline["size"]), wanted_size}:
                    failure("saved_readback_checksum_changed", ("size",))
                if meta["md5Checksum"] not in {self.baseline["md5Checksum"], wanted_md5}:
                    failure("saved_readback_checksum_changed", ("md5Checksum",))
                allowed_times = {self.baseline["modifiedTime"]} | ({revision["modifiedTime"]} if revision else set())
                if meta["modifiedTime"] not in allowed_times:
                    failure("saved_readback_revision_changed", ("modifiedTime",))
            fields = {k for k in METADATA_FIELDS if before[k] != after[k]}
            wanted = dict(size=str(wanted_size), md5Checksum=wanted_md5,
                          version=str(base_version + 2), headRevisionId=new_revision,
                          modifiedTime=revision["modifiedTime"] if revision else None)
            fields.update(k for k, v in wanted.items() if after.get(k) != v)
            changed.update(fields)
            if self.owner.diagnostics:
                self.owner.diagnostics.readback_attempt(attempt, fields)
            if not fields and revision is not None:
                if datetime.fromisoformat(revision["modifiedTime"].replace("Z", "+00:00")) < datetime.fromisoformat(self.baseline["modifiedTime"].replace("Z", "+00:00")):
                    failure("saved_readback_revision_changed", ("modifiedTime",))
                self.owner.check()
                self.owner.pin = dict(sha256=wanted_sha, metadata_digest=digest(standard(after)))
                return expected
            if attempt == 3: failure("saved_readback_unstable", changed)
            self.owner.check()
            self.owner.sleeper(.25 * attempt)


class RepairReadbacks:
    def __init__(self, native, parent, record, **kwargs):
        self.sessions = {scope: SavedReadback(reader,
            dict(sha256=record[prefix + "_before_sha256"], metadata_digest=record[prefix + "_before_metadata_digest"]), **kwargs)
            for scope, reader, prefix in (("native", native, "native"), ("parent", parent, "parent"))}

    def begin(self, scope, before):
        return self.sessions[scope].begin(before)
