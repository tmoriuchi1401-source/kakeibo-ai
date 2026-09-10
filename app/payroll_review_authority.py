"""Explicit, replay-protected business authority for Payroll review items.

This module never derives a decision.  It validates an operator assertion
against the exact source, parser, schema, item occurrence, and raw value that
were previewed.  Applying an accepted assertion only returns a copied storage
candidate; it performs no I/O and never calls a writer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import hmac
import json
import re
from typing import Literal

from .payroll_sheets import PayrollSheetsSnapshot
from .payroll_storage import (
    PayrollStorageCandidate,
    sync_statement_review_reasons,
)


REVIEW_AUTHORITY_VERSION = "payroll-review-authority-v1"
ReviewDecision = Literal["confirm_existing_value", "exclude_non_item"]


def _require_key(local_key: bytes) -> None:
    if not isinstance(local_key, bytes) or len(local_key) < 32:
        raise ValueError("local_key_too_short")


def _canonical(payload) -> bytes:
    return json.dumps(
        payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"),
    ).encode()


def _mac(local_key: bytes, payload) -> str:
    return hmac.new(local_key, _canonical(payload), hashlib.sha256).hexdigest()


def payroll_review_schema_fingerprint(snapshot: PayrollSheetsSnapshot) -> str:
    """Bind decisions to the exact active interpretation schema."""

    payload = {
        "schema_ok": snapshot.schema_ok,
        "standard_items": sorted(
            (item.standard_item_id, item.standard_name, item.section,
             item.value_type, item.active)
            for item in snapshot.standard_items
        ),
        "aliases": sorted(
            (item.alias_id, item.raw_item_name, item.standard_item_id,
             item.employer_id, item.active)
            for item in snapshot.aliases
        ),
        "employers": sorted(
            (item.employer_id, item.employer_label, item.active,
             str(item.start_date), str(item.end_date))
            for item in snapshot.employers
        ),
    }
    return hashlib.sha256(_canonical(payload)).hexdigest()


@dataclass(frozen=True)
class PayrollReviewEvidence:
    contract_version: str
    source_file_id_digest: str = field(repr=False)
    content_hash: str = field(repr=False)
    source_type: str
    employer_id: str = field(repr=False)
    statement_type: str
    pay_period: str | None
    parse_status: str
    parser_version: str
    schema_fingerprint: str
    item_occurrence: int
    raw_label_digest: str = field(repr=False)
    raw_value_digest: str = field(repr=False)
    current_standard_item_id: str | None
    section: str
    review_reason: str | None
    evidence_id: str


@dataclass(frozen=True)
class PayrollReviewAssertion:
    contract_version: str
    assertion_revision: int
    evidence_id: str
    decision: ReviewDecision
    operator_id: str = field(repr=False)
    standard_item_id: str | None
    reviewed_value: int | float | str | None
    signature: str = field(repr=False)


@dataclass(frozen=True)
class PayrollReviewEvaluation:
    accepted: bool
    reason_code: str
    evidence_id: str | None
    decision: ReviewDecision | None


@dataclass(frozen=True)
class PayrollReviewApplyResult:
    candidate: PayrollStorageCandidate = field(repr=False)
    evaluation: PayrollReviewEvaluation
    applied: bool


def _evidence_payload(candidate, snapshot, item_occurrence, local_key):
    statement = candidate.statement
    if not snapshot.schema_ok:
        raise ValueError("schema_authority_incomplete")
    if not statement.source_file_id or not statement.content_hash:
        raise ValueError("source_binding_incomplete")
    if not statement.employer_id or not statement.statement_type:
        raise ValueError("business_scope_incomplete")
    if sum(value.active and value.employer_id == statement.employer_id
           for value in snapshot.employers) != 1:
        raise ValueError("employer_authority_missing_or_ambiguous")
    if not statement.parser_version:
        raise ValueError("parser_provenance_incomplete")
    if not 0 <= item_occurrence < len(candidate.items):
        raise IndexError("item_occurrence_out_of_range")
    item = candidate.items[item_occurrence]
    schema_fingerprint = payroll_review_schema_fingerprint(snapshot)
    return {
        "contract_version": REVIEW_AUTHORITY_VERSION,
        "source_file_id_digest": _mac(
            local_key, ("source_file_id", statement.source_file_id),
        ),
        "content_hash": statement.content_hash,
        "source_type": statement.source_type,
        "employer_id": statement.employer_id,
        "statement_type": statement.statement_type,
        "pay_period": statement.pay_period,
        "parse_status": statement.parse_status,
        "parser_version": statement.parser_version,
        "schema_fingerprint": schema_fingerprint,
        "item_occurrence": item_occurrence,
        "raw_label_digest": _mac(local_key, ("raw_label", item.raw_item_name)),
        "raw_value_digest": _mac(local_key, ("raw_value", item.raw_value)),
        "current_standard_item_id": item.standard_item_id,
        "section": item.section,
        "review_reason": item.review_reason_code,
    }


def capture_payroll_review_evidence(
    candidate: PayrollStorageCandidate,
    snapshot: PayrollSheetsSnapshot,
    item_occurrence: int,
    *,
    local_key: bytes,
) -> PayrollReviewEvidence:
    """Capture immutable evidence for one pending review occurrence."""

    _require_key(local_key)
    payload = _evidence_payload(
        candidate, snapshot, item_occurrence, local_key,
    )
    item = candidate.items[item_occurrence]
    if not item.needs_review or item.review_status != "pending":
        raise ValueError("item_not_pending")
    return PayrollReviewEvidence(**payload, evidence_id=_mac(local_key, payload))


def create_payroll_review_assertion(
    evidence: PayrollReviewEvidence,
    *,
    decision: ReviewDecision,
    operator_id: str,
    local_key: bytes,
    standard_item_id: str | None = None,
    reviewed_value: int | float | str | None = None,
    assertion_revision: int = 1,
) -> PayrollReviewAssertion:
    """Sign an explicit decision; this function never chooses one."""

    _require_key(local_key)
    if not operator_id.strip():
        raise ValueError("operator_id_required")
    payload = {
        "contract_version": REVIEW_AUTHORITY_VERSION,
        "assertion_revision": assertion_revision,
        "evidence_id": evidence.evidence_id,
        "decision": decision,
        "operator_id": operator_id,
        "standard_item_id": standard_item_id,
        "reviewed_value": reviewed_value,
    }
    return PayrollReviewAssertion(**payload, signature=_mac(local_key, payload))


def _assertion_payload(assertion: PayrollReviewAssertion):
    return {
        "contract_version": assertion.contract_version,
        "assertion_revision": assertion.assertion_revision,
        "evidence_id": assertion.evidence_id,
        "decision": assertion.decision,
        "operator_id": assertion.operator_id,
        "standard_item_id": assertion.standard_item_id,
        "reviewed_value": assertion.reviewed_value,
    }


def _typed_raw_value(raw_value: str | None, value_type: str):
    if raw_value is None:
        return None
    stripped = raw_value.strip()
    if value_type == "money":
        if not re.fullmatch(r"[+-]?(?:\d+|\d{1,3}(?:,\d{3})+)", stripped):
            return None
        return int(stripped.replace(",", ""))
    if value_type in {"number", "days", "hours"}:
        suffix = "日" if value_type == "days" else "時間" if value_type == "hours" else ""
        if suffix and stripped.endswith(suffix):
            stripped = stripped[:-len(suffix)].strip()
        if not re.fullmatch(r"[+-]?\d+(?:\.\d+)?", stripped):
            return None
        number = float(stripped)
        return int(number) if number.is_integer() else number
    if value_type == "text":
        return raw_value
    return None


def preview_payroll_review_assertion(
    candidate: PayrollStorageCandidate,
    snapshot: PayrollSheetsSnapshot,
    evidence: PayrollReviewEvidence,
    assertion: PayrollReviewAssertion,
    *,
    local_key: bytes,
) -> PayrollReviewEvaluation:
    """Validate authority without changing the candidate or any external state."""

    _require_key(local_key)

    def reject(reason):
        return PayrollReviewEvaluation(
            False, reason, evidence.evidence_id, assertion.decision,
        )

    if (evidence.contract_version != REVIEW_AUTHORITY_VERSION
            or assertion.contract_version != REVIEW_AUTHORITY_VERSION):
        return reject("review_contract_version_stale")
    if (not isinstance(assertion.assertion_revision, int)
            or isinstance(assertion.assertion_revision, bool)
            or assertion.assertion_revision != 1):
        return reject("assertion_revision_stale")
    if not assertion.operator_id.strip():
        return reject("operator_id_required")
    if assertion.evidence_id != evidence.evidence_id:
        return reject("assertion_evidence_mismatch")
    if not hmac.compare_digest(
        assertion.signature, _mac(local_key, _assertion_payload(assertion)),
    ):
        return reject("assertion_signature_invalid")
    try:
        current = capture_payroll_review_evidence(
            candidate, snapshot, evidence.item_occurrence, local_key=local_key,
        )
    except (IndexError, ValueError):
        return reject("review_evidence_not_current")
    if current != evidence:
        return reject("review_evidence_stale")
    item = candidate.items[evidence.item_occurrence]
    if assertion.decision == "exclude_non_item":
        if assertion.standard_item_id is not None or assertion.reviewed_value is not None:
            return reject("excluded_item_cannot_set_field_or_value")
    elif assertion.decision == "confirm_existing_value":
        if item.raw_value is None:
            return reject("source_value_missing")
        standard = next((
            value for value in snapshot.standard_items
            if value.active and value.standard_item_id == assertion.standard_item_id
        ), None)
        if standard is None:
            return reject("active_standard_item_missing")
        typed = _typed_raw_value(item.raw_value, standard.value_type)
        if (typed != assertion.reviewed_value
                or isinstance(assertion.reviewed_value, bool)
                or (isinstance(typed, int)
                    and not isinstance(assertion.reviewed_value, int))
                or (isinstance(typed, float)
                    and not isinstance(assertion.reviewed_value, (int, float)))):
            return reject("reviewed_value_not_exact_raw_value")
    else:
        return reject("unsupported_review_decision")
    return PayrollReviewEvaluation(
        True, "review_assertion_accepted", evidence.evidence_id,
        assertion.decision,
    )


def apply_payroll_review_assertion(
    candidate: PayrollStorageCandidate,
    snapshot: PayrollSheetsSnapshot,
    evidence: PayrollReviewEvidence,
    assertion: PayrollReviewAssertion,
    *,
    local_key: bytes,
    confirmed: bool = False,
) -> PayrollReviewApplyResult:
    """Apply to a copy only after explicit confirmation; performs no I/O."""

    evaluation = preview_payroll_review_assertion(
        candidate, snapshot, evidence, assertion, local_key=local_key,
    )
    result = candidate.model_copy(deep=True)
    if not confirmed or not evaluation.accepted:
        return PayrollReviewApplyResult(result, evaluation, False)
    item = result.items[evidence.item_occurrence]
    if assertion.decision == "exclude_non_item":
        item.standard_item_id = None
        item.value = None
        item.needs_review = False
        item.review_status = "confirmed"
    else:
        standard = next(
            value for value in snapshot.standard_items
            if value.active and value.standard_item_id == assertion.standard_item_id
        )
        corrected = item.standard_item_id != assertion.standard_item_id
        item.standard_item_id = assertion.standard_item_id
        item.section = standard.section
        item.value = assertion.reviewed_value
        item.needs_review = False
        item.review_status = "corrected" if corrected else "confirmed"
    item.review_reason_code = None
    sync_statement_review_reasons(result.statement, result.items)
    result.statement.needs_review = bool(
        result.statement.parse_status != "success"
        or result.statement.statement_type in {None, "other"}
        or any(value.needs_review or value.review_status == "pending"
               for value in result.items)
    )
    return PayrollReviewApplyResult(result, evaluation, True)
