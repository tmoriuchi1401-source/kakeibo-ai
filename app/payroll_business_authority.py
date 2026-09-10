"""Conservative, read-only resolution of existing Payroll business authority.

Parser evidence may identify a label, but it never creates an employer identity.
Employer scope is resolved only by an exact, unique match to the active employer
master after presentation-only Unicode and whitespace normalization.
"""

from __future__ import annotations

from dataclasses import dataclass
import unicodedata
from typing import Iterable

from .payroll_models import PayrollPreview
from .payroll_storage import (
    PayrollEmployerRecord,
    StatementType,
    classify_statement_type,
)


@dataclass(frozen=True)
class PayrollBusinessAuthority:
    employer_id: str | None
    statement_type: StatementType | None
    employer_source: str
    statement_type_source: str
    employer_reason: str
    statement_type_reason: str


def _normalized_employer_label(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value)
    return "".join(normalized.split())


def resolve_payroll_business_authority(
    preview: PayrollPreview,
    employers: Iterable[PayrollEmployerRecord],
    *,
    employer_id: str | None = None,
    statement_type: StatementType | None = None,
) -> PayrollBusinessAuthority:
    """Resolve only authority already supplied or present in trusted evidence."""

    resolved_employer = employer_id
    employer_source = "operator" if employer_id else "unresolved"
    employer_reason = "operator_supplied" if employer_id else "employer_evidence_missing"
    if resolved_employer is None and preview.company_name:
        evidence = _normalized_employer_label(preview.company_name)
        matches = {
            employer.employer_id
            for employer in employers
            if employer.active
            and _normalized_employer_label(employer.employer_label) == evidence
        }
        if len(matches) == 1:
            resolved_employer = next(iter(matches))
            employer_source = "active_employer_master_exact_match"
            employer_reason = "exact_unique_active_employer_match"
        elif len(matches) > 1:
            employer_reason = "active_employer_match_ambiguous"
        else:
            employer_reason = "active_employer_match_missing"

    resolved_statement_type = statement_type
    type_source = "operator" if statement_type else "unresolved"
    type_reason = "operator_supplied" if statement_type else "statement_type_evidence_missing"
    if resolved_statement_type is None and preview.statement_label:
        classified = classify_statement_type(preview.statement_label)
        if classified != "other":
            resolved_statement_type = classified
            type_source = "parser_explicit_statement_label"
            type_reason = "explicit_statement_label_classified"
        else:
            type_reason = "statement_type_evidence_unsupported"

    return PayrollBusinessAuthority(
        employer_id=resolved_employer,
        statement_type=resolved_statement_type,
        employer_source=employer_source,
        statement_type_source=type_source,
        employer_reason=employer_reason,
        statement_type_reason=type_reason,
    )
