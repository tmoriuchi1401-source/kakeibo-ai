"""Source/page capabilities, independent of OAuth sessions and canary IDs.

Cloud Run seals decisions after its existing verified-actor/explicit-POST gate.
Actions receives only private durable metadata, never cookies or OIDC tokens.
The verifier is an injected trusted backend; a Sheet value or self-declared
actor is not a verifier. Existing normal intake does not acquire human grants.
"""
from copy import deepcopy
from hashlib import sha256
import hmac
import json
import re
from uuid import UUID

from .drive_run_state import StateError

SCHEMA = 'pdf-intake-page-capability-v1'
MAX_BYTES = 4 * 1024 * 1024
MAX_ACTIVE_PAGES = 500
OPERATIONS = frozenset({'single_page_ai', 'receipt_posting'})
PAGE_FIELDS = frozenset({
    'source', 'page_number', 'stable_page_identity', 'automatic_classification',
    'automatic_reason', 'observation_complete', 'extraction_status',
    'observation_render_hash', 'human_page_kind', 'review_identity',
    'authority_revision', 'clearly_sensitive', 'processing_status',
})
UNKNOWN_REASONS = frozenset({
    'insufficient_evidence', 'sensitive_signal_insufficient', 'privacy_unresolved',
})


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True,
                      separators=(',', ':'), allow_nan=False).encode()


def digest(value):
    return sha256(canonical(value)).hexdigest()


def _hash(value):
    return isinstance(value, str) and re.fullmatch('[0-9a-f]{64}', value) is not None


def _id(value):
    return isinstance(value, str) and re.fullmatch('[A-Za-z0-9_-]{1,150}', value) is not None


def request_id(value):
    try:
        return str(UUID(value)) == value
    except (ValueError, TypeError, AttributeError):
        return False


def review_identity(page):
    return digest(['page-general-ai-review-v1', page['source'], page['page_number'],
                   page['stable_page_identity'], page['automatic_classification'],
                   page['automatic_reason'], page['human_page_kind'],
                   page['authority_revision'], page['observation_complete'],
                   page['extraction_status'], page['clearly_sensitive']])


def validate_page(page):
    try:
        if not isinstance(page, dict) or set(page) != PAGE_FIELDS:
            raise ValueError()
        source = page['source']
        if (set(source) != {'source_file_id', 'source_content_hash', 'page_count', 'source_kind'}
                or not _id(source['source_file_id']) or not _hash(source['source_content_hash'])
                or source['source_kind'] != 'pdf'
                or type(source['page_count']) is not int or not 1 <= source['page_count'] <= 50
                or type(page['page_number']) is not int
                or not 1 <= page['page_number'] <= source['page_count']):
            raise ValueError()
        # Existing immutable pdf-page-v1 identity uses this exact wire order.
        identity = sha256(json.dumps(['pdf-page-v1', source['source_content_hash'],
                                     page['page_number'], source['page_count']],
                                    ensure_ascii=True, separators=(',', ':')).encode()).hexdigest()
        if (page['stable_page_identity'] != identity
                or page['review_identity'] != review_identity(page)
                or not _hash(page['observation_render_hash'])
                or type(page['authority_revision']) is not int or page['authority_revision'] < 1
                or type(page['observation_complete']) is not bool
                or type(page['clearly_sensitive']) is not bool
                or page['automatic_classification'] not in {'normal', 'unknown', 'sensitive_unknown', 'medical', 'payroll'}
                or page['human_page_kind'] not in {None, 'unknown', 'general_receipt', 'medical', 'payroll'}
                or not isinstance(page['automatic_reason'], str) or len(page['automatic_reason']) > 100
                or not isinstance(page['extraction_status'], str) or len(page['extraction_status']) > 50
                or not isinstance(page['processing_status'], str) or len(page['processing_status']) > 80):
            raise ValueError()
    except (ValueError, TypeError, KeyError):
        raise StateError('pdf_intake_page_binding_invalid') from None
    return deepcopy(page)


def page_key(page):
    validate_page(page)
    return digest(['source-page-unit-v1', page['source'], page['page_number'], page['stable_page_identity']])


def binding(page, operation, *, unit=None, snapshot_digest=None, plan_digest=None):
    page = validate_page(page)
    if operation not in OPERATIONS:
        raise StateError('pdf_intake_operation_forbidden')
    result = {'schema': SCHEMA, 'page': page, 'operation': operation}
    if operation == 'receipt_posting':
        if (not isinstance(unit, dict) or set(unit) != {'receipt_unit_id', 'segmentation_digest', 'item_identities'}
                or not isinstance(unit['receipt_unit_id'], str)
                or not unit['receipt_unit_id'].startswith('page-receipt-v1:')
                or not _hash(unit['receipt_unit_id'].split(':')[-1])
                or not _hash(unit['segmentation_digest'])
                or not isinstance(unit['item_identities'], list) or not 1 <= len(unit['item_identities']) <= 300
                or len(set(unit['item_identities'])) != len(unit['item_identities'])
                or any(not _hash(x) for x in unit['item_identities'])
                or not _hash(snapshot_digest) or not _hash(plan_digest)):
            raise StateError('pdf_intake_unit_binding_invalid')
        result.update(unit=deepcopy(unit), snapshot_digest=snapshot_digest, plan_digest=plan_digest)
    elif any(value is not None for value in (unit, snapshot_digest, plan_digest)):
        raise StateError('pdf_intake_ai_scope_expansion_forbidden')
    return result


def eligible_for_hga(page):
    validate_page(page)
    return (page['automatic_classification'] in {'unknown', 'sensitive_unknown'}
            and page['automatic_reason'] in UNKNOWN_REASONS
            and page['human_page_kind'] not in {'medical', 'payroll'}
            and not page['clearly_sensitive'] and page['observation_complete']
            and page['extraction_status'] == 'extracted')


class DecisionSealer:
    """Cloud Run only. Separate-purpose MAC using its existing private key.

    The key and owner identity are never distributed to Actions. This is not
    an identity provider: actor must come from existing verified_actor(rid).
    """
    def __init__(self, key, owner_actor_id):
        if not isinstance(key, bytes) or len(key) < 32 or not _hash(owner_actor_id):
            raise StateError('pdf_intake_sealer_configuration_invalid')
        self.key, self.owner_actor_id = key, owner_actor_id

    def _mac(self, decision):
        return hmac.new(self.key, b'kakeibo-pdf-intake-decision-v1\0' + canonical(decision), sha256).hexdigest()

    def seal(self, expected, actor, rid, confirmed_at):
        if (not request_id(rid) or actor.request_id != rid
                or actor.actor_id != self.owner_actor_id
                or actor.method not in {'google_oidc_code_pkce_v1', 'google_oidc_shared_session_v2'}
                or actor.request_digest != digest({'request_id': rid, **expected})
                or type(confirmed_at) is not int or confirmed_at <= 0):
            raise StateError('pdf_intake_verified_actor_required')
        if expected['operation'] == 'single_page_ai' and not eligible_for_hga(expected['page']):
            raise StateError('pdf_intake_sensitive_hga_forbidden')
        decision = {'binding': deepcopy(expected), 'request_id': rid,
                    'actor_id': actor.actor_id, 'verification_method': actor.method,
                    'verified_at': actor.verified_at, 'confirmed_at': confirmed_at,
                    'revoked': False, 'schema_version': 1, 'retention_class': 'permanent'}
        return {'decision': decision, 'seal': self._mac(decision)}

    def verify(self, proof, expected):
        try:
            decision = proof['decision']
            if (set(proof) != {'decision', 'seal'} or not _hash(proof['seal'])
                    or set(decision) != {'binding', 'request_id', 'actor_id', 'verification_method', 'verified_at',
                                         'confirmed_at', 'revoked', 'schema_version', 'retention_class'}
                    or not hmac.compare_digest(proof['seal'], self._mac(decision))
                    or decision['binding'] != expected or decision['actor_id'] != self.owner_actor_id
                    or decision['revoked'] is not False or decision['schema_version'] != 1
                    or decision['retention_class'] != 'permanent' or not request_id(decision['request_id'])
                    or type(decision['verified_at']) is not int or type(decision['confirmed_at']) is not int
                    or decision['verified_at']<=0
                    or decision['verification_method'] not in {'google_oidc_code_pkce_v1','google_oidc_shared_session_v2'}
                    or decision['verified_at'] > decision['confirmed_at']):
                raise ValueError()
        except (ValueError, TypeError, KeyError):
            raise StateError('pdf_intake_authority_proof_invalid') from None
        return {'valid': True, 'binding_digest': digest(expected),
                'authority_digest': digest(proof), 'actor_id': decision['actor_id'],
                'request_id': decision['request_id']}


class AuthorityAdapter:
    """Actions-side read-only gate. No Firestore/session permissions required."""
    def __init__(self, current_page, load_source, load_proof, verify_proof):
        self.current_page, self.load_source = current_page, load_source
        self.load_proof, self.verify_proof = load_proof, verify_proof

    def authorize(self, page, operation='single_page_ai', **unit_binding):
        expected = binding(page, operation, **unit_binding)
        current = validate_page(self.current_page(page['source']['source_file_id'], page['page_number']))
        if current != page:
            raise StateError('pdf_intake_page_stale')
        if (page['automatic_classification'] in {'medical', 'payroll'}
                or page['human_page_kind'] in {'medical', 'payroll'} or page['clearly_sensitive']
                or not page['observation_complete']):
            raise StateError('pdf_intake_sensitive_page_forbidden')
        raw = self.load_source(page['source']['source_file_id'])
        if type(raw) is not bytes or sha256(raw).hexdigest() != page['source']['source_content_hash']:
            raise StateError('pdf_intake_source_stale')
        if operation == 'single_page_ai' and page['automatic_classification'] == 'normal':
            return {'valid': True, 'basis': 'automatic_normal', 'binding_digest': digest(expected)}
        if operation == 'single_page_ai' and not eligible_for_hga(page):
            raise StateError('pdf_intake_hga_ineligible')
        proof = self.load_proof(page_key(page), operation, unit_binding.get('unit'))
        if proof is None:
            raise StateError('pdf_intake_authority_missing')
        verified = self.verify_proof(proof, expected)
        if (verified.get('valid') is not True or verified.get('binding_digest') != digest(expected)
                or verified.get('authority_digest') != digest(proof)
                or not _hash(verified.get('actor_id')) or not request_id(verified.get('request_id'))):
            raise StateError('pdf_intake_authority_verification_failed')
        # No cached fresh page may survive the backend verification round-trip.
        if validate_page(self.current_page(page['source']['source_file_id'], page['page_number'])) != page:
            raise StateError('pdf_intake_page_stale')
        return verified
