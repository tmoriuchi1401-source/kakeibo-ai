"""Named Firestore database, conditional state and encrypted 10-minute tickets.

The ETag is THIS service's strong validator, not a Drive File ETag. Firestore's
server update-time precondition enforces CAS; conflicts become HTTP_412 with no
retry. There is deliberately no Drive/Sheets credential or production mode.
"""
from dataclasses import asdict
from datetime import datetime, timezone
from hashlib import sha256
import json
import time

from cryptography.fernet import Fernet
from google.api_core.exceptions import FailedPrecondition, NotFound
from google.cloud.firestore_v1 import LastUpdateOption

from app.drive_run_state import StateError
from app.human_general_auth_transport import BrowserSession, TTL, UUID, fingerprint


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def timestamp(value):
    return datetime.fromtimestamp(value, timezone.utc)


class FirestoreConditionalState:
    def __init__(self, document, *, clock=time.time):
        self.document, self.clock = document, clock

    @staticmethod
    def etag(snapshot):
        return '"fs-' + sha256(snapshot.update_time.isoformat().encode()
                              + snapshot.get('payload')).hexdigest() + '"'

    def read_snapshot(self):
        try:
            snapshot = self.document.get(retry=None, timeout=10)
            if not snapshot.exists or snapshot.get('expires_at').timestamp() <= self.clock():
                raise ValueError()
            payload = snapshot.get('payload')
            if type(payload) is not bytes or len(payload) > 750_000:
                raise ValueError()
            return snapshot
        except Exception:
            raise StateError('synthetic_state_unavailable') from None

    def read_versioned(self):
        snapshot = self.read_snapshot()
        return snapshot.get('payload'), self.etag(snapshot)

    def replace_versioned(self, before, tag, after):
        snapshot = self.read_snapshot()
        if (not isinstance(tag, str) or tag == '*' or tag != self.etag(snapshot)
                or before != snapshot.get('payload') or type(after) is not bytes
                or len(after) > 750_000):
            raise StateError('HTTP_412')
        try:
            self.document.update({'payload': after},
                option=LastUpdateOption(snapshot.update_time), retry=None, timeout=10)
        except (FailedPrecondition, NotFound):
            raise StateError('HTTP_412') from None
        except Exception:
            # Ambiguous network failure must never retry or fall back to set().
            raise StateError('synthetic_conditional_save_unknown') from None


class EncryptedTickets:
    def __init__(self, client, key, *, clock=time.time):
        self.collection, self.cipher, self.clock = client.collection('sessions'), Fernet(key), clock

    def save(self, ticket, expires_at):
        if not self.clock() < expires_at <= self.clock() + TTL:
            raise StateError('synthetic_session_expiry_invalid')
        record = {'ticket': asdict(ticket), 'expires_at': expires_at}
        ciphertext = self.cipher.encrypt(canonical(record))
        self.collection.document(fingerprint(ticket.cookie)).create(
            {'ciphertext': ciphertext, 'expires_at': timestamp(expires_at)}, retry=None, timeout=10)

    def load(self, cookie):
        try:
            if not isinstance(cookie, str) or not 32 <= len(cookie) <= 100:
                raise ValueError()
            snapshot = self.collection.document(fingerprint(cookie)).get(retry=None, timeout=10)
            if not snapshot.exists or snapshot.get('expires_at').timestamp() <= self.clock():
                raise ValueError()
            record = json.loads(self.cipher.decrypt(snapshot.get('ciphertext'), ttl=TTL))
            if record['expires_at'] <= self.clock():
                raise ValueError()
            ticket = BrowserSession(**record['ticket'])
            if ticket.cookie != cookie or not UUID.fullmatch(ticket.request_id):
                raise ValueError()
            return ticket
        except Exception:
            raise StateError('human_general_auth_request_expired') from None
