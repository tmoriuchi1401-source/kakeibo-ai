"""Validation-only registration adapter for the EXISTING OIDC/session core.

Offline integration candidate; excluded from compose.py's deployable whitelist.
No HTTP routes, deployment, credentials, authority factory, writer or dispatch.
The host supplies a protected request store and independent source/Sheet reader.
Editors are untrusted; only the authenticated registrant makes the explicit POST.
"""
from copy import deepcopy
from hashlib import sha256
from uuid import uuid4

from app.drive_run_state import StateError
from app.human_general_auth_transport import (
    AuthenticatedGeneralConfirmation, TTL, UUID, validate_auth_state, same, fingerprint,
)
from app.page_receipt_model import digest

ACTION = 'register_receipt_item_correction_validation_v1'
SCOPE = 'receipt-correction-validation-only-v1'


def receipt_digest(record, validation):
    return digest([ACTION, record['digest'], record['actor']['actor_id'], validation])


def validate_registration_state(value, scope):
    """Reuse existing actor/session checks without creating authority evidence.

    The core validator calls its completion checksum authority_digest. Project
    ONLY into a temporary validation copy; never persist that field or make a
    registration receipt acceptable as an existing posting grant.
    """
    if scope != SCOPE:
        raise StateError('receipt_registration_scope_invalid')
    projected = deepcopy(value)
    try:
        for record in projected['requests'].values():
            b = record['binding']
            if (set(b) != {'requested_action', 'request_id', 'case_id', 'source',
                           'snapshot', 'created_at', 'expires_at'}
                    or b['requested_action'] != ACTION
                    or b['snapshot']['case_id'] != b['case_id']
                    or b['snapshot']['source'] != b['source']
                    or b['created_at'] != record['created_at']
                    or b['expires_at'] != record['expires_at']):
                raise ValueError()
            if record['status'] == 'complete':
                validation = record.pop('validation')
                checksum = record.pop('registration_digest')
                if (validation['posting_authority'] is not False
                        or validation['financial_write_count'] != 0
                        or validation['state'] not in {'修正が必要', '検証済み・未記帳'}
                        or checksum != receipt_digest(record, validation)):
                    raise ValueError()
                record['authority_digest'] = checksum
        validate_auth_state(projected, scope, allowed_actions={ACTION})
    except Exception:
        raise StateError('receipt_registration_state_invalid') from None
    return value


class AuthenticatedCorrectionRegistration(AuthenticatedGeneralConfirmation):
    """Reuse begin/callback/resume_login/actor/CSRF/CAS from the pinned core.

    read_case(case_id) must independently read original metadata+bytes, saved
    candidate and current Sheet draft, plus trusted categories/rules/prior total
    confirmation. Never supply these from browser form fields or a hidden queue.
    This adapter owns no source-edit, journal-creation or accounting capability.
    """
    def __init__(self, store, identity, *, read_case, build_review, validate_snapshot, **kwargs):
        if store.binding != SCOPE or store.validate is not validate_registration_state:
            raise StateError('receipt_registration_dedicated_store_required')
        super().__init__(store, identity, lambda *_: None, lambda *_: None, **kwargs)
        self.requested_action, self.read_case = ACTION, read_case
        self.build_review, self.validate_snapshot = build_review, validate_snapshot

    def current(self, case_id):
        try:
            packet = self.read_case(case_id)
            if (packet['tx'].import_id != case_id or type(packet['source_bytes']) is not bytes
                    or sha256(packet['source_bytes']).hexdigest() != packet['source']['sha256']):
                raise ValueError()
            return self.build_review(packet['tx'], packet['indexed_rows'], packet['source'],
                          packet['operations'], packet['categories'],
                          total_confirmation=packet['total_confirmation'],
                          approved_product_rules=packet['approved_product_rules'])
        except Exception:
            raise StateError('receipt_registration_independent_read_failed') from None

    def fresh(self, record):
        value = self.current(record['binding']['case_id'])
        if value['snapshot'] != record['binding']['snapshot']:
            raise StateError('receipt_registration_snapshot_stale')
        return value

    def prepare(self, case_id, *, request_id=None):
        # Trusted host reads and freezes; no browser-supplied snapshot or actor.
        rid, now = request_id or str(uuid4()), int(self.clock())
        if not isinstance(rid, str) or not UUID.fullmatch(rid):
            raise StateError('receipt_registration_request_invalid')
        value = self.current(case_id)
        bound = dict(requested_action=ACTION, request_id=rid, case_id=case_id,
                     source=deepcopy(value['snapshot']['source']), snapshot=value['snapshot'],
                     created_at=now, expires_at=now + TTL)
        record = dict(request_id=rid, binding=bound, digest=digest(bound),
                      created_at=now, expires_at=now + TTL, status='prepared')
        state = self.store.load()
        if rid in state['requests']:
            raise StateError('receipt_registration_request_reuse')
        state['requests'][rid] = record
        self.fresh(record)
        self.store.save(state)
        return rid

    def details(self, ticket, *, cookie):
        # Read-only preview of exactly what will be submitted, not just a hash.
        _, record = self.record(ticket.request_id, 'authenticated')
        self.browser(record, ticket, cookie)
        return deepcopy(self.fresh(record))

    def confirm(self, ticket, *, cookie, csrf, origin, method):
        state, record = self.record(ticket.request_id, 'authenticated')
        self.browser(record, ticket, cookie)
        if (method != 'POST' or not same(origin, self.origin) or not same(csrf, ticket.csrf)
                or not same(fingerprint(csrf), record['session']['csrf'])):
            raise StateError('human_general_auth_csrf_invalid')
        self.fresh(record)
        record['status'] = 'claimed'
        self.store.save(state)  # Existing versioned CAS; no retries/fallback.
        actor = self.verified_actor(ticket.request_id)
        validation = self.validate_snapshot(record['binding']['snapshot'], self.fresh(record))
        state, record = self.record(ticket.request_id, 'claimed')
        # Check login revocation, source and draft again before the final CAS.
        self.verified_actor(ticket.request_id)
        checksum = receipt_digest(record, validation)
        record.update(status='complete', validation=validation, registration_digest=checksum)
        self.store.save(state)
        return dict(request_id=ticket.request_id, actor_id=actor.actor_id,
                    case_id=record['binding']['case_id'], source=deepcopy(record['binding']['source']),
                    snapshot_digest=record['binding']['snapshot']['digest'], expires_at=record['expires_at'],
                    registration_digest=checksum, validation=deepcopy(validation),
                    posting_authority=False, financial_write_count=0)

    def result(self, ticket, *, cookie):
        # Reconcile/read only. Completed requests never execute validation again.
        _, record = self.record(ticket.request_id, 'complete')
        self.browser(record, ticket, cookie)
        return dict(request_id=ticket.request_id, actor_id=record['actor']['actor_id'],
                    case_id=record['binding']['case_id'], source=deepcopy(record['binding']['source']),
                    snapshot_digest=record['binding']['snapshot']['digest'], expires_at=record['expires_at'],
                    registration_digest=record['registration_digest'],
                    validation=deepcopy(record['validation']),
                    posting_authority=False, financial_write_count=0)
