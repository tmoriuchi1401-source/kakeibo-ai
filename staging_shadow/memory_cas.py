"""Pure memory CAS port; no Google SDK, filesystem or credentials."""
from copy import deepcopy
from datetime import datetime, timezone, timedelta
from threading import RLock
from types import SimpleNamespace


class FailedPrecondition(Exception): pass
class NotFound(Exception): pass


class LastUpdateOption:
    def __init__(self, last_update_time): self._last_update_time = last_update_time


class Snapshot:
    def __init__(self, data, version):
        self.exists = data is not None
        self.data = deepcopy(data)
        self.update_time = datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(microseconds=version)
    def get(self, key): return deepcopy(self.data[key])


class Document:
    def __init__(self, db, path): self.db, self.path = db, path
    def get(self, *, retry, timeout):
        assert retry is None and timeout == 10
        with self.db.lock: return Snapshot(self.db.data.get(self.path), self.db.versions.get(self.path, 0))
    def create(self, data, *, retry, timeout):
        assert retry is None and timeout == 10
        with self.db.lock:
            if self.path in self.db.data: raise FailedPrecondition()
            self.db.data[self.path] = deepcopy(data)
            self.db.versions[self.path] = 1
            self.db.writes += 1
    def update(self, data, *, option, retry, timeout):
        assert retry is None and timeout == 10 and isinstance(option, LastUpdateOption)
        with self.db.lock:
            before = self.get(retry=None, timeout=10)
            if not before.exists: raise NotFound()
            if option._last_update_time != before.update_time: raise FailedPrecondition()
            self.db.data[self.path].update(deepcopy(data))
            self.db.versions[self.path] += 1
            self.db.writes += 1


class Database:
    shadow_memory_only = True
    def __init__(self):
        self.lock = RLock()
        self.data, self.versions, self.writes = {}, {}, 0
    def collection(self, name):
        if name not in {'requests', 'sessions', 'login_sessions', 'pages'}:
            raise ValueError('staging_collection_forbidden')
        return SimpleNamespace(document=lambda rid: Document(self, name + '/' + rid))
