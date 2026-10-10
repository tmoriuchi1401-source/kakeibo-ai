"""One original, compact permanent metadata, finite private diagnostics.

This module only plans cleanup. It has no delete, Drive, ledger or token API.
Existing artifacts are never reclassified or expired merely by importing it.
"""
from datetime import datetime, timedelta, timezone
import json
from .drive_run_state import StateError

VERSION = 1
PERMANENT = {'ledger', 'original_source', 'source_binding', 'receipt_identity',
             'authority_decision', 'reconciliation_decision', 'duplicate_decision',
             'medical_confirmation', 'void', 'replacement', 'audit_event',
             'field_provenance', 'confirmation_receipt', 'current_state'}
# Raw AI/OCR responses default to no persistence, not a months-long archive.
POLICIES = {k: ('permanent', None) for k in PERMANENT} | {
    'analysis_details': ('medium', 90 * 86400),
    'segmentation_details': ('medium', 90 * 86400),
    'investigation_details': ('medium', 180 * 86400),
    'ocr_diagnostic_codes': ('medium', 30 * 86400),
    'retry_diagnostics': ('medium', 30 * 86400),
    'encrypted_actions_diagnostic': ('medium', 86400),
    'debug_log': ('ephemeral', 3 * 86400),
    'oauth_session': ('ephemeral', 600),
    'confirmation_request': ('ephemeral', 600),
    'temporary_processing': ('ephemeral', 3600),
    'temporary_png': ('ephemeral', 0),
    'raw_gemini_response': ('ephemeral', 0),
    'raw_ocr': ('ephemeral', 0),
}


def utc(value):
    try:
        result = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if result.tzinfo is None or result.utcoffset() != timedelta(0):
            raise ValueError()
        return result.astimezone(timezone.utc)
    except Exception:
        raise StateError('retention_utc_timestamp_required') from None


def metadata(kind, created_at, *, object_id, byte_size, namespace='kakeibo-temporary-v1'):
    if kind not in POLICIES or type(byte_size) is not int or byte_size < 0:
        raise StateError('retention_metadata_invalid')
    created = utc(created_at)
    retention, seconds = POLICIES[kind]
    return {'schema_version': VERSION, 'retention_class': retention, 'kind': kind,
            'object_id': object_id, 'namespace': namespace, 'byte_size': byte_size,
            'created_at': created.isoformat(), 'expires_at': None if seconds is None
            else (created + timedelta(seconds=seconds)).isoformat()}


def cleanup_dry_run(inventory, now):
    """Trusted inventory metadata only; permanent/legacy/ambiguous stay put.

    No filename inference, recursive deletion or live cleanup activation. A
    future executor must reread the exact object's generation/digest and policy
    before deletion. Zero-TTL pixels are released at processing teardown, never
    dumped to disk just to make this planner find them.
    """
    now = utc(now); counts = {}; candidates = []; protected = 0; rejected = 0
    for item in inventory:
        if not isinstance(item, dict):
            rejected += 1; continue
        if item.get('kind') in PERMANENT or item.get('retention_class') == 'permanent':
            protected += 1; continue
        try:
            expected = metadata(item['kind'], item['created_at'], object_id=item['object_id'],
                                byte_size=item['byte_size'], namespace=item['namespace'])
            if item != expected or item['namespace'] != 'kakeibo-temporary-v1':
                raise ValueError()
            if not isinstance(item['object_id'], str) or not 1 <= len(item['object_id']) <= 150:
                raise ValueError()
            if utc(item['expires_at']) > now:
                continue
        except Exception:
            rejected += 1; continue
        candidates.append({'object_id': item['object_id'], 'kind': item['kind'],
                           'byte_size': item['byte_size'], 'expires_at': item['expires_at']})
        counts[item['kind']] = counts.get(item['kind'], 0) + 1
    return {'schema_version': VERSION, 'mode': 'dry_run', 'deletion_enabled': False,
            'candidate_count': len(candidates), 'candidate_bytes': sum(i['byte_size'] for i in candidates),
            'counts_by_kind': counts, 'protected_count': protected, 'rejected_count': rejected,
            'candidates': candidates, 'deleted': 0}


def event_capacity(events):
    sizes = [len(json.dumps(e, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()) for e in events]
    if not sizes: raise StateError('retention_size_sample_required')
    mean = sum(sizes) / len(sizes)
    return {'samples': len(sizes), 'mean_bytes': mean, 'max_bytes': max(sizes),
            'estimated_bytes': {str(n): round(mean*n) for n in (10_000, 100_000, 1_000_000)}}
