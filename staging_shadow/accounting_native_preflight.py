"""Read-only adapter around existing native validators and versioned state.

The host supplies the native validator, never browser approval or the Phase
5B-2A acknowledgement. Native claims/writers are deliberately not invoked.
"""
from dataclasses import dataclass
from copy import deepcopy

from app.drive_run_state import StateError
from app.iphone_accounting_gate import NativePreflight
from app.pdf_intake_registry import strong_etag
from app.unified_receipt_review import digest


@dataclass(frozen=True)
class NativeValidation:
    operation_identity: str
    input_snapshot: str
    authority_revision: int
    result_digest: str
    ledger_targets: tuple


class NativeReadAdapter:
    def __init__(self, read_versioned, validate, *, expected_source, expected_partition, expected_case_id):
        self.read_versioned, self.validate = read_versioned, validate
        self.source_binding=deepcopy(expected_source)
        self.partition_digest=digest(expected_partition)
        self.case_id=expected_case_id
        self.expected_state=read_versioned()

    def __call__(self, observation):
        try:
            before, tag = self.read_versioned()
        except Exception:
            raise StateError('synthetic_native_state_unavailable') from None
        if type(before) is not bytes or not strong_etag(tag):
            raise StateError('synthetic_native_state_unavailable')
        if ((before,tag)!=self.expected_state or self.source_binding!=observation.source
                or self.partition_digest!=digest(observation.partition) or self.case_id!=observation.case_id):
            raise StateError('synthetic_native_redisplay_required')
        try:
            checked = self.validate(observation)
        except Exception:
            raise StateError('synthetic_native_validation_rejected') from None
        if (not isinstance(checked, NativeValidation)
                or (checked.operation_identity, checked.input_snapshot, checked.authority_revision) !=
                   (observation.operation_identity, observation.input_snapshot, observation.authority_revision)
                or checked.ledger_targets != tuple(observation.ledger_targets)):
            raise StateError('synthetic_native_binding_changed')
        try:
            after = self.read_versioned()
        except Exception:
            raise StateError('synthetic_native_state_unavailable') from None
        if after != (before, tag):
            raise StateError('synthetic_native_CAS_precondition_changed')
        return NativePreflight(digest(observation.value()), checked.result_digest, digest(before.hex()), tag)


def verify_registered_pdf_partition(observation, registry, page_key, unit_id):
    """Bind the trusted ownership partition to the CURRENT native manifest.

    Native manual/auto validators still run separately. This catches a native
    segmentation/range change even if an upstream saved projection is stale.
    """
    try:
        page = registry['pages'][page_key]['page']
        candidate = registry['pages'][page_key]['units'][unit_id]['candidate']
        authority = registry['pages'][page_key]['units'][unit_id]['posting_authority']
        manifest = candidate['manifest']
        units = [dict(unit_id=u['receipt_unit_id'],receipt_index=u['receipt_index'],members=[dict(
            page=page['page_number'],identity=page['stable_page_identity'],
            box=[u['bbox'][k] for k in ('left','top','right','bottom')])]) for u in manifest['units']]
        if (observation.scope != 'unit' or observation.unit['unit_id'] != unit_id
                or observation.partition['units'] != units
                or page['source']['source_file_id'] != observation.source['source_id']
                or page['source']['source_content_hash'] != observation.source['sha256']
                or page['source']['page_count'] != observation.source['page_count']
                or authority is not None and authority['decision']['actor_id'] != observation.owner
                or manifest['segmentation_digest'] != candidate['record']['legacy']['identity']['segmentation_digest']
                or 'posting' in candidate and manifest['segmentation_digest'] != candidate['posting']['unit_binding']['segmentation_digest']):
            raise ValueError()
    except Exception:
        raise StateError('synthetic_native_partition_changed') from None
