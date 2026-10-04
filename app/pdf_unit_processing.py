"""Private per-Unit completion journal, separate from grouping permission.

Reuse the existing Drive v2 conditional transport. Runtime never provisions,
repairs or restores missing state from local files. This module has no writer,
AI, Medical parser or mover. Medical completion stores only an existing backend
reference; Medical inputs and accounting plans remain in that backend.
"""
from copy import deepcopy
from datetime import datetime, timezone
from hashlib import sha256
import json
import math
import re

from .drive_run_state import StateError, _aware
from .pdf_page_identity import page_identity

SCHEMA = 'pdf-unit-processing-v1'
MAX_BYTES = 8 * 1024 * 1024
KINDS = {'normal', 'medical', 'payroll', 'sensitive_unknown'}
ROUTES = {'receipt': 'imported', 'general_manual': 'manual_imported',
          'medical_manual': 'medical_manual_imported',
          'duplicate_confirmed': 'duplicate_confirmed',
          'intentionally_skipped': 'intentionally_skipped'}
TABLES = {'レシート': 9, '支出明細': 13, '取込データ': 12}


def encoded(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True,
                      separators=(',', ':'), allow_nan=False).encode()


def digest(value):
    return sha256(encoded(value)).hexdigest()


def _hash(value):
    return isinstance(value, str) and re.fullmatch('[0-9a-f]{64}', value) is not None


def unit_spec(source_id, source_hash, page_count, numbers, automatic, human,
              *, revision, proposal_digest, confirmation_digest, kind_digests):
    """Caller supplies freshly validated Drive evidence, never UI cells."""
    spec = {'identity_scope': 'grouping', 'source_file_id': source_id, 'source_content_hash': source_hash,
        'page_count': page_count, 'page_numbers': list(numbers),
        'member_page_identities': [page_identity(source_hash, n, page_count) for n in numbers],
        'automatic_classifications': list(automatic), 'human_classifications': list(human),
        'grouping_revision': revision, 'proposal_digest': proposal_digest,
        'confirmation_digest': confirmation_digest, 'page_kind_digests': list(kind_digests)}
    spec['unit_id'] = 'pdf-confirmed-unit-v2:' + digest(['pdf-unit-v2', source_id, source_hash,
        spec['page_numbers'], spec['member_page_identities'], revision, proposal_digest])
    _spec(spec)
    return spec


def medical_page_spec(source_id, source_hash, page_count, number, automatic,
                      human_kind_digest):
    """Completion reference only; never changes the existing Medical review ID.

    A Medical page outside the general grouping partition must not wait for or
    inherit general grouping authority. The caller verifies the existing durable
    Medical manual confirmation and source/page-kind proof independently.
    """
    identity=page_identity(source_hash, number, page_count)
    spec={'identity_scope': 'page_kind', 'source_file_id': source_id,
        'source_content_hash': source_hash, 'page_count': page_count,
        'page_numbers': [number], 'member_page_identities': [identity],
        'automatic_classifications': [automatic], 'human_classifications': ['medical'],
        'grouping_revision': 0, 'proposal_digest': '',
        'confirmation_digest': human_kind_digest, 'page_kind_digests': [human_kind_digest]}
    spec['unit_id']='pdf-page-kind-unit-v1:'+digest(['pdf-page-kind-unit-v1',
        source_id, source_hash, number, page_count, identity, human_kind_digest])
    _spec(spec)
    return spec


def _spec(s):
    if (set(s) != {'identity_scope', 'unit_id', 'source_file_id', 'source_content_hash', 'page_count',
            'page_numbers', 'member_page_identities', 'automatic_classifications',
            'human_classifications', 'grouping_revision', 'proposal_digest',
            'confirmation_digest', 'page_kind_digests'}
            or not isinstance(s['source_file_id'], str)
            or not re.fullmatch('[A-Za-z0-9_-]{1,150}', s['source_file_id'])
            or not _hash(s['source_content_hash']) or type(s['page_count']) is not int
            or not 1 <= s['page_count'] <= 50 or type(s['grouping_revision']) is not int
            or s['identity_scope'] not in {'grouping','page_kind'}
            or not _hash(s['confirmation_digest'])):
        raise ValueError()
    ns = s['page_numbers']
    if (not isinstance(ns, list) or not ns or ns != sorted(set(ns))
            or any(type(n) is not int or not 1 <= n <= s['page_count'] for n in ns)
            or s['member_page_identities'] != [page_identity(s['source_content_hash'], n, s['page_count']) for n in ns]
            or any(not isinstance(s[k], list) or len(s[k]) != len(ns) for k in
                ('automatic_classifications', 'human_classifications', 'page_kind_digests'))
            or any(x not in KINDS for x in s['automatic_classifications'] + s['human_classifications'])
            or any(x != '' and not _hash(x) for x in s['page_kind_digests'])):
        raise ValueError()
    if s['identity_scope']=='grouping':
        if (s['grouping_revision']<1 or not _hash(s['proposal_digest'])
                or s['unit_id'] != 'pdf-confirmed-unit-v2:' + digest(['pdf-unit-v2',
                    s['source_file_id'], s['source_content_hash'], ns, s['member_page_identities'],
                    s['grouping_revision'], s['proposal_digest']])):
            raise ValueError()
    elif (len(ns)!=1 or s['grouping_revision']!=0 or s['proposal_digest']!=''
            or s['human_classifications']!=['medical']
            or s['page_kind_digests']!=[s['confirmation_digest']]
            or s['unit_id']!='pdf-page-kind-unit-v1:'+digest(['pdf-page-kind-unit-v1',
                s['source_file_id'],s['source_content_hash'],ns[0],s['page_count'],
                s['member_page_identities'][0],s['confirmation_digest']])):
        raise ValueError()


def _route(s, route, plan, reference):
    if route not in ROUTES or not _hash(reference):
        raise ValueError()
    a, h = s['automatic_classifications'], s['human_classifications']
    if s['identity_scope']=='page_kind' and route not in {'medical_manual','duplicate_confirmed','intentionally_skipped'}:
        raise ValueError()
    if route == 'receipt' and (set(a + h) != {'normal'}):
        raise ValueError()
    if route == 'general_manual' and (set(h) != {'normal'} or
            any(x in {'medical', 'payroll'} for x in a) or
            any(x == 'sensitive_unknown' and not k for x, k in zip(a, s['page_kind_digests']))):
        raise ValueError()
    # Medical completion is only a reference to the existing manual backend.
    # It never inherits permission to use the normal writer or AI.
    if route == 'medical_manual' and (set(h) != {'medical'} or not all(s['page_kind_digests'])):
        raise ValueError()
    if not isinstance(plan, dict):
        raise ValueError()
    required = {'receipt': set(TABLES), 'general_manual': {'支出明細'}}.get(route, set())
    if set(plan) != required:
        raise ValueError()
    for title, rows in plan.items():
        if not isinstance(rows, list) or not 1 <= len(rows) <= 500:
            raise ValueError()
        for row in rows:
            if (not isinstance(row, list) or len(row) != TABLES[title] or
                    any(type(x) not in {str, int, float} or
                        (type(x) is float and not math.isfinite(x)) for x in row)):
                raise ValueError()
        if title != '支出明細' and len(rows) != 1:
            raise ValueError()
    if route == 'general_manual' and len(plan['支出明細']) != 1:
        raise ValueError()


def empty_state(binding):
    if not _hash(binding):
        raise StateError('pdf_processing_binding_invalid')
    return {'schema': SCHEMA, 'binding': binding, 'generation': 0, 'records': {}, 'audit': []}


def _intent(record):
    return {k: record[k] for k in ('unit', 'route', 'input_digest', 'planned_rows', 'writer_reference')}


def validate(value, binding):
    try:
        if (not _hash(binding) or set(value) != {'schema', 'binding', 'generation', 'records', 'audit'}
                or value['schema'] != SCHEMA or value['binding'] != binding
                or type(value['generation']) is not int or value['generation'] < 0
                or not isinstance(value['records'], dict) or not isinstance(value['audit'], list)
                or value['generation'] != len(value['audit'])):
            raise ValueError()
        coverage = set()
        for key, r in value['records'].items():
            if set(r) != {'unit', 'route', 'input_digest', 'planned_rows', 'writer_reference',
                         'intent_digest', 'phase', 'created_at', 'completed_at'}:
                raise ValueError()
            _spec(r['unit']); _route(r['unit'], r['route'], r['planned_rows'], r['writer_reference'])
            if (key != r['unit']['unit_id'] or not _hash(r['input_digest'])
                    or r['intent_digest'] != digest(_intent(r)) or r['phase'] not in {'pending', 'applied'}):
                raise ValueError()
            _aware(r['created_at'])
            if r['phase'] == 'applied':
                if _aware(r['completed_at']) < _aware(r['created_at']): raise ValueError()
            elif r['completed_at'] != '': raise ValueError()
            for n in r['unit']['page_numbers']:
                p = (r['unit']['source_file_id'], r['unit']['source_content_hash'], n)
                if p in coverage: raise ValueError()
                coverage.add(p)
        for event in value['audit']:
            if (set(event) != {'operation', 'timestamp', 'unit_id', 'source_file_id',
                    'source_content_hash', 'grouping_revision', 'proposal_digest',
                    'confirmation_digest', 'intent_digest', 'result'}
                    or event['operation'] not in {'reserve', 'complete'}
                    or event['result'] != {'reserve': 'pending', 'complete': 'applied'}[event['operation']]):
                raise ValueError()
            _aware(event['timestamp']); record = value['records'][event['unit_id']]
            if any(event[k] != record['unit'][k] for k in ('source_file_id',
                    'source_content_hash', 'grouping_revision', 'proposal_digest', 'confirmation_digest')):
                raise ValueError()
            if event['intent_digest'] != record['intent_digest']: raise ValueError()
        # Reject orphan records, repeated completion and invented applied flags.
        for key, r in value['records'].items():
            events = [e for e in value['audit'] if e['unit_id'] == key]
            if [e['operation'] for e in events] != (['reserve', 'complete'] if r['phase'] == 'applied' else ['reserve']):
                raise ValueError()
            if events[0]['timestamp'] != r['created_at'] or (r['phase'] == 'applied' and
                    events[1]['timestamp'] != r['completed_at']): raise ValueError()
        return value
    except Exception:
        raise StateError('pdf_processing_state_invalid') from None


def _unique_pairs(pairs):
    value = {}
    for key, item in pairs:
        if key in value: raise ValueError()
        value[key] = item
    return value


class DriveUnitProcessingStore:
    """ACL -> versioned read -> If-Match -> exact read-back, no retry/fallback."""
    def __init__(self, transport, binding, *, preflight, clock=None):
        if not _hash(binding) or not callable(preflight):
            raise StateError('pdf_processing_binding_invalid')
        self.transport, self.binding, self.preflight = transport, binding, preflight
        self.clock = clock or (lambda: datetime.now(timezone.utc).isoformat())
        self.payload = self.tag = None

    def load(self):
        from .conditional_drive_state_v2 import strong_etag
        try:
            self.preflight()
            payload, tag = self.transport.read_versioned()
            if not isinstance(payload, bytes) or len(payload) > MAX_BYTES or not strong_etag(tag):
                raise StateError('pdf_processing_read_unavailable')
            value = validate(json.loads(payload, object_pairs_hook=_unique_pairs), self.binding)
            self.payload, self.tag = payload, tag
            return deepcopy(value)
        except StateError: raise
        except Exception: raise StateError('pdf_processing_read_unavailable') from None

    def _save(self, value):
        validate(value, self.binding); proposed = encoded(value)
        if len(proposed) > MAX_BYTES: raise StateError('pdf_processing_size_limit')
        self.preflight()
        self.transport.replace_versioned(self.payload, self.tag, proposed)
        if self.load() != value or self.payload != proposed:
            raise StateError('pdf_processing_readback_mismatch')

    def _event(self, value, record, operation):
        unit = record['unit']
        value['audit'].append({'operation': operation,
            'timestamp': record['created_at'] if operation == 'reserve' else record['completed_at'],
            'unit_id': unit['unit_id'], **{k: unit[k] for k in ('source_file_id',
                'source_content_hash', 'grouping_revision', 'proposal_digest', 'confirmation_digest')},
            'intent_digest': record['intent_digest'],
            'result': 'pending' if operation == 'reserve' else 'applied'})
        value['generation'] += 1

    def reserve(self, spec, route, input_digest, plan, writer_reference, *, verify_current):
        value = self.load()
        record = {'unit': deepcopy(spec), 'route': route, 'input_digest': input_digest,
            'planned_rows': deepcopy(plan), 'writer_reference': writer_reference,
            'phase': 'pending', 'created_at': self.clock(), 'completed_at': ''}
        record['intent_digest'] = digest(_intent(record))
        trial = empty_state(self.binding); trial['records'][spec['unit_id']] = record
        self._event(trial, record, 'reserve'); validate(trial, self.binding)
        if verify_current(deepcopy(spec)) is not True:
            raise StateError('pdf_processing_authority_or_source_changed')
        previous = value['records'].get(spec['unit_id'])
        if previous:
            if _intent(previous) != _intent(record):
                raise StateError('pdf_processing_intent_changed')
            # Existing pending intent must be reconciled, never automatically retried.
            return deepcopy(previous), False
        for old in value['records'].values():
            s = old['unit']
            if (s['source_file_id'] == spec['source_file_id'] and
                    s['source_content_hash'] == spec['source_content_hash'] and
                    set(s['page_numbers']) & set(spec['page_numbers'])):
                raise StateError('pdf_processing_page_already_claimed')
        value['records'][spec['unit_id']] = record
        self._event(value, record, 'reserve'); self._save(value)
        return deepcopy(record), True

    def complete(self, unit_id, intent_digest, *, verify_current, verify_readback):
        value = self.load(); record = value['records'].get(unit_id)
        if not record or record['intent_digest'] != intent_digest:
            raise StateError('pdf_processing_intent_changed')
        if verify_current(deepcopy(record['unit'])) is not True:
            raise StateError('pdf_processing_authority_or_source_changed')
        if verify_readback(deepcopy(record)) is not True:
            raise StateError('pdf_processing_accounting_readback_mismatch')
        # Close the source/authority race across accounting read-back as well.
        if verify_current(deepcopy(record['unit'])) is not True:
            raise StateError('pdf_processing_authority_or_source_changed')
        if record['phase'] == 'applied': return deepcopy(record)
        record['phase'], record['completed_at'] = 'applied', self.clock()
        self._event(value, record, 'complete'); self._save(value)
        return deepcopy(record)

    def verify_pending(self, unit_id, intent_digest):
        """Fresh Drive read immediately before entering the existing writer."""
        record=self.load()['records'].get(unit_id)
        if not record or record['phase']!='pending' or record['intent_digest']!=intent_digest:
            raise StateError('pdf_processing_pending_intent_changed')
        return True
