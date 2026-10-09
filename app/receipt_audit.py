"""Compact, year-partitioned append-only decision metadata and current state.

No PII text, raw snapshots or authority permissions. The host must supply
verified identity/freshness. Retention never erases posting/replay identities.
"""
from copy import deepcopy
from hashlib import sha256
import json
import re
from threading import RLock
from .drive_run_state import StateError
from .receipt_retention import utc

VERSION = 1
MAX_EVENT_BYTES = 2048
MAX_CURRENT_BYTES = 2048
EVENT_TYPES = {'reconciled_existing', 'confirmed_distinct', 'duplicate_confirmed',
               'imported', 'manual_imported', 'medical_manual_imported',
               'hga_confirmed', 'item_structure_confirmed', 'intentionally_skipped'}
ACTIONS = {k: ('reconcile_receipt' if k in {'reconciled_existing','confirmed_distinct','duplicate_confirmed'}
               else 'general_receipt_and_gemini_permission' if k == 'hga_confirmed'
               else 'confirm_receipt_item_snapshot' if k == 'item_structure_confirmed'
               else 'medical_manual_confirm' if k == 'medical_manual_imported'
               else 'skip_receipt' if k == 'intentionally_skipped' else 'post_receipt') for k in EVENT_TYPES}
REASONS = {'reconciled_existing': 'owner_verified_existing_receipt',
           'confirmed_distinct': 'owner_verified_distinct_pair', 'imported': 'writer_exact_readback',
           'duplicate_confirmed': 'explicit_duplicate_confirmed', 'hga_confirmed': 'explicit_hga_ai_consent',
           'intentionally_skipped': 'owner_intentionally_skipped', 'manual_imported': 'manual_writer_exact_readback',
           'medical_manual_imported': 'medical_manual_exact_readback',
           'item_structure_confirmed': 'owner_verified_item_snapshot'}
REASON_CODES = set(REASONS.values())
# Void/replacement are documented future event types; no posting/void handler.
TERMINAL = {'imported', 'manual_imported', 'medical_manual_imported', 'reconciled_existing',
            'duplicate_confirmed', 'intentionally_skipped', 'voided'}
VISIBLE = {'pending', 'requires_review', 'reconciliation_required',
           'needs_human_completion', 'needs_review', 'ready_to_write', 'confirmed_distinct'}
IDENTITY_FIELDS = {'source_file_id', 'source_content_hash', 'page_count', 'page_number',
                   'page_identity', 'receipt_unit_id', 'review_identity', 'revision'}
PROVENANCE_FIELDS = {'date', 'amount', 'category', 'merchant', 'payment', 'memo'}


def encoded(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode()


def digest(value): return sha256(encoded(value)).hexdigest()


def checked_hash(value):
    if not isinstance(value, str) or not re.fullmatch('[0-9a-f]{64}', value):
        raise StateError('audit_hash_invalid')


def identifier(value, *, empty=False):
    if empty and value == '': return
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_:.\-]{1,160}', value):
        raise StateError('audit_identifier_invalid')


def validate_identity(identity):
    try:
        if set(identity) != IDENTITY_FIELDS: raise ValueError()
        identifier(identity['source_file_id']); identifier(identity['receipt_unit_id'], empty=True)
        for k in ('source_content_hash', 'page_identity', 'review_identity'): checked_hash(identity[k])
        if (type(identity['page_count']) is not int or not 1 <= identity['page_count'] <= 50
                or type(identity['page_number']) is not int or not 1 <= identity['page_number'] <= identity['page_count']
                or type(identity['revision']) is not int or identity['revision'] < 1): raise ValueError()
        from .pdf_page_identity import page_identity
        if identity['page_identity'] != page_identity(identity['source_content_hash'], identity['page_number'], identity['page_count']):
            raise ValueError()
    except Exception: raise StateError('audit_identity_invalid') from None
    return identity


def entity_key(identity):
    validate_identity(identity)
    return digest(['receipt-current-v1', identity['source_file_id'], identity['source_content_hash'],
                   identity['page_identity'], identity['receipt_unit_id']])


def pair_key(identity, ledger_id):
    identifier(ledger_id)
    return digest(['receipt-comparison-v1', entity_key(identity), ledger_id])


def request_key(request_id):
    if not isinstance(request_id, str) or not re.fullmatch('[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}', request_id):
        raise StateError('audit_request_invalid')
    return digest(['receipt-confirmation-v1', request_id])


def event(identity, *, request_id, request_digest, event_type, ledger_id='', candidate_ledger_id='',
          decision, actor_id, confirmed_at, authority_digest, reason_code, provenance=None):
    validate_identity(identity)
    if event_type not in EVENT_TYPES: raise StateError('audit_event_type_invalid')
    for value in (request_digest, actor_id, authority_digest): checked_hash(value)
    identifier(ledger_id, empty=True); identifier(candidate_ledger_id, empty=True)
    if decision not in {'same', 'different', 'confirmed', 'skipped'}: raise StateError('audit_decision_invalid')
    if reason_code not in REASON_CODES: raise StateError('audit_reason_code_invalid')
    expected = {'reconciled_existing': ('same', True), 'confirmed_distinct': ('different', False)}.get(event_type)
    if expected and (decision != expected[0] or not candidate_ledger_id or bool(ledger_id) != expected[1]):
        raise StateError('audit_comparison_invalid')
    if event_type == 'reconciled_existing' and ledger_id != candidate_ledger_id:
        raise StateError('audit_comparison_invalid')
    provenance = provenance or {}
    if set(provenance) - PROVENANCE_FIELDS or any(v not in {'gemini', 'human', 'human_override', 'missing'} for v in provenance.values()):
        raise StateError('audit_provenance_invalid')
    when = utc(confirmed_at).isoformat()
    result = {'schema_version': VERSION, 'retention_class': 'permanent',
              'event_id': request_key(request_id), 'event_type': event_type, 'confirmed_action': ACTIONS[event_type], **deepcopy(identity),
              'ledger_id': ledger_id, 'candidate_ledger_id': candidate_ledger_id, 'decision': decision,
              'actor_id': actor_id, 'confirmed_at': when, 'authority_digest': authority_digest,
              'request_digest': request_digest, 'reason_code': reason_code, 'provenance': deepcopy(provenance)}
    result['event_digest'] = digest(result)
    validate_event(result)
    return result


def validate_event(value):
    try:
        fields = {'schema_version', 'retention_class', 'event_id', 'event_type', 'confirmed_action', *IDENTITY_FIELDS,
                  'ledger_id', 'candidate_ledger_id', 'decision', 'actor_id', 'confirmed_at',
                  'authority_digest', 'request_digest', 'reason_code', 'provenance', 'event_digest'}
        if set(value) != fields or type(value['schema_version']) is not int or value['schema_version'] != VERSION or value['retention_class'] != 'permanent': raise ValueError()
        validate_identity({k: value[k] for k in IDENTITY_FIELDS})
        for k in ('event_id', 'actor_id', 'authority_digest', 'request_digest', 'event_digest'): checked_hash(value[k])
        copy = dict(value); copy.pop('event_digest')
        if value['event_digest'] != digest(copy) or len(encoded(value)) > MAX_EVENT_BYTES: raise ValueError()
        if value['event_type'] not in EVENT_TYPES or value['confirmed_action'] != ACTIONS[value['event_type']] or value['decision'] not in {'same', 'different', 'confirmed', 'skipped'}: raise ValueError()
        identifier(value['ledger_id'], empty=True); identifier(value['candidate_ledger_id'], empty=True)
        if value['reason_code'] != REASONS[value['event_type']]: raise ValueError()
        if value['event_type'] in {'imported','manual_imported','medical_manual_imported','duplicate_confirmed'} and not value['ledger_id']: raise ValueError()
        if value['event_type'] not in {'reconciled_existing','confirmed_distinct'}:
            if value['decision'] != ('skipped' if value['event_type']=='intentionally_skipped' else 'confirmed'): raise ValueError()
        if set(value['provenance']) - PROVENANCE_FIELDS or any(v not in {'gemini','human','human_override','missing'} for v in value['provenance'].values()): raise ValueError()
        if value['event_type'] == 'reconciled_existing' and (value['decision'] != 'same' or not value['ledger_id'] or value['ledger_id'] != value['candidate_ledger_id']): raise ValueError()
        if value['event_type'] == 'confirmed_distinct' and (value['decision'] != 'different' or value['ledger_id'] or not value['candidate_ledger_id']): raise ValueError()
        utc(value['confirmed_at'])
    except Exception: raise StateError('audit_event_invalid') from None
    return value


def partition(value): return str(utc(value['confirmed_at']).year)


def pending(identity):
    validate_identity(identity)
    return {'schema_version': VERSION, 'retention_class': 'permanent', 'entity_key': entity_key(identity),
            'identity': deepcopy(identity), 'state_revision': 0, 'status': 'reconciliation_required',
            'ledger_id': '', 'last_event': None, 'last_authority_digest': ''}


def current_after(value, revision):
    validate_event(value)
    if type(revision) is not int or revision < 0: raise StateError('audit_current_revision_invalid')
    identity = {k: value[k] for k in IDENTITY_FIELDS}
    status = {'confirmed_distinct': 'confirmed_distinct', 'hga_confirmed': 'needs_human_completion',
              'item_structure_confirmed': 'ready_to_write'}.get(value['event_type'], value['event_type'])
    result = {**pending(identity), 'state_revision': revision+1, 'status': status,
              'ledger_id': value['ledger_id'], 'last_event': {'year': partition(value), 'event_id': value['event_id']},
              'last_authority_digest': value['authority_digest']}
    if len(encoded(result)) > MAX_CURRENT_BYTES: raise StateError('audit_current_size_exceeded')
    return result


def needs_attention(current):
    validate_current(current)
    if current['status'] in TERMINAL: return False
    if current['status'] in VISIBLE: return True
    raise StateError('audit_current_status_unknown')


def validate_current(value):
    try:
        if (set(value) != {'schema_version','retention_class','entity_key','identity','state_revision',
                          'status','ledger_id','last_event','last_authority_digest'}
                or type(value['schema_version']) is not int or value['schema_version'] != VERSION
                or value['retention_class'] != 'permanent'
                or value['entity_key'] != entity_key(value['identity'])
                or type(value['state_revision']) is not int or value['state_revision'] < 0
                or value['status'] not in TERMINAL | VISIBLE or len(encoded(value)) > MAX_CURRENT_BYTES):
            raise ValueError()
        identifier(value['ledger_id'], empty=True)
        if value['state_revision']==0:
            if value != pending(value['identity']): raise ValueError()
        else:
            ref=value['last_event']
            if set(ref)!={'year','event_id'} or not re.fullmatch('[0-9]{4}',ref['year']): raise ValueError()
            checked_hash(ref['event_id']);checked_hash(value['last_authority_digest'])
    except Exception: raise StateError('audit_current_invalid') from None
    return value


def receipt(value):
    return {'schema_version': VERSION, 'retention_class': 'permanent', 'request_digest': value['request_digest'],
            'event_digest': value['event_digest'], 'actor_id': value['actor_id'],
            'entity_key': entity_key({k: value[k] for k in IDENTITY_FIELDS}),
            'event_ref': {'year': partition(value), 'event_id': value['event_id']}}


class MemoryAuditRepository:
    """Synthetic atomic store with the same per-document production layout."""
    def __init__(self):
        self.events = {}; self.current = {}; self.receipts = {}; self.pairs = {}; self.writes = 0; self.lock = RLock()

    def get_current(self, identity): return deepcopy(validate_current(self.current.get(entity_key(identity), pending(identity))))
    def lookup(self, rid): return deepcopy(self.receipts.get(request_key(rid)))
    def get_event(self, ref): return deepcopy(self.events[(ref['year'], ref['event_id'])])
    def get_pair(self, identity, ledger_id): return deepcopy(self.pairs.get(pair_key(identity, ledger_id)))

    def commit(self, value, expected_revision):
        validate_event(value)
        if type(expected_revision) is not int or expected_revision < 0: raise StateError('audit_current_revision_invalid')
        with self.lock:
            prior = self.receipts.get(value['event_id'])
            if prior:
                if prior != receipt(value): raise StateError('audit_request_reuse')
                return {'replayed': True, 'receipt': deepcopy(prior)}
            identity = {k: value[k] for k in IDENTITY_FIELDS}; key = entity_key(identity)
            current = self.get_current(identity)
            if current['state_revision'] != expected_revision or current['identity'] != identity:
                raise StateError('HTTP_412')
            if not needs_attention(current): raise StateError('audit_terminal_operation_forbidden')
            next_state = current_after(value, expected_revision)
            # All validation happens before the atomic write.
            pair = pair_key(identity, value['candidate_ledger_id']) if value['event_type'] in {'reconciled_existing','confirmed_distinct'} else None
            if pair and pair in self.pairs: raise StateError('audit_pair_already_confirmed')
            self.events[(partition(value), value['event_id'])] = deepcopy(value)
            self.receipts[value['event_id']] = receipt(value); self.current[key] = next_state
            if pair: self.pairs[pair] = {'decision': value['decision'], 'event_ref': next_state['last_event'], 'authority_digest': value['authority_digest']}
            self.writes += 1
            return {'replayed': False, 'receipt': receipt(value)}
