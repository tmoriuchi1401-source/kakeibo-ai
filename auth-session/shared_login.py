"""Encrypted, revocable identity sessions; never operation/posting authority.

Only verified Google claims may create a login. Each use creates a NEW actor
bound to the protected operation request. Firestore TTL is housekeeping only:
absolute/idle/policy/revocation checks are enforced synchronously on every use.
"""
from dataclasses import dataclass
import base64
from hashlib import sha256
import hmac
import json
import re
import secrets
import time

from cryptography.fernet import Fernet
from google.api_core.exceptions import FailedPrecondition, NotFound
from google.cloud.firestore_v1 import LastUpdateOption

from app.drive_run_state import StateError
from app.human_general_auth_transport import ISSUER, VerifiedActor, fingerprint, same
from app.page_receipt_model import digest
from .backend import canonical, timestamp

METHOD = 'google_oidc_shared_session_v2'
SCHEMA = 'google-identity-session-v1'


@dataclass(frozen=True)
class Lifetime:
    absolute: int = 8 * 3600
    idle: int = 60 * 60

    def __post_init__(self):
        if (type(self.absolute) is not int or type(self.idle) is not int
                or not 60 <= self.idle <= self.absolute <= 24 * 3600):
            raise StateError('login_session_policy_invalid')


class SharedLogins:
    def __init__(self, client, key, identity, *, lifetime=None, clock=time.time):
        self.collection = client.collection('login_sessions')
        # Separate cryptographic domain from the old operation-ticket store.
        raw=base64.urlsafe_b64decode(key)
        derived=base64.urlsafe_b64encode(hmac.new(raw,b'kakeibo/google-identity-session/v1',sha256).digest())
        self.cipher, self.identity, self.clock = Fernet(derived), identity, clock
        self.lifetime = lifetime or Lifetime()

    @property
    def policy(self):
        # Invalidate all existing logins on allowlist/audience/revision/TTL change.
        return digest([SCHEMA, ISSUER, self.identity.owner_subject,
                       self.identity.client_id, self.identity.policy_revision,
                       self.lifetime.absolute, self.lifetime.idle])

    @staticmethod
    def handle(cookie):
        if not isinstance(cookie, str) or not re.fullmatch(r'[A-Za-z0-9_-]{43}', cookie):
            raise StateError('login_session_invalid')
        return fingerprint(cookie)

    def _read(self, cookie, *, allow_revoked=False):
        handle = self.handle(cookie)
        document = self.collection.document(handle)
        try:
            snapshot = document.get(retry=None, timeout=10)
            if not snapshot.exists:
                raise StateError('login_session_invalid')
            value = json.loads(self.cipher.decrypt(snapshot.get('ciphertext')))
            fields = {'schema', 'handle', 'issuer', 'subject', 'email', 'policy',
                      'authenticated_at', 'last_seen', 'absolute_expires', 'expires',
                      'revoked', 'revision'}
            now = int(self.clock())
            if (set(value) != fields or value['schema'] != SCHEMA
                    or not same(value['handle'], handle) or value['issuer'] != ISSUER
                    or not re.fullmatch(r'[0-9]{1,255}', value['subject'])
                    or not re.fullmatch(r'[^\s@]{1,100}@[^\s@]{1,150}', value['email'])
                    or any(type(value[k]) is not int for k in
                           ('authenticated_at', 'last_seen', 'absolute_expires', 'expires', 'revision'))
                    or value['revision'] < 1 or type(value['revoked']) is not bool
                    or not value['authenticated_at'] <= value['last_seen'] <= now
                    or value['absolute_expires'] != value['authenticated_at'] + self.lifetime.absolute
                    or value['expires'] != min(value['absolute_expires'], value['last_seen'] + self.lifetime.idle)
                    or snapshot.get('expires_at') != timestamp(value['expires'])):
                raise StateError('login_session_invalid')
            if value['policy'] != self.policy or value['subject'] != self.identity.owner_subject:
                raise StateError('login_session_policy_changed')
            if value['revoked'] and not allow_revoked:
                raise StateError('login_session_revoked')
            if now >= value['expires']:
                raise StateError('login_session_expired')
            return document, snapshot, value
        except StateError:
            raise
        except Exception:
            # No raw exception, cookie, claims or token in error messages/logs.
            raise StateError('login_session_unavailable') from None

    def current(self, cookie):
        """Read only. Invalid/expired cookies require login; outages fail closed."""
        if not cookie:
            return None
        try:
            return self._read(cookie)[2]
        except StateError as error:
            if str(error) in {'login_session_invalid', 'login_session_expired',
                              'login_session_revoked', 'login_session_policy_changed'}:
                return None
            raise

    def _save(self, document, before, value):
        ciphertext = self.cipher.encrypt(canonical(value))
        data = {'ciphertext': ciphertext, 'expires_at': timestamp(value['expires'])}
        try:
            document.update(data, option=LastUpdateOption(before.update_time), retry=None, timeout=10)
        except (FailedPrecondition, NotFound):
            raise StateError('HTTP_412') from None
        except Exception:
            raise StateError('login_session_save_unknown') from None
        # Do not auto-retry a race or an unknown outcome.
        after = document.get(retry=None, timeout=10)
        if (not after.exists or after.get('ciphertext') != ciphertext
                or after.get('expires_at') != data['expires_at']):
            raise StateError('login_session_readback_required')

    def create(self, actor):
        now = int(self.clock())
        if (not isinstance(actor, VerifiedActor) or actor.method != 'google_oidc_code_pkce_v1'
                or actor.verification_revision != 1 or actor.issuer != ISSUER
                or actor.subject != self.identity.owner_subject
                or actor.policy_revision != self.identity.policy_revision
                or not now - 600 <= actor.verified_at <= now):
            raise StateError('login_session_identity_rejected')
        cookie = secrets.token_urlsafe(32)  # Never accept/upgrade a supplied ID.
        value = dict(schema=SCHEMA, handle=self.handle(cookie), issuer=actor.issuer,
                     subject=actor.subject, email=actor.email, policy=self.policy,
                     authenticated_at=now, last_seen=now,
                     absolute_expires=now+self.lifetime.absolute,
                     expires=now+self.lifetime.idle, revoked=False, revision=1)
        document = self.collection.document(value['handle'])
        ciphertext = self.cipher.encrypt(canonical(value))
        try:
            document.create({'ciphertext': ciphertext, 'expires_at': timestamp(value['expires'])},
                            retry=None, timeout=10)
        except Exception:
            raise StateError('login_session_create_unknown') from None
        _, snapshot, saved = self._read(cookie)
        if saved != value or snapshot.get('ciphertext') != ciphertext:
            raise StateError('login_session_readback_required')
        return cookie

    def touch(self, cookie):
        document, snapshot, value = self._read(cookie)
        value.update(last_seen=int(self.clock()), revision=value['revision']+1)
        value['expires'] = min(value['absolute_expires'], value['last_seen']+self.lifetime.idle)
        self._save(document, snapshot, value)
        return value

    def revoke(self, cookie):
        document, snapshot, value = self._read(cookie, allow_revoked=True)
        if not value['revoked']:
            value.update(revoked=True, revision=value['revision']+1)
            self._save(document, snapshot, value)

    def evidence(self, cookie):
        value = self._read(cookie)[2]
        return {'handle': value['handle'], 'policy': value['policy']}

    def actor(self, cookie, record):
        value = self._read(cookie)[2]
        now = int(self.clock())
        if not record['created_at'] <= now < record['expires_at']:
            raise StateError('human_general_auth_request_expired')
        return VerifiedActor(ISSUER, value['subject'], value['email'], METHOD, now,
                             record['request_id'], record['digest'],
                             self.identity.policy_revision, verification_revision=2)

    def verify_bound(self, record, cookie):
        value = self._read(cookie)[2]
        if (record.get('login_evidence') != {'handle': value['handle'], 'policy': value['policy']}
                or record['actor']['issuer'] != value['issuer']
                or record['actor']['subject'] != value['subject']
                or record['actor']['email'] != value['email']):
            raise StateError('login_session_request_binding_invalid')
