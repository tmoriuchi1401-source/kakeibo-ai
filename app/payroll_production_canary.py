"""Explicit, single-statement Payroll production canary orchestration.

The caller keeps review reload, preview, the existing writer authority, and
post-write verification separate.  It never broadens a plan: exactly one
content-bound ready plan may reach the production append adapter.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
from typing import Callable, Iterable

from pydantic import BaseModel, ConfigDict, Field

from .google_clients import credentials
from .payroll_review_integration import (
    PayrollReviewReloadEvidence,
    PayrollReviewReloadRequest,
    reload_payroll_review_decisions,
)
from .payroll_sheets import PayrollSheetsReadRepository, SHEET_TITLES
from .payroll_single_attempt_executor import production_payroll_google_sheets_adapter
from .payroll_storage import (
    PAYROLL_ITEM_COLUMNS,
    PAYROLL_STATEMENT_COLUMNS,
    PayrollStorageCandidate,
)
from .payroll_storage_preview import (
    PayrollPlannedRow,
    PayrollWritePlan,
    build_write_plan,
    drive_storage_candidates,
)
from .payroll_writer import (
    PayrollAppendOutcome,
    PayrollWriteBatchResult,
    apply_payroll_write_plans,
    preview_payroll_write,
)


class PayrollCanaryError(RuntimeError):
    """Fail-closed canary precondition error raised before any write."""


class PayrollCanaryPreview(BaseModel):
    model_config = ConfigDict(frozen=True)

    spreadsheet_fingerprint: str
    header_sheet: str
    item_sheet: str
    header_schema_ok: bool
    item_schema_ok: bool
    column_mapping_ok: bool
    values_validated: bool
    employer_id_matches: bool
    statement_type_matches: bool
    pay_period_present: bool
    duplicate_status: str
    idempotency_key_hash: str
    plan_hash: str
    expected_header_range: str
    expected_item_range: str
    review_before: int
    review_after: int
    persisted_decisions: int
    applied_decisions: int
    rejected_decisions: int
    status: str
    reason: str
    header_rows: int
    item_rows: int
    existing_header_matches: int
    existing_item_matches: int
    plan: PayrollWritePlan = Field(exclude=True, repr=False)
    candidate: PayrollStorageCandidate = Field(exclude=True, repr=False)
    raw_candidate: PayrollStorageCandidate = Field(exclude=True, repr=False)


class PayrollCanaryApplyReport(BaseModel):
    model_config = ConfigDict(frozen=True)

    preview: PayrollCanaryPreview
    writer_status: str
    writer_applied: bool
    actual_header_rows: int
    actual_item_rows: int
    actual_header_range: str | None = None
    actual_item_range: str | None = None
    post_read_header_matches: int
    post_read_item_matches: int
    post_read_exact: bool
    total_header_row_delta: int
    total_item_row_delta: int
    unexpected_changes: bool
    partial_success: bool


def _review_count(candidate) -> int:
    return sum(item.needs_review or item.review_status == "pending"
               for item in candidate.items)


def _plan_hash(
    plan: PayrollWritePlan, *, header_range: str, item_range: str,
) -> str:
    """Bind semantic values and target ranges while excluding generated row IDs."""
    header = plan.planned_header_rows[0].as_dict()
    stable_header = {
        key: value for key, value in header.items()
        if key not in {"statement_id", "imported_at"}
    }
    stable_items = []
    for row in plan.planned_item_rows:
        values = row.as_dict()
        stable_items.append({
            key: value for key, value in values.items()
            if key not in {"item_id", "statement_id"}
        })
    stable_identity = plan.identity.model_dump(mode="json")
    stable_identity.pop("statement_id", None)
    payload = json.dumps(
        {
            "identity": stable_identity,
            "duplicate": plan.duplicate.model_dump(mode="json"),
            "header": stable_header,
            "items": stable_items,
            "header_range": header_range,
            "item_range": item_range,
        },
        ensure_ascii=False,
        sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _spreadsheet_fingerprint(spreadsheet_id: str) -> str:
    return hashlib.sha256(spreadsheet_id.encode("utf-8")).hexdigest()[:16]


def _append_range(title: str, first_row: int, row_count: int, width: int) -> str:
    def column_name(number: int) -> str:
        result = ""
        while number:
            number, remainder = divmod(number - 1, 26)
            result = chr(65 + remainder) + result
        return result
    last_row = first_row + row_count - 1
    return f"'{title}'!A{first_row}:{column_name(width)}{last_row}"


def _sheet_cell_matches(actual, expected) -> bool:
    """Compare a Sheets formatted value with the value sent using RAW input."""
    if actual == expected:
        return True
    if isinstance(expected, bool):
        return isinstance(actual, str) and actual.upper() == str(expected).upper()
    if isinstance(expected, (int, float)) and not isinstance(expected, bool):
        if not isinstance(actual, str):
            return False
        try:
            return Decimal(actual) == Decimal(str(expected))
        except InvalidOperation:
            return False
    return False


def _row_matches(actual: PayrollPlannedRow, expected: PayrollPlannedRow) -> bool:
    return (
        actual.columns == expected.columns
        and len(actual.values) == len(expected.values)
        and all(
            _sheet_cell_matches(actual_value, expected_value)
            for actual_value, expected_value in zip(actual.values, expected.values)
        )
    )


def _exact_rows(rows: Iterable[PayrollPlannedRow], expected) -> int:
    return sum(
        any(_row_matches(row, expected_row) for expected_row in expected)
        for row in rows
    )


def _read_planned_rows(reader, sheet_key: str, statement_id: str):
    if sheet_key == "payroll_statements":
        columns = PAYROLL_STATEMENT_COLUMNS
        identity_index = 0
    elif sheet_key == "payroll_items":
        columns = PAYROLL_ITEM_COLUMNS
        identity_index = 1
    else:
        raise ValueError("payroll statement or item sheet required")
    result = []
    for raw_row in reader.data_rows(sheet_key):
        if len(raw_row) <= identity_index or raw_row[identity_index] != statement_id:
            continue
        values = [None if value == "" else value for value in raw_row]
        values += [None] * max(0, len(columns) - len(values))
        result.append(PayrollPlannedRow(
            columns=columns, values=tuple(values[:len(columns)]),
        ))
    return tuple(result)


def _same_a1_range(actual: str | None, expected: str) -> bool:
    return bool(actual) and actual.replace("'", "") == expected.replace("'", "")


def build_payroll_canary_preview(
    *,
    spreadsheet_id: str,
    snapshot,
    raw_candidates,
    expected_content_hash: str,
    journal_path: str | Path,
    hmac_key_path: str | Path,
    repository_root: str | Path,
    expected_employer_id: str,
    expected_statement_type: str,
    reader: PayrollSheetsReadRepository,
) -> PayrollCanaryPreview:
    """Build a privacy-safe preview for one exact source without writing."""
    selected = [candidate for candidate in raw_candidates
                if candidate.statement.content_hash == expected_content_hash]
    if len(selected) != 1:
        raise PayrollCanaryError("payroll_canary_exact_source_required")
    raw_candidate = selected[0]
    request = PayrollReviewReloadRequest(
        expected_content_hash=expected_content_hash,
        journal_path=journal_path,
        hmac_key_path=hmac_key_path,
        repository_root=repository_root,
    )
    reloaded = reload_payroll_review_decisions(
        raw_candidate, snapshot, request, enabled=True,
    )
    evidence: PayrollReviewReloadEvidence = reloaded.evidence
    if evidence.status != "applied" or evidence.rejected_decision_count:
        raise PayrollCanaryError(evidence.reason_code)
    plan = build_write_plan([reloaded.candidate], snapshot)[0]
    writer_preview = preview_payroll_write([plan])
    header_schema = next(
        value for value in snapshot.schemas
        if value.sheet_key == "payroll_statements"
    )
    item_schema = next(
        value for value in snapshot.schemas
        if value.sheet_key == "payroll_items"
    )
    stored_headers = _read_planned_rows(
        reader, "payroll_statements", plan.identity.statement_id,
    )
    stored_items = _read_planned_rows(
        reader, "payroll_items", plan.identity.statement_id,
    )
    all_header_count = reader.data_row_count("payroll_statements")
    all_item_count = reader.data_row_count("payroll_items")
    column_mapping_ok = (
        all(row.columns == PAYROLL_STATEMENT_COLUMNS
            for row in plan.planned_header_rows)
        and all(row.columns == PAYROLL_ITEM_COLUMNS
                for row in plan.planned_item_rows)
    )
    values_validated = all(
        len(row.columns) == len(row.values)
        for row in (*plan.planned_header_rows, *plan.planned_item_rows)
    )
    safe = (
        header_schema.schema_ok and item_schema.schema_ok
        and column_mapping_ok and values_validated
        and plan.identity.employer_id == expected_employer_id
        and plan.identity.statement_type == expected_statement_type
        and bool(plan.identity.pay_period)
        and plan.duplicate.status == "new"
        and not stored_headers and not stored_items
        and plan.status == "ready"
        and writer_preview.plan_count == writer_preview.ready_count == 1
        and writer_preview.header_rows == 1
        and writer_preview.item_rows > 0
    )
    if not safe:
        raise PayrollCanaryError("payroll_canary_preview_not_safe")
    expected_header_range = _append_range(
        SHEET_TITLES["payroll_statements"], all_header_count + 2,
        1, len(PAYROLL_STATEMENT_COLUMNS),
    )
    expected_item_range = _append_range(
        SHEET_TITLES["payroll_items"], all_item_count + 2,
        len(plan.planned_item_rows), len(PAYROLL_ITEM_COLUMNS),
    )
    return PayrollCanaryPreview(
        spreadsheet_fingerprint=_spreadsheet_fingerprint(spreadsheet_id),
        header_sheet=SHEET_TITLES["payroll_statements"],
        item_sheet=SHEET_TITLES["payroll_items"],
        header_schema_ok=header_schema.schema_ok,
        item_schema_ok=item_schema.schema_ok,
        column_mapping_ok=column_mapping_ok,
        values_validated=values_validated,
        employer_id_matches=plan.identity.employer_id == expected_employer_id,
        statement_type_matches=(
            plan.identity.statement_type == expected_statement_type
        ),
        pay_period_present=bool(plan.identity.pay_period),
        duplicate_status=plan.duplicate.status,
        idempotency_key_hash=hashlib.sha256(
            ("payroll-content:" + expected_content_hash).encode("ascii")
        ).hexdigest(),
        plan_hash=_plan_hash(
            plan, header_range=expected_header_range,
            item_range=expected_item_range,
        ),
        expected_header_range=expected_header_range,
        expected_item_range=expected_item_range,
        review_before=_review_count(raw_candidate),
        review_after=_review_count(reloaded.candidate),
        persisted_decisions=evidence.persisted_decision_count,
        applied_decisions=evidence.applied_decision_count,
        rejected_decisions=evidence.rejected_decision_count,
        status=plan.status,
        reason=plan.reason,
        header_rows=len(plan.planned_header_rows),
        item_rows=len(plan.planned_item_rows),
        existing_header_matches=len(stored_headers),
        existing_item_matches=len(stored_items),
        plan=plan,
        candidate=reloaded.candidate,
        raw_candidate=raw_candidate,
    )


def rebuild_latest_payroll_canary_plan(
    preview: PayrollCanaryPreview, snapshot, *, journal_path: str | Path,
    hmac_key_path: str | Path, repository_root: str | Path,
) -> PayrollWritePlan:
    """Revalidate the signed journal and rebuild against the freshest Sheets state."""
    request = PayrollReviewReloadRequest(
        expected_content_hash=preview.plan.identity.content_hash,
        journal_path=journal_path, hmac_key_path=hmac_key_path,
        repository_root=repository_root,
    )
    reloaded = reload_payroll_review_decisions(
        preview.raw_candidate, snapshot, request, enabled=True,
    )
    if (
        reloaded.evidence.status != "applied"
        or reloaded.evidence.applied_decision_count != preview.applied_decisions
        or reloaded.evidence.rejected_decision_count
    ):
        raise PayrollCanaryError("payroll_canary_latest_journal_rejected")
    return build_write_plan([reloaded.candidate], snapshot)[0]


@dataclass
class _RecordingAdapter:
    target: object
    header_outcome: PayrollAppendOutcome | None = None
    item_outcome: PayrollAppendOutcome | None = None

    def append_header_rows(self, rows):
        self.header_outcome = self.target.append_header_rows(rows)
        return self.header_outcome

    def append_item_rows(self, rows):
        self.item_outcome = self.target.append_item_rows(rows)
        return self.item_outcome


def apply_payroll_canary(
    preview: PayrollCanaryPreview,
    *,
    expected_plan_hash: str,
    reader: PayrollSheetsReadRepository,
    latest_plan: Callable[[], PayrollWritePlan],
    writer=None,
    confirmed: bool,
) -> PayrollCanaryApplyReport:
    """Apply the previewed exact plan once, then always perform post-read."""
    if not confirmed or expected_plan_hash != preview.plan_hash:
        raise PayrollCanaryError("payroll_canary_explicit_plan_confirmation_required")
    plan = preview.plan
    before_header_count = reader.data_row_count("payroll_statements")
    before_item_count = reader.data_row_count("payroll_items")
    if _read_planned_rows(
        reader, "payroll_statements", plan.identity.statement_id,
    ) or _read_planned_rows(
        reader, "payroll_items", plan.identity.statement_id,
    ):
        raise PayrollCanaryError("payroll_canary_duplicate_pre_read")
    recording = _RecordingAdapter(writer)
    result: PayrollWriteBatchResult = apply_payroll_write_plans(
        [plan], recording, confirmed=True, latest_plans=lambda: [latest_plan()],
    )
    post_headers = _read_planned_rows(
        reader, "payroll_statements", plan.identity.statement_id,
    )
    post_items = _read_planned_rows(
        reader, "payroll_items", plan.identity.statement_id,
    )
    after_header_count = reader.data_row_count("payroll_statements")
    after_item_count = reader.data_row_count("payroll_items")
    header_matches = _exact_rows(post_headers, plan.planned_header_rows)
    item_matches = _exact_rows(post_items, plan.planned_item_rows)
    exact = (
        len(post_headers) == len(plan.planned_header_rows)
        and len(post_items) == len(plan.planned_item_rows)
        and header_matches == len(plan.planned_header_rows)
        and item_matches == len(plan.planned_item_rows)
    )
    header_delta = after_header_count - before_header_count
    item_delta = after_item_count - before_item_count
    expected_success = result.status == "completed" and result.applied
    ranges_exact = (
        recording.header_outcome is not None
        and recording.item_outcome is not None
        and _same_a1_range(
            recording.header_outcome.updated_range, preview.expected_header_range,
        )
        and _same_a1_range(
            recording.item_outcome.updated_range, preview.expected_item_range,
        )
    )
    unexpected = (
        not exact or not ranges_exact
        or header_delta != len(plan.planned_header_rows)
        or item_delta != len(plan.planned_item_rows)
    ) if expected_success else bool(header_delta or item_delta)
    return PayrollCanaryApplyReport(
        preview=preview,
        writer_status=result.status,
        writer_applied=result.applied,
        actual_header_rows=(recording.header_outcome.confirmed_rows
                            if recording.header_outcome else 0),
        actual_item_rows=(recording.item_outcome.confirmed_rows
                          if recording.item_outcome else 0),
        actual_header_range=(recording.header_outcome.updated_range
                             if recording.header_outcome else None),
        actual_item_range=(recording.item_outcome.updated_range
                           if recording.item_outcome else None),
        post_read_header_matches=header_matches,
        post_read_item_matches=item_matches,
        post_read_exact=exact,
        total_header_row_delta=header_delta,
        total_item_row_delta=item_delta,
        unexpected_changes=unexpected,
        partial_success=result.status in {"partial_failure", "header_outcome_unknown"},
    )


def load_production_canary_preview(
    *, spreadsheet_id: str, folder_id: str, expected_content_hash: str,
    journal_path: str | Path, hmac_key_path: str | Path,
    repository_root: str | Path, employer_id: str, statement_type: str,
    reader=None,
) -> PayrollCanaryPreview:
    """Use production read-only repositories to produce one canary preview."""
    reader = reader or PayrollSheetsReadRepository(spreadsheet_id)
    snapshot = reader.snapshot()
    candidates = drive_storage_candidates(
        folder_id, snapshot, employer_id=employer_id,
        statement_type=statement_type,
    )
    return build_payroll_canary_preview(
        spreadsheet_id=spreadsheet_id, snapshot=snapshot,
        raw_candidates=candidates, expected_content_hash=expected_content_hash,
        journal_path=journal_path, hmac_key_path=hmac_key_path,
        repository_root=repository_root, expected_employer_id=employer_id,
        expected_statement_type=statement_type, reader=reader,
    )


def production_canary_writer(spreadsheet_id: str):
    return production_payroll_google_sheets_adapter(
        spreadsheet_id, credentials=credentials(),
    )
