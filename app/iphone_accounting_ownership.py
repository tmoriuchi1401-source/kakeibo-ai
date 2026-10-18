"""Trusted ownership observations for an explicitly isolated synthetic gate.

No reader factory, live client, authority issuance, ledger repair or UI input.
Whole-source ownership remains distinct from Unit ownership.
"""
from copy import deepcopy
from dataclasses import asdict, dataclass
from hashlib import sha256
import re

from .drive_run_state import StateError
from .page_receipt_model import SourceRef, stable_page
from .unified_receipt_review import digest
from .unified_receipt_native import confirmation_operation_identity, pdf_operation_identity

SCHEMA = 'synthetic-accounting-ownership-v1'
SOURCE_KEYS = {'source_id', 'sha256', 'drive_revision_id', 'drive_numeric_version',
               'native_source_version', 'source_kind', 'page_count'}


def unknown():
    raise StateError('accounting_ownership_UNKNOWN')


def hashed(value):
    return isinstance(value, str) and re.fullmatch('[a-f0-9]{64}', value) is not None


def positive(value):
    return type(value) is int and value > 0


def source_valid(source):
    if (not isinstance(source, dict) or set(source) != SOURCE_KEYS
            or any(not isinstance(source[k], str) or not source[k] for k in
                   ('source_id', 'drive_revision_id', 'native_source_version'))
            or not hashed(source['sha256']) or source['source_kind'] not in {'pdf', 'image'}
            or not re.fullmatch('[A-Za-z0-9_-]{1,150}', source['source_id'])
            or not isinstance(source['drive_numeric_version'], str)
            or not re.fullmatch('[1-9][0-9]*', source['drive_numeric_version'])
            or not positive(source['page_count']) or source['page_count'] > 50
            or source['source_kind'] == 'image' and source['page_count'] != 1):
        unknown()


def partition_valid(partition, source):
    if (set(partition) != {'source', 'generation', 'revision', 'state', 'complete', 'units'}
            or partition['source'] != source or not positive(partition['generation'])
            or not positive(partition['revision']) or partition['complete'] is not True
            or partition['state'] not in {'confirmed', 'not_applicable'}
            or not isinstance(partition['units'], list)):
        unknown()
    if partition['state'] == 'not_applicable':
        if source['source_kind'] != 'image' or partition['units']:
            unknown()
        return
    if not partition['units']:
        unknown()
    units, positions = set(), set()
    for unit in partition['units']:
        if (set(unit) != {'unit_id', 'receipt_index', 'members'}
                or not isinstance(unit['unit_id'], str) or not unit['unit_id']
                or unit['unit_id'] in units or not positive(unit['receipt_index'])
                or not isinstance(unit['members'], list) or not unit['members']):
            unknown()
        units.add(unit['unit_id']); pages = []
        for member in unit['members']:
            if (set(member) != {'page', 'identity', 'box'} or not positive(member['page'])
                    or member['page'] > source['page_count']
                    or member['identity'] != stable_page(SourceRef(source_file_id=source['source_id'],
                        source_content_hash=source['sha256'], source_kind=source['source_kind'],
                        page_count=source['page_count']), member['page'])
                    or not isinstance(member['box'], list) or len(member['box']) != 4
                    or any(type(x) not in (int, float) or not 0 <= x <= 1 for x in member['box'])):
                unknown()
            x0, y0, x1, y1 = member['box']
            if not x0 < x1 or not y0 < y1:
                unknown()
            pages.append(member['page'])
        if pages != sorted(set(pages)) or (pages[0], unit['receipt_index']) in positions:
            unknown()
        positions.add((pages[0], unit['receipt_index']))
    for i, left in enumerate(partition['units']):
        for right in partition['units'][i+1:]:
            if overlap(left['members'], right['members']):
                unknown()


def overlap(left, right):
    for a in left:
        for b in right:
            if a['page'] == b['page']:
                x0, y0, x1, y1 = a['box']; u0, v0, u1, v1 = b['box']
                if max(x0, u0) < min(x1, u1) and max(y0, v0) < min(y1, v1):
                    return True
    return False


def native_identity(native, source, unit):
    kind = native['namespace']
    if kind == 'receipt_confirmation' and unit is None:
        return confirmation_operation_identity({'source': {'source_id': source['source_id'],
            'sha256': source['sha256'], 'version': source['native_source_version']}})
    if kind == 'pdf_intake' and unit is not None and len(unit['members']) == 1:
        member = unit['members'][0]
        page = native['native_page']
        if (not isinstance(page, dict) or page['source']['source_file_id'] != source['source_id']
                or page['source']['source_content_hash'] != source['sha256']
                or page['source']['page_count'] != source['page_count']
                or page['page_number'] != member['page'] or page['stable_page_identity'] != member['identity']):
            unknown()
        return pdf_operation_identity(page, unit['unit_id'])
    if kind == 'pdf_unit_processing' and unit is not None:
        return digest(['pdf-unit-processing', unit['unit_id']])
    if kind == 'receipt_unit_posting' and unit is not None:
        if not native['native_context'] or not native['native_request_id']:
            unknown()
        return digest(['receipt-unit-posting', native['native_context'], native['native_request_id']])
    if kind == 'p14_posting' and unit is not None and [m['page'] for m in unit['members']] == [14]:
        if not native['native_request_id']:
            unknown()
        return digest(['p14-posting', native['native_request_id']])
    unknown()


@dataclass(frozen=True)
class Ownership:
    owner: str
    case_id: str
    source: dict
    partition: dict
    scope: str
    unit: dict | None
    operation_identity: str
    input_snapshot: str
    authority_revision: int
    ledger_targets: list
    observation_digest: str

    def value(self):
        return asdict(self)


def parse_ownership(value):
    """Strict persisted fence parsing; does not establish current freshness."""
    try:
        observation = Ownership(**deepcopy(value))
        source_valid(observation.source)
        partition_valid(observation.partition, observation.source)
        if (not hashed(observation.owner) or not isinstance(observation.case_id,str) or not observation.case_id
                or observation.scope not in {'whole', 'unit'}
                or observation.scope == 'whole' and observation.unit is not None
                or observation.scope == 'unit' and (observation.unit is None or
                    observation.unit not in observation.partition['units'])
                or not all(hashed(v) for v in (observation.operation_identity, observation.input_snapshot,
                                               observation.observation_digest))
                or not positive(observation.authority_revision)
                or not isinstance(observation.ledger_targets, list) or not observation.ledger_targets
                or any(not isinstance(x, str) or not x for x in observation.ledger_targets)
                or len(set(observation.ledger_targets)) != len(observation.ledger_targets)):
            unknown()
        return observation
    except (TypeError, ValueError, KeyError, AttributeError):
        unknown()


def relation(left, right):
    """Compare persisted trusted scopes, regardless of entry or request ID."""
    left, right = parse_ownership(left), parse_ownership(right)
    if left.owner != right.owner:
        unknown()
    if left.source['source_id'] != right.source['source_id']:
        if (left.operation_identity == right.operation_identity
                or set(left.ledger_targets) & set(right.ledger_targets)):
            unknown()
        return 'independent'
    if left.source != right.source or left.partition != right.partition:
        unknown()  # Includes same hash but different revision/native version/partition.
    if left.scope == 'whole' or right.scope == 'whole':
        return 'conflict'
    if (left.unit['unit_id'] == right.unit['unit_id'] or
            overlap(left.unit['members'], right.unit['members'])):
        return 'conflict'
    if (left.operation_identity == right.operation_identity or
            set(left.ledger_targets) & set(right.ledger_targets)):
        unknown()
    return 'independent'


class TrustedOwnershipReader:
    """Five independently injected trusted ports; no Sheet/browser adapter.

    Each read is a versioned frame. Two complete observations surround the
    revision-specific bytes read. Real transport and ABA guarantees are unproven.
    """
    def __init__(self, source, partition, native, ledger, pending):
        self.source, self.partition, self.native = source, partition, native
        self.ledger, self.pending = ledger, pending

    def _frames(self, entry, key):
        return deepcopy(dict(source=self.source.read(), partition=self.partition.read(),
            native=self.native.read(entry, key), ledger=self.ledger.read(), pending=self.pending.read()))

    @property
    def synthetic_memory_only(self):
        return all(getattr(port, 'synthetic_memory_only', False) is True for port in
                   (self.source, self.partition, self.native, self.ledger, self.pending))

    def observe(self, entry, key):
        try:
            first = self._frames(entry, key)
            drive = first['source']
            raw = self.source.revision_bytes(drive['source_id'], drive['drive_revision_id'])
            if first != self._frames(entry, key) or type(raw) is not bytes or not raw:
                unknown()
            n = first['native']
            if (set(drive) != SOURCE_KEYS - {'native_source_version'} | {'generation'}
                    or not positive(drive['generation'])
                    or drive['sha256'] != sha256(raw).hexdigest()
                    or set(n) != {'generation', 'owner', 'source', 'case_id', 'purpose', 'action', 'namespace',
                        'native_context', 'native_request_id', 'native_page', 'scope', 'unit_id', 'receipt_index',
                        'operation_identity', 'input_snapshot', 'authority_revision', 'ledger_targets', 'state'}
                    or not positive(n['generation']) or n['case_id'] in {'SYN-G103', 'SYN-H301', 'G103', 'H301'}
                    or n['purpose'] != 'ACCOUNTING_POST' or n['action'] not in {'post', 'buyback_income_post'}
                    or n['state'] != 'ready'):
                unknown()
            source = n['source']; source_valid(source)
            routes = {'normal_auto': ('whole', 'receipt_confirmation'),
                      'buyback_income': ('whole', 'receipt_confirmation'),
                      'legacy_confirmation': ('whole', 'receipt_confirmation'),
                      'pdf_auto': ('unit', 'pdf_intake'), 'pdf_manual': ('unit', 'pdf_intake'),
                      'grouped_unit': ('unit', 'pdf_unit_processing'),
                      'fixed_canary': ('unit', 'p14_posting'),
                      'explicit_unit': ('unit', 'receipt_unit_posting')}
            if (entry in routes and (n['scope'], n['namespace']) != routes[entry]
                    or (entry == 'buyback_income') != (n['action'] == 'buyback_income_post')):
                unknown()
            if {k: v for k, v in drive.items() if k != 'generation'} != {
                    k: v for k, v in source.items() if k != 'native_source_version'}:
                unknown()
            partition = first['partition']; partition_valid(partition, source)
            if n['scope'] == 'whole':
                if n['unit_id'] is not None or n['receipt_index'] is not None:
                    unknown()
                unit = None
            elif n['scope'] == 'unit':
                candidates = [u for u in partition['units'] if u['unit_id'] == n['unit_id']
                              and u['receipt_index'] == n['receipt_index']]
                if len(candidates) != 1:
                    unknown()
                unit = candidates[0]
            else:
                unknown()
            if native_identity(n, source, unit) != n['operation_identity']:
                unknown()
            if not hashed(n['input_snapshot']) or not positive(n['authority_revision']):
                unknown()
            for kind in ('ledger', 'pending'):
                frame = first[kind]
                if (set(frame) not in ({'generation', 'complete', 'records'},
                                      {'generation', 'complete', 'records', 'native_rows_digest'})
                        or not positive(frame['generation']) or frame['complete'] is not True
                        or not isinstance(frame['records'], list)
                        or 'native_rows_digest' in frame and not hashed(frame['native_rows_digest'])):
                    unknown()
            result = parse_ownership(dict(owner=n['owner'], case_id=n['case_id'], source=source, partition=partition, scope=n['scope'], unit=unit,
                operation_identity=n['operation_identity'], input_snapshot=n['input_snapshot'],
                authority_revision=n['authority_revision'], ledger_targets=n['ledger_targets'],
                observation_digest=digest(first)))
            # Legacy pending (including an apparently different Unit) is not
            # converted into a new grant or automatically migrated/recovered.
            for pending in first['pending']['records']:
                prior = parse_ownership(pending)
                if (prior.source['source_id'] == source['source_id'] or
                        prior.operation_identity == result.operation_identity or
                        set(prior.ledger_targets) & set(result.ledger_targets)):
                    unknown()
            ledger_ids = set()
            for record in first['ledger']['records']:
                if (set(record) != {'transaction_id', 'ownership'} or not record['transaction_id']
                        or record['transaction_id'] in ledger_ids):
                    unknown()
                ledger_ids.add(record['transaction_id'])
                prior = parse_ownership(record['ownership'])
                if record['transaction_id'] not in prior.ledger_targets:
                    unknown()
                if relation(result.value(), prior.value()) != 'independent':
                    raise StateError('accounting_ownership_already_recorded')
            return result
        except StateError:
            raise
        except Exception:
            unknown()
