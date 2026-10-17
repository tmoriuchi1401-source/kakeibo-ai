"""Stage 1 read-only review projection. No Sheets/Drive/AI/writer clients.

Legacy references are retained separately until an exact Unit mapping exists.
This module cannot accept a decision, grant authority, or move an original.
"""
from copy import deepcopy
from dataclasses import dataclass, field
from hashlib import sha256
import json

from .drive_run_state import StateError

STATES = ('入力が必要', '原本確認が必要', '本人確認が必要', '処理待ち', '保留・再確認', '完了')
PDF_SCHEMAS = {'pdf-page-review-v1', 'general-receipt-item-review-v1', 'pdf-intake-page-review-v1'}
TERMINAL = {'applied', 'closed_user', 'closed_machine', 'imported', 'manual_imported',
            'medical_manual_imported', 'reconciled_existing', 'duplicate_confirmed', 'intentionally_skipped'}
LEGACY_UNMAPPED_HOLD = 'LEGACY_UNMAPPED_HOLD'
LEGACY_HOLD_LABEL = '旧案件・対応確認保留'
LEGACY_HOLD_REASON = '旧確認と新Receipt Unitの対応を証明できないため自動処理不可'


def digest(value):
    return sha256(json.dumps(value, ensure_ascii=True, sort_keys=True,
                             separators=(',', ':')).encode()).hexdigest()


@dataclass
class Case:
    legacy_id: str
    schema: str
    identity: dict
    fields: list
    origin: str
    kind: str
    authority_status: str = ''
    issues: list = field(default_factory=list)
    history: bool = False
    synthetic: bool = False
    # Evidence must come from the existing validator, not a status cell.
    verified_terminal: bool = False
    accepted: str = ''
    last_processed_at: str = ''
    unfinished_reason: str = ''
    summary: dict = field(default_factory=dict)
    disposition: str = ''
    audit_findings: list = field(default_factory=list)
    related_to: str = ''

    @property
    def restrictions(self):
        # The prototype has no mutation capability for any case. Explicitly
        # expose every forbidden legacy capability for the audit view.
        return dict(operation=False,accounting_write=False,ai_send=False,
                    authority_issue=False,authority_transfer=False,source_move=False,automatic_retry=False)

    @property
    def case_id(self):
        return 'case-' + digest([self.schema, self.legacy_id, self.identity])[:24]

    def value(self, marker, default=''):
        found = [r[2] for r in self.fields if r[0] == marker]
        if len(found) > 1:
            raise StateError('unified_review_duplicate_field')
        return found[0] if found else default

    @property
    def source_id(self):
        return self.identity.get('source_file_id', '')

    @property
    def pages(self):
        return self.identity.get('page_numbers', [self.identity['page_number']]
                                 if 'page_number' in self.identity else [])

    @property
    def unit_key(self):
        """Never use file ID, page ordinal, or an amount as a Unit join key."""
        i = self.identity
        page = i.get('stable_page_identity', i.get('page_identity'))
        if i.get('stable_page_identity') and i.get('page_identity') and i['stable_page_identity'] != i['page_identity']:
            return None
        required = ('source_file_id', 'source_content_hash', 'receipt_unit_id', 'receipt_index')
        if not page or not all(i.get(k) for k in required):
            return None
        return tuple(json.dumps(v, sort_keys=True) for v in (i['source_file_id'], i['source_content_hash'], page, i['receipt_unit_id'], i['receipt_index']))

    def display(self):
        if self.disposition == LEGACY_UNMAPPED_HOLD:
            return '保留・再確認', LEGACY_HOLD_REASON
        if self.issues or self.authority_status in {'held', 'superseded', 'needs_review', 'privacy_observation_changed'}:
            return '保留・再確認', '対応・原本・入力を再確認してください'
        terminal_kind = self.kind not in {'intake', 'grouping', 'page_kind'}
        if self.kind in {'medical', 'receipt_reconciliation'} and self.authority_status in {'closed_user', 'closed_machine'}:
            terminal_kind = False
        if self.verified_terminal and self.authority_status in TERMINAL and terminal_kind:
            return '完了', '必要な記帳・読戻し・原本状態を確認済み'
        if self.accepted or self.authority_status in TERMINAL | {'pending', 'claimed', 'posting_intent', 'grouping_confirmed'}:
            return '処理待ち', self.unfinished_reason or '後続処理と読戻し・原本状態の確認が残っています'
        if self.kind in {'receipt_reconciliation', 'page_kind', 'grouping', 'intake', 'page_privacy'}:
            return '原本確認が必要', '原本・既存記帳・対象範囲を確認してください'
        if self.kind == 'medical':
            return '入力が必要', '照合を先に確認し、別支出の場合だけ日付・施設・実支払額・カテゴリを完全手入力'
        if self.value('action') == '記帳する' and self.value('structure_confirmation') == '確認済み':
            return '本人確認が必要', 'この案件の入力検証と本人確認の受付結果を照合します'
        return '入力が必要', '原本と既存値・読取候補・明細差・カテゴリを確認してください'


def cell_rows(snapshot, title):
    """Convert bounded connector CellData to immutable-value input rows."""
    sheets = [s for s in snapshot['sheets'] if s['properties']['title'] == title]
    if len(sheets) != 1:
        raise StateError('unified_review_sheet_missing')
    result = []
    for grid in sheets[0].get('data', []):
        start = grid.get('startRow', 0)
        for offset, row in enumerate(grid.get('rowData', [])):
            while len(result) <= start + offset:
                result.append([])
            values = []
            for cell in row.get('values', []):
                raw = cell.get('userEnteredValue', {})
                values.append(next((raw[k] for k in ('stringValue', 'numberValue', 'boolValue', 'formulaValue') if k in raw), ''))
            result[start + offset] = values
    return result


def receipt_cases(rows, items):
    from .receipt_confirmation import HEADERS
    if not rows or rows[0][:len(HEADERS)] != HEADERS:
        raise StateError('unified_review_receipt_header_changed')
    result, seen = [], set()
    markers = ['date', 'merchant', 'amount', 'category', 'payment', 'action', 'duplicate_target', 'memo']
    for raw in rows[1:]:
        if not any(x != '' for x in raw):
            continue
        row = list(raw) + [''] * max(0, 16-len(raw))
        key = row[0]
        if not key or key in seen:
            raise StateError('unified_review_receipt_identity_ambiguous')
        seen.add(key)
        item = items.get(key)
        if item is None:
            raise StateError('unified_review_receipt_authority_missing')
        source = item['source']
        identity = {'source_file_id': source['source_id'], 'source_content_hash': source['sha256'],
                    'source_version': source['version'], 'review_purpose': item['kind'], 'review_id': key}
        fields = [[f, HEADERS[n], row[n]] for f, n in zip(markers, range(7, 15))]
        # Medical legacy OCR/AI suggestions are never carried into the new view.
        fields += [['original', HEADERS[3], row[3]], ['existing', HEADERS[5], row[5]],
                   ['candidate', HEADERS[6], row[6] if item['kind'] == 'normal' else ''],
                   ['legacy_state', HEADERS[2], row[2]], ['result', HEADERS[15], row[15]]]
        history = item['status'] == 'superseded'
        case = Case(key, 'receipt-confirmation-v1', identity, fields, '領収書確認', item['kind'],
                    authority_status=item['status'], history=history)
        existing = item.get('before', {}).get('receipt_rows', [])
        if item['kind'] == 'normal' and len(existing) == 1 and len(existing[0]) >= 4:
            case.summary = dict(date=existing[0][1],merchant=existing[0][2],amount=existing[0][3])
        if history:
            case.issues.append('旧版：本人判断・入力を保持。自動統合しません')
        if input_differences(row[7:15], item.get('inputs', [])):
            case.issues.append('未送信入力あり：authorityに未反映のセルを保持')
        result.append(case)
    return result


def input_differences(live, saved):
    """Display-only comparison across Sheets date/number render options.

    Raw cells and authority snapshots stay untouched. Invalid values compare
    exactly; this must never be used as a writer's confirmation digest.
    """
    from .receipt_reimport import _date, _money
    if len(live) != 8 or len(saved) != 8:
        return list(range(8))
    different = []
    for n, (a, b) in enumerate(zip(live, saved)):
        normalize = _date if n == 0 else _money if n == 2 else None
        if a == b:
            continue
        if normalize and normalize(a) is not None and normalize(a) == normalize(b):
            continue
        different.append(n)
    return different


def pdf_cases(rows, items):
    grouped = {}
    for row in rows:
        if row == ['項目', '内容', 'pdf-page-review-v1']:
            continue
        if len(row) > 5 and row[2] == 'pdf-page-review-v1':
            schema, token, raw, marker = row[2:6]
        elif len(row) > 20 and row[17] in PDF_SCHEMAS:
            schema, token, raw, marker = row[17:21]
        else:
            if any(v in PDF_SCHEMAS for v in row):
                raise StateError('unified_review_pdf_marker_incomplete')
            continue
        try:
            identity = json.loads(raw)
            if not isinstance(identity, dict) or not token or not marker:
                raise ValueError()
        except (ValueError, TypeError):
            raise StateError('unified_review_pdf_identity_invalid') from None
        key = schema, token
        if key not in grouped:
            source = identity.get('source', {}).get('source_file_id', identity.get('source_file_id', ''))
            ident = deepcopy(identity)
            ident.setdefault('source_file_id', source)
            if 'source' in ident:
                ident.setdefault('source_content_hash', ident['source'].get('source_content_hash', ''))
            kind = identity.get('kind', 'page_privacy')
            ident['review_purpose'] = kind
            grouped[key] = Case(token, schema, ident, [], 'PDFページ確認', kind,
                                synthetic=source.startswith('synthetic-'))
        case = grouped[key]
        if {k:v for k,v in case.identity.items() if k != 'review_purpose'} != {
                **identity, **({ 'source_file_id': case.source_id,
                'source_content_hash': case.identity.get('source_content_hash', '')} if 'source' in identity else {})}:
            raise StateError('unified_review_pdf_card_identity_changed')
        if any(r[0] == marker for r in case.fields):
            raise StateError('unified_review_duplicate_field')
        case.fields.append([marker, row[0], deepcopy(row[1])])
        if marker.startswith('item:'):
            if len(row) <= 21 or row[21] != marker[5:]:
                raise StateError('unified_review_pdf_item_identity_changed')
            case.fields.append(['category:'+marker[5:], 'カテゴリ', deepcopy(row[16])])
    for case in grouped.values():
        review = case.identity.get('review_id')
        if review and review not in items:
            case.issues.append('旧確認IDが現在のauthorityに存在しません')
        if case.kind in {'medical', 'receipt_reconciliation', 'general_receipt_item_review'} and case.unit_key is None:
            case.issues.append('原本版・page identity・Receipt Unit・receipt indexの対応が未確定')
        if (case.schema == 'pdf-page-review-v1' and case.kind == 'medical'
                and (not review or review not in items or case.unit_key is None)):
            case.disposition = LEGACY_UNMAPPED_HOLD
            case.audit_findings = [
                ['mapping_result','一意対応','対応不能：欠けたidentityを類似値で補いません'],
                ['authority_reference','旧確認IDのnative authority','存在未確認' if review not in items else 'Unit対応未確定'],
                ['unit_mapping','Receipt Unit / receipt index','未確定。発行・移植・再記帳を禁止'],
            ]
    return list(grouped.values())


def project(receipt_rows, pdf_rows, items):
    """Offline Stage 1: each legacy purpose remains an independently traced case."""
    cases = receipt_cases(deepcopy(receipt_rows), deepcopy(items)) + pdf_cases(deepcopy(pdf_rows), deepcopy(items))
    if len({c.case_id for c in cases}) != len(cases):
        raise StateError('unified_review_case_id_collision')
    # A page-level reconciliation without an exact Unit mapping blocks every
    # possible medical new-posting request on that same immutable page.
    recon = [c for c in cases if c.kind == 'receipt_reconciliation']
    for case in cases:
        if case.kind != 'medical':
            continue
        matches = [c for c in recon if c.source_id == case.source_id and
                   c.identity.get('source_content_hash') == case.identity.get('source_content_hash') and
                   set(c.pages).intersection(case.pages)]
        if matches:
            case.issues.append('既存記帳照合を先に解決。同一なら新規医療記帳不要')
    # No operation target in Stage 1; no person confirmation is reused.
    return cases


def relate_legacy_references(cases, relations):
    """Display explicit audit references together without merging their identity.

    Relations must name exact case IDs observed by the auditor. No matching by
    file/page/date/facility/amount and no authority or snapshot propagation.
    """
    by_id={c.case_id:c for c in cases}; seen=set()
    for relation in relations:
        parent=by_id.get(relation.get('primary_case_id'))
        child=by_id.get(relation.get('related_case_id'))
        if (parent is None or child is None or parent is child or child.case_id in seen
                or parent.disposition != LEGACY_UNMAPPED_HOLD
                or child.kind != 'receipt_reconciliation'
                or relation.get('provenance') != 'explicit_audit_reference_only'):
            raise StateError('unified_legacy_relation_invalid')
        seen.add(child.case_id);child.related_to=parent.case_id
    return cases


def annotate_posting_receipts(cases, pdf_rows, entries):
    """Bind optional native posting receipts without declaring source complete.

    Entries contain a downloaded candidate and its native financial receipt.
    The existing snapshot reader and state validator run locally. No status
    cell, page ordinal, or legacy review ID can substitute for their digests.
    """
    from datetime import datetime, timezone
    from .receipt_item_review_ui import read_snapshot
    from .pdf_review_fields import original_uri
    from .receipt_plan.posting import validate_state as p14_validate
    from .receipt_plan.unit_posting import validate_state as unit_validate
    seen = set()
    for candidate, receipt in entries:
        view = candidate['view']; ident = view['identity']; grant = receipt['grant']
        matches = [c for c in cases if c.schema == 'general-receipt-item-review-v1'
                   and c.legacy_id == view['token']]
        if len(matches) != 1 or matches[0].case_id in seen:
            raise StateError('unified_review_posting_mapping_ambiguous')
        case = matches[0]; seen.add(case.case_id)
        try:
            # Native schema validators reject malformed/history-inconsistent receipts.
            (p14_validate if receipt['schema'] == 'p14-explicit-posting-v1' else unit_validate)(receipt)
            if digest(ident) != grant['identity_digest']:
                raise StateError('unified_review_posting_identity_changed')
            current = read_snapshot(pdf_rows, view, original_uri(ident['source_file_id'],ident['page_number']))
            if digest(current) != grant['snapshot_digest']:
                raise StateError('unified_review_posting_snapshot_changed')
            if receipt['state'] != 'complete' or receipt['current']['receipt_unit_id'] != ident['receipt_unit_id']:
                raise StateError('unified_review_posting_not_complete')
        except (StateError, KeyError, ValueError, TypeError):
            case.issues.append('記帳証跡と現在の入力・identityが一致しません。操作停止')
            continue
        case.authority_status = 'imported'
        case.accepted = 'このレシートの記帳処理（保存済み証跡）'
        case.last_processed_at = datetime.fromtimestamp(max(
            event['confirmed_at'] for events in receipt['history'].values() for event in events.values()), timezone.utc).isoformat()
        case.unfinished_reason = '記帳証跡は一致。現在の会計read-back・全ページ/全Unitの終端・原本移動条件の照合が残っています'
        # A native receipt alone never proves the complete source/move/readback.
        case.verified_terminal = False
    return cases


def inventory(cases):
    return {'cases':len(cases), 'legacy_receipt_rows':sum(c.origin=='領収書確認' for c in cases),
            'legacy_pdf_cards':sum(c.origin=='PDFページ確認' for c in cases),
            'history':sum(c.history for c in cases), 'synthetic':sum(c.synthetic for c in cases),
            'blocked':sum(bool(c.issues) for c in cases),
            'legacy_unmapped_holds':sum(c.disposition==LEGACY_UNMAPPED_HOLD for c in cases),
            'related_references':sum(bool(c.related_to) for c in cases),
            'states':{s:sum(c.display()[0]==s for c in cases) for s in STATES},
            'operation_targets':0, 'accounting_writes':0, 'source_moves':0, 'ai_sends':0}
