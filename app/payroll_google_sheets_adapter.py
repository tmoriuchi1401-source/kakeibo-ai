from __future__ import annotations

from typing import Iterable, Literal, Protocol

from googleapiclient.errors import HttpError
from pydantic import BaseModel, ConfigDict

from .payroll_sheets import SHEET_TITLES
from .payroll_storage import PAYROLL_ITEM_COLUMNS, PAYROLL_STATEMENT_COLUMNS
from .payroll_storage_preview import PayrollPlannedRow, PayrollWriteIdentity, PayrollWritePlan
from .payroll_writer import PayrollAppendOutcome, validate_payroll_write_contract


# These responses safely establish that the append was rejected. Timeout-like,
# concurrency, quota, and server statuses are deliberately excluded.
_CONFIRMED_NO_WRITE_HTTP_STATUSES = frozenset({
    400, 401, 403, 404, 405, 410, 413, 414, 415, 422,
})


class PayrollSingleAttemptRequestExecutor(Protocol):
    """Execute exactly one HTTP transport attempt.

    ``HttpRequest.execute(num_retries=0)`` alone does not satisfy this contract:
    the installed httplib2 transport has its own lower-level retry paths. A
    production implementation must bypass or disable those paths explicitly.
    """

    def execute_once(self, request): ...


class PayrollRequestNotSentError(RuntimeError):
    """The payroll write request was rejected before transport dispatch."""


class PayrollGoogleSheetsAppendAdapter:
    """Google Values append boundary with one HTTP attempt per method call.

    A service must be injected deliberately. This class never creates credentials,
    performs read-back, retries, updates, deletes, or repairs partial data.
    """

    def __init__(self, spreadsheet_id: str, *, service, executor):
        if not spreadsheet_id:
            raise ValueError("spreadsheet_id is required")
        if service is None:
            raise ValueError("service must be injected")
        if executor is None:
            raise ValueError("single-attempt executor must be injected")
        self.spreadsheet_id = spreadsheet_id
        self.service = service
        self.executor: PayrollSingleAttemptRequestExecutor = executor

    def append_header_rows(
        self, rows: tuple[PayrollPlannedRow, ...],
    ) -> PayrollAppendOutcome:
        return self._append("payroll_statements", rows, PAYROLL_STATEMENT_COLUMNS)

    def append_item_rows(
        self, rows: tuple[PayrollPlannedRow, ...],
    ) -> PayrollAppendOutcome:
        return self._append("payroll_items", rows, PAYROLL_ITEM_COLUMNS)

    def _append(
        self,
        sheet_key: str,
        rows: tuple[PayrollPlannedRow, ...],
        expected_columns: tuple[str, ...],
    ) -> PayrollAppendOutcome:
        requested_rows = len(rows)
        if not rows or any(row.columns != expected_columns for row in rows):
            return PayrollAppendOutcome(
                status="confirmed_failure",
                requested_rows=requested_rows,
                failure_kind="local_contract_rejection",
            )

        title = SHEET_TITLES[sheet_key]
        body = {"values": [list(row.values) for row in rows]}
        try:
            request = self.service.spreadsheets().values().append(
                spreadsheetId=self.spreadsheet_id,
                range=f"'{title}'!A:A",
                valueInputOption="RAW",
                insertDataOption="INSERT_ROWS",
                body=body,
            )
        except Exception as exc:
            # No request object was created, so no HTTP write was dispatched.
            return PayrollAppendOutcome(
                status="confirmed_failure",
                requested_rows=requested_rows,
                failure_kind="request_construction_failed",
                error_type=type(exc).__name__,
            )

        try:
            response = self.executor.execute_once(request)
        except PayrollRequestNotSentError as exc:
            return PayrollAppendOutcome(
                status="confirmed_failure",
                requested_rows=requested_rows,
                failure_kind="request_not_sent",
                error_type=type(exc).__name__,
            )
        except HttpError as exc:
            status = int(getattr(exc.resp, "status", 0) or 0)
            confirmed = status in _CONFIRMED_NO_WRITE_HTTP_STATUSES
            return PayrollAppendOutcome(
                status="confirmed_failure" if confirmed else "outcome_unknown",
                requested_rows=requested_rows,
                failure_kind=("http_request_rejected" if confirmed
                              else "http_outcome_unknown"),
                error_type=type(exc).__name__,
                http_status=status or None,
            )
        except Exception as exc:
            # Transport interruption can occur after the server commits the append.
            return PayrollAppendOutcome(
                status="outcome_unknown",
                requested_rows=requested_rows,
                failure_kind="transport_outcome_unknown",
                error_type=type(exc).__name__,
            )

        return self._map_success_response(
            response, title=title, requested_rows=requested_rows,
        )

    def _map_success_response(
        self,
        response,
        *,
        title: str,
        requested_rows: int,
    ) -> PayrollAppendOutcome:
        updates = response.get("updates", {}) if isinstance(response, dict) else {}
        updated_range = updates.get("updatedRange")
        target = (str(updated_range).split("!", 1)[0].strip("'")
                  if updated_range else None)
        confirmed = (
            isinstance(response, dict)
            and response.get("spreadsheetId") == self.spreadsheet_id
            and updates.get("updatedRows") == requested_rows
            and target == title
        )
        if not confirmed:
            return PayrollAppendOutcome(
                status="outcome_unknown",
                requested_rows=requested_rows,
                failure_kind="response_confirmation_mismatch",
                updated_range=updated_range,
            )
        return PayrollAppendOutcome(
            status="confirmed_success",
            requested_rows=requested_rows,
            confirmed_rows=requested_rows,
            updated_range=updated_range,
        )


class PayrollRecoveryReadAdapter(Protocol):
    """Read-only recovery boundary, deliberately separate from append."""

    def read_header_rows(
        self, statement_id: str,
    ) -> Iterable[PayrollPlannedRow]: ...

    def read_item_rows(
        self, statement_id: str,
    ) -> Iterable[PayrollPlannedRow]: ...


class PayrollRecoveryAssessment(BaseModel):
    model_config = ConfigDict(frozen=True)

    statement_id: str
    header_state: Literal["absent", "identity_confirmed", "conflict_or_duplicate"]
    item_state: Literal["absent", "complete", "incomplete_or_duplicate"]
    matching_header_count: int
    observed_item_count: int
    expected_item_count: int
    safe_to_automatic_retry: Literal[False] = False


def _identity_matches(row: PayrollPlannedRow, identity: PayrollWriteIdentity) -> bool:
    if row.columns != PAYROLL_STATEMENT_COLUMNS:
        return False
    values = row.as_dict()
    return all(values[field] == getattr(identity, field) for field in (
        "statement_id", "employer_id", "statement_type", "source_file_id",
        "content_hash", "pay_period",
    ))


def _same_rows(
    expected: tuple[PayrollPlannedRow, ...],
    observed: tuple[PayrollPlannedRow, ...],
) -> bool:
    remaining = list(observed)
    for row in expected:
        try:
            index = remaining.index(row)
        except ValueError:
            return False
        remaining.pop(index)
    return not remaining


def inspect_payroll_recovery(
    plan: PayrollWritePlan,
    reader: PayrollRecoveryReadAdapter,
) -> PayrollRecoveryAssessment:
    """Read back ambiguous state without writing, repairing, or authorizing retry."""
    validate_payroll_write_contract([plan])
    if plan.status != "ready":
        raise ValueError("recovery inspection requires the attempted ready plan")
    statement_id = plan.identity.statement_id
    headers = tuple(reader.read_header_rows(statement_id))
    items = tuple(reader.read_item_rows(statement_id))
    matching_headers = sum(
        _identity_matches(row, plan.identity) for row in headers
    )
    if not headers:
        header_state = "absent"
    elif len(headers) == 1 and matching_headers == 1:
        header_state = "identity_confirmed"
    else:
        header_state = "conflict_or_duplicate"

    if not items:
        item_state = "absent"
    elif _same_rows(plan.planned_item_rows, items):
        item_state = "complete"
    else:
        item_state = "incomplete_or_duplicate"
    return PayrollRecoveryAssessment(
        statement_id=statement_id,
        header_state=header_state,
        item_state=item_state,
        matching_header_count=matching_headers,
        observed_item_count=len(items),
        expected_item_count=len(plan.planned_item_rows),
    )
