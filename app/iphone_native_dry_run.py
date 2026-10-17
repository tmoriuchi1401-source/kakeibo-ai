"""Raw draft -> existing purpose-specific preview, with trusted host context.

No context is synthesized from Sheets. Missing Unit/page/snapshot context holds
the case. A field-only medical check is explicitly weaker than native handoff.
"""
from copy import deepcopy
from datetime import date, timedelta
from dataclasses import dataclass
from types import SimpleNamespace

from .iphone_sheet_reader import deny, raw_value, CASE_KINDS
from .iphone_review_drafts import snapshot, check_row
from .iphone_review_preview import preview_medical, preview_general, preview_structure
from .unified_receipt_review import digest

KINDS = {'一般': 'normal', '医療': 'medical', 'PDF': 'grouping'}


@dataclass(frozen=True)
class VersionBinding:
    source_id: str
    sha256: str
    drive_revision_id: str
    native_source_version: str


@dataclass(frozen=True)
class SheetNativeBinding:
    sheet_case_id: str
    native_case_id: str
    kind: str
    native_snapshot_digest: str
    source_id: str
    sha256: str


def bind_case(sheet_case_id, canonical, native_digest):
    """Manager-owned alias -> stable native Case mapping, never rename a Case.

    Existing native case IDs are derived from their identity. Synthetic Sheet
    aliases cannot overwrite them or invent a Unit. This binding is draft-only.
    """
    check_row(canonical)
    kind = CASE_KINDS.get(sheet_case_id)
    if (kind not in KINDS or KINDS[kind] != canonical['kind'] or canonical['hold']
            or not isinstance(native_digest, str) or len(native_digest) != 64):
        deny('native_case_binding_invalid')
    i = canonical['identity']
    return SheetNativeBinding(sheet_case_id, canonical['case_id'], kind, native_digest,
                              i['source_file_id'], i['source_content_hash'])


def bind_versions(read_versions, sheet_identity, native_identity):
    """Fresh manager-side co-observation of two DISTINCT version namespaces.

    The injected read-only host reader must independently obtain the current
    revision ID and exact version used by the existing native source reader.
    No coercion, equal-string inference or Sheet-supplied aliases are allowed.
    No live reader/OAuth endpoint is constructed here.
    """
    first, second = read_versions(), read_versions()
    keys = {'source_id', 'sha256', 'drive_revision_id', 'native_source_version'}
    if (not isinstance(first, dict) or first != second or set(first) != keys
            or any(not isinstance(v, str) or not v for v in first.values())
            or sheet_identity.get('version_scheme') != 'drive_revision_id'
            or (first['source_id'], first['sha256'], first['drive_revision_id']) !=
               (sheet_identity['source_id'], sheet_identity['sha256'], sheet_identity['source_version'])
            or (first['source_id'], first['sha256'], first['native_source_version']) !=
               (native_identity['source_file_id'], native_identity['source_content_hash'], native_identity['source_version'])):
        deny('version_binding_missing_or_changed')
    return VersionBinding(**first)


def check_version_binding(binding, source, native):
    if (not isinstance(binding, VersionBinding) or source['version_scheme'] != 'drive_revision_id'
            or (binding.source_id, binding.sha256, binding.drive_revision_id) !=
               (source['source_id'], source['sha256'], source['source_version'])
            or (binding.source_id, binding.sha256, binding.native_source_version) !=
               (native['source_file_id'], native['source_content_hash'], native['source_version'])):
        deny('version_binding_missing_or_changed')


def day_value(value):
    if type(value) in (int, float):
        if value != int(value) or not 1 <= value <= 2958465:
            deny('date_serial_invalid')
        return (date(1899, 12, 30) + timedelta(days=int(value))).isoformat()
    return value


def field_inputs(draft):
    cells = draft['cells']
    if draft['kind'] == 'PDF':
        return dict(memo=raw_value(cells[12]))
    result = dict(date=day_value(raw_value(cells[4])), merchant=raw_value(cells[5]),
                  amount=raw_value(cells[6]), category=raw_value(cells[7]), memo=raw_value(cells[12]))
    if draft['kind'] == '医療':
        result['payment'] = raw_value(cells[8])
    return result


def preview_bound_draft(draft, expected_input_digest, context):
    """Context and saved digest come from the host/journal, never Sheet notes.

    context['canonical'] retains native page/Unit identity. General per-item
    categories and PDF selection -> native operation mapping remain native.
    """
    if (digest(draft['cells']) != draft['input_snapshot_digest']
            or draft['input_snapshot_digest'] != expected_input_digest):
        deny('native_handoff_snapshot_changed')
    if draft['held'] or raw_value(draft['cells'][3]) == '保留':
        return dict(status='held', authorized=False, operation_targets=[])
    if context is None:
        return dict(status='native_snapshot_required', authorized=False, operation_targets=[])
    canonical = context['canonical']
    source = draft['identity']; native = canonical['identity']
    binding = context.get('sheet_binding')
    expected_binding = bind_case(draft['case_id'], canonical, context['native_digest'])
    if (not isinstance(binding, SheetNativeBinding) or binding != expected_binding
            or KINDS.get(draft['kind']) != canonical['kind'] or binding.kind != draft['kind']):
        deny('native_handoff_identity_changed')
    check_version_binding(context.get('version_binding'), source, native)
    row = deepcopy(canonical)
    row['inputs'].update(field_inputs(draft))
    if draft['kind'] == 'PDF':
        if context['native_digest'] != digest(context['observation'].evidence):
            deny('native_handoff_snapshot_changed')
        selection = (raw_value(draft['cells'][10]), raw_value(draft['cells'][11]))
        if selection not in context['selections']:
            deny('native_structure_mapping_required')
        row['inputs']['partition'] = context['selections'][selection]
    # Cell notes are immutable metadata verified by raw reader, not native
    # page/Unit authority. The native canonical notes remain host-owned.
    expected = snapshot(row)
    if draft['kind'] == '医療':
        return preview_medical(row, canonical, expected, context['case'], context['native_digest'], context['port'])
    if draft['kind'] == '一般':
        return preview_general(row, canonical, expected, context['case'], context['native_digest'],
                               context['record'], context['accepted_snapshot'], context['accepted_digest'],
                               context['categories'])
    return preview_structure(row, canonical, expected, context['observation'], context['request_id'])


def medical_fields_only(draft, categories):
    """Reuse existing manual parser/category/result validator, no native grant.

    This does NOT establish native Unit identity, duplicate/replay clearance,
    plan validation or readiness to post. No AI inference or default inputs.
    """
    from .receipt_confirmation import ReceiptConfirmation
    from .receipt_validation import validate_receipt_result
    values = field_inputs(draft)
    service = ReceiptConfirmation(SimpleNamespace(value={}),
                                  SimpleNamespace(categories=lambda: deepcopy(categories)), None)
    item = dict(kind='medical', inputs=[values[k] for k in ('date', 'merchant', 'amount', 'category', 'payment')])
    try:
        parsed = service._parsed(item)
        ok, _ = validate_receipt_result(parsed, categories)
    except ValueError:
        ok = False
    return dict(status='manual_fields_validated_only' if ok else 'incomplete_or_invalid_manual_fields',
                native_snapshot_required=True, authorized=False, operation_targets=[])
