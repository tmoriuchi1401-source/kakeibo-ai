"""Co-observation contract, no live Drive/native transport or version inference."""
from copy import deepcopy
from dataclasses import asdict, dataclass
from hashlib import sha256
import re

from .drive_run_state import StateError
from .iphone_native_dry_run import VersionBinding, check_version_binding
from .page_receipt_model import SourceRef, stable_page
from .unified_receipt_review import digest


def unknown():
    raise StateError('iphone_source_UNKNOWN')


@dataclass(frozen=True)
class SourceObservation:
    source_id: str
    sha256: str
    drive_revision_id: str
    drive_numeric_version: str | None
    native_source_version: str
    source_kind: str
    pages: list
    receipt_unit_id: str
    receipt_index: int
    purpose: str
    input_snapshot: str
    observed_at: int
    snapshot_digest: str

    def binding(self):
        return VersionBinding(self.source_id, self.sha256, self.drive_revision_id, self.native_source_version)


def observe(reader, sheet, native, *, purpose, input_snapshot, clock):
    """Trusted manager injects reader; browser values never implement this port.

    read_frame independently reads Drive metadata AND the existing native
    source/page/Unit snapshot. read_revision_bytes reads the exact revision.
    Both frames must match, including optional numeric version availability.
    Reader must fail when revision-specific bytes or independent native version
    cannot be obtained. Equality of the two version strings proves nothing.
    """
    try:
        first = deepcopy(reader.read_frame())
        raw = reader.read_revision_bytes(first['source_id'], first['drive_revision_id'])
        second = deepcopy(reader.read_frame())
        keys = {'source_id', 'sha256', 'drive_revision_id', 'drive_numeric_version',
                'native_source_version', 'source_kind', 'pages', 'receipt_unit_id', 'receipt_index', 'purpose'}
        if (type(raw) is not bytes or not raw or first != second or set(first) != keys
                or any(not isinstance(first[k], str) or not first[k] for k in
                       ('source_id', 'sha256', 'drive_revision_id', 'native_source_version'))
                or first['sha256'] != sha256(raw).hexdigest()
                or first['source_kind'] not in {'pdf', 'image'}
                or first['purpose'] != purpose or purpose not in {'ACCOUNTING_POST', 'STRUCTURE_REVIEW'}
                or not isinstance(input_snapshot, str) or not re.fullmatch('[a-f0-9]{64}', input_snapshot)):
            unknown()
        numeric = first['drive_numeric_version']
        if numeric is not None and (not isinstance(numeric, str) or not re.fullmatch('[1-9][0-9]*', numeric)):
            unknown()
        check_version_binding(VersionBinding(**{k:first[k] for k in
            ('source_id', 'sha256', 'drive_revision_id', 'native_source_version')}), sheet, native)
        pages = first['pages']
        if (not isinstance(pages, list) or not pages or any(set(p) != {'number', 'count', 'identity'}
                or type(p['number']) is not int or type(p['count']) is not int
                or not 1 <= p['number'] <= p['count']
                or p['identity'] != stable_page(SourceRef(source_file_id=first['source_id'],
                    source_content_hash=first['sha256'], page_count=p['count'], source_kind=first['source_kind']),
                    p['number']) for p in pages)
                or len({p['number'] for p in pages}) != len(pages)
                or len({p['count'] for p in pages}) != 1
                or [p['number'] for p in pages] != native['page_numbers']
                or [p['identity'] for p in pages] != native['page_identities']
                or first['receipt_unit_id'] != native['receipt_unit_id']
                or first['receipt_index'] != native['receipt_index']):
            unknown()
        if purpose == 'ACCOUNTING_POST':
            if (not first['receipt_unit_id'] or type(first['receipt_index']) is not int or first['receipt_index'] < 1):
                unknown()
        elif first['receipt_unit_id'] != '' or type(first['receipt_index']) is not int or first['receipt_index'] != 0:
            unknown()
        at = clock()
        if type(at) is not int or at <= 0:
            unknown()
        value = dict(deepcopy(first), input_snapshot=input_snapshot, observed_at=at)
        return SourceObservation(**value, snapshot_digest=digest(value))
    except (StateError, KeyError, TypeError, ValueError, AttributeError, OSError):
        unknown()


def verify_saved(observation, sheet, native, purpose, input_snapshot):
    """Pure saved-evidence check for GET. Does not claim current Drive freshness."""
    try:
        value = asdict(observation)
        signature = value.pop('snapshot_digest')
        if (signature != digest(value) or observation.purpose != purpose
                or observation.input_snapshot != input_snapshot
                or [p['number'] for p in observation.pages] != native['page_numbers']
                or [p['identity'] for p in observation.pages] != native['page_identities']
                or (observation.receipt_unit_id, observation.receipt_index) !=
                   (native['receipt_unit_id'], native['receipt_index'])):
            unknown()
        check_version_binding(observation.binding(), sheet, native)
    except (StateError, KeyError, TypeError, ValueError, AttributeError):
        unknown()


def revalidate(saved, reader, sheet, native, *, purpose, input_snapshot, stage, clock):
    """Mandatory fresh check before prepare, acceptance and future execution.
    This function never creates an acceptance/authority or dispatches a writer.
    """
    if stage not in {'prepare', 'acceptance', 'execution'}:
        unknown()
    verify_saved(saved, sheet, native, purpose, input_snapshot)
    fresh = observe(reader, sheet, native, purpose=purpose, input_snapshot=input_snapshot, clock=clock)
    identity = lambda o: {k:v for k,v in asdict(o).items() if k not in {'observed_at', 'snapshot_digest'}}
    if identity(saved) != identity(fresh):
        unknown()
    return fresh
