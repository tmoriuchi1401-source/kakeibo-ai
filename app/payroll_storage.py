from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Iterable, Literal
from uuid import uuid4

from pydantic import BaseModel, Field, model_validator

from .payroll_models import PayrollItem, PayrollPreview


StatementType = Literal["salary", "bonus", "adjustment", "other"]
ItemSection = Literal["earning", "deduction", "attendance", "reference", "unknown"]
ReviewStatus = Literal["not_required", "pending", "confirmed", "corrected"]
DuplicateStatus = Literal["new", "duplicate", "needs_review"]

PARSER_VERSION = "payroll-phase-b1-v1"


def _uuid() -> str:
    return str(uuid4())


def _now() -> datetime:
    return datetime.now(timezone.utc)


class PayrollStatementRecord(BaseModel):
    statement_id: str = Field(default_factory=_uuid)
    employer_id: str | None = None
    statement_type: StatementType = "other"
    pay_period: str | None = None
    pay_date: str | None = None
    gross_pay: int | None = None
    total_deductions: int | None = None
    net_pay: int | None = None
    parse_status: Literal["success", "partial", "failed"] = "failed"
    needs_review: bool = True
    source_type: Literal["drive", "local", "other"] = "other"
    source_file_id: str | None = None
    content_hash: str | None = None
    imported_at: datetime = Field(default_factory=_now)
    parser_version: str = PARSER_VERSION


class PayrollStatementItemRecord(BaseModel):
    item_id: str = Field(default_factory=_uuid)
    statement_id: str
    raw_item_name: str
    standard_item_id: str | None = None
    section: ItemSection = "unknown"
    raw_value: str | None = None
    value: int | float | str | None = None
    confidence: float | None = None
    needs_review: bool = False
    review_status: ReviewStatus = "not_required"
    display_order: int = 0

    @model_validator(mode="after")
    def keep_unreviewed_values_unconfirmed(self):
        if self.needs_review or self.review_status == "pending":
            self.value = None
            self.needs_review = True
            self.review_status = "pending"
        return self


class PayrollStandardItemRecord(BaseModel):
    standard_item_id: str
    standard_name: str
    section: ItemSection
    value_type: Literal["money", "number", "hours", "days", "text"]
    active: bool = True
    created_at: datetime = Field(default_factory=_now)


class PayrollItemAliasRecord(BaseModel):
    alias_id: str = Field(default_factory=_uuid)
    raw_item_name: str
    standard_item_id: str
    employer_id: str | None = None
    active: bool = True
    created_at: datetime = Field(default_factory=_now)


class PayrollEmployerRecord(BaseModel):
    employer_id: str = Field(default_factory=_uuid)
    employer_label: str
    active: bool = True
    start_date: date | None = None
    end_date: date | None = None


class PayrollStorageCandidate(BaseModel):
    statement: PayrollStatementRecord
    items: list[PayrollStatementItemRecord] = Field(default_factory=list)
    # Preview-only source context. These fields are deliberately absent from the
    # Sheets schemas and therefore can never become stored columns by accident.
    file_name: str | None = None
    parse_method: Literal["pdf_text", "ocr"] | None = None
    employee: str | None = None


class DuplicateDecision(BaseModel):
    status: DuplicateStatus
    reason: Literal["none", "source_file_id", "content_hash", "statement_key"]
    matched_statement_id: str | None = None


PAYROLL_STATEMENT_COLUMNS = (
    "statement_id", "employer_id", "statement_type", "pay_period", "pay_date",
    "gross_pay", "total_deductions", "net_pay", "parse_status", "needs_review",
    "source_type", "source_file_id", "content_hash", "imported_at", "parser_version",
)

PAYROLL_ITEM_COLUMNS = (
    "item_id", "statement_id", "raw_item_name", "standard_item_id", "section",
    "raw_value", "value", "confidence", "needs_review", "review_status",
    "display_order",
)

PAYROLL_STANDARD_ITEM_COLUMNS = (
    "standard_item_id", "standard_name", "section", "value_type", "active", "created_at",
)

PAYROLL_ALIAS_COLUMNS = (
    "alias_id", "raw_item_name", "standard_item_id", "employer_id", "active", "created_at",
)

PAYROLL_EMPLOYER_COLUMNS = (
    "employer_id", "employer_label", "active", "start_date", "end_date",
)

PAYROLL_SCHEMAS = {
    "payroll_statements": PAYROLL_STATEMENT_COLUMNS,
    "payroll_items": PAYROLL_ITEM_COLUMNS,
    "payroll_standard_items": PAYROLL_STANDARD_ITEM_COLUMNS,
    "payroll_item_aliases": PAYROLL_ALIAS_COLUMNS,
    "payroll_employers": PAYROLL_EMPLOYER_COLUMNS,
}


INITIAL_STANDARD_ITEMS = (
    PayrollStandardItemRecord(standard_item_id="basic_pay", standard_name="基本給",
                              section="earning", value_type="money"),
    PayrollStandardItemRecord(standard_item_id="overtime_pay", standard_name="時間外手当",
                              section="earning", value_type="money"),
    PayrollStandardItemRecord(standard_item_id="commuting_allowance", standard_name="通勤手当",
                              section="earning", value_type="money"),
    PayrollStandardItemRecord(standard_item_id="health_insurance", standard_name="健康保険",
                              section="deduction", value_type="money"),
    PayrollStandardItemRecord(standard_item_id="employees_pension", standard_name="厚生年金",
                              section="deduction", value_type="money"),
    PayrollStandardItemRecord(standard_item_id="employment_insurance", standard_name="雇用保険",
                              section="deduction", value_type="money"),
    PayrollStandardItemRecord(standard_item_id="income_tax", standard_name="所得税",
                              section="deduction", value_type="money"),
    PayrollStandardItemRecord(standard_item_id="resident_tax", standard_name="住民税",
                              section="deduction", value_type="money"),
    PayrollStandardItemRecord(standard_item_id="union_dues", standard_name="組合費",
                              section="deduction", value_type="money"),
    PayrollStandardItemRecord(standard_item_id="taxable_earnings",
                              standard_name="課税対象支給額",
                              section="reference", value_type="money"),
    PayrollStandardItemRecord(standard_item_id="taxable_amount",
                              standard_name="課税対象額",
                              section="reference", value_type="money"),
    PayrollStandardItemRecord(standard_item_id="social_insurance_total",
                              standard_name="社会保険控除",
                              section="reference", value_type="money"),
    PayrollStandardItemRecord(standard_item_id="non_taxable_total",
                              standard_name="非課税合計",
                              section="reference", value_type="money"),
    PayrollStandardItemRecord(standard_item_id="remuneration_amount",
                              standard_name="報酬月額",
                              section="reference", value_type="money"),
    PayrollStandardItemRecord(standard_item_id="employment_insurance_base",
                              standard_name="雇用保険対象額",
                              section="reference", value_type="money"),
    PayrollStandardItemRecord(standard_item_id="ytd_gross_pay",
                              standard_name="総支給額累計",
                              section="reference", value_type="money"),
    PayrollStandardItemRecord(standard_item_id="ytd_taxable_amount",
                              standard_name="累積課税合計",
                              section="reference", value_type="money"),
    PayrollStandardItemRecord(standard_item_id="ytd_social_insurance",
                              standard_name="社会保険料累計",
                              section="reference", value_type="money"),
    PayrollStandardItemRecord(standard_item_id="ytd_income_tax",
                              standard_name="所得税累計",
                              section="reference", value_type="money"),
    PayrollStandardItemRecord(standard_item_id="attendance_days", standard_name="出勤日数",
                              section="attendance", value_type="days"),
    PayrollStandardItemRecord(standard_item_id="paid_leave_days", standard_name="有給日数",
                              section="attendance", value_type="days"),
    PayrollStandardItemRecord(standard_item_id="overtime_hours", standard_name="残業時間",
                              section="attendance", value_type="hours"),
)

INITIAL_ALIASES = (
    PayrollItemAliasRecord(alias_id="alias-basic-pay-honkyu",
                           raw_item_name="本給", standard_item_id="basic_pay"),
    PayrollItemAliasRecord(alias_id="alias-health-insurance-fee",
                           raw_item_name="健康保険料", standard_item_id="health_insurance"),
    PayrollItemAliasRecord(alias_id="alias-employees-pension-insurance",
                           raw_item_name="厚生年金保険", standard_item_id="employees_pension"),
    PayrollItemAliasRecord(alias_id="alias-employment-insurance-fee",
                           raw_item_name="雇用保険料", standard_item_id="employment_insurance"),
    PayrollItemAliasRecord(alias_id="alias-overtime-work-allowance",
                           raw_item_name="時間外勤務手当", standard_item_id="overtime_pay"),
    PayrollItemAliasRecord(alias_id="alias-overtime-allowance",
                           raw_item_name="残業手当", standard_item_id="overtime_pay"),
    PayrollItemAliasRecord(alias_id="alias-union-dues",
                           raw_item_name="組合費", standard_item_id="union_dues"),
    PayrollItemAliasRecord(alias_id="alias-taxable-earnings",
                           raw_item_name="課税対象支給額",
                           standard_item_id="taxable_earnings"),
    PayrollItemAliasRecord(alias_id="alias-taxable-amount",
                           raw_item_name="課税対象額",
                           standard_item_id="taxable_amount"),
    PayrollItemAliasRecord(alias_id="alias-social-insurance-total",
                           raw_item_name="社会保険控除",
                           standard_item_id="social_insurance_total"),
    PayrollItemAliasRecord(alias_id="alias-non-taxable-total",
                           raw_item_name="非課税合計",
                           standard_item_id="non_taxable_total"),
    PayrollItemAliasRecord(alias_id="alias-remuneration-amount",
                           raw_item_name="報酬月額",
                           standard_item_id="remuneration_amount"),
    PayrollItemAliasRecord(alias_id="alias-employment-insurance-base",
                           raw_item_name="雇用保険対象額",
                           standard_item_id="employment_insurance_base"),
    PayrollItemAliasRecord(alias_id="alias-ytd-gross-pay",
                           raw_item_name="総支給額累計",
                           standard_item_id="ytd_gross_pay"),
    PayrollItemAliasRecord(alias_id="alias-ytd-taxable-amount",
                           raw_item_name="累積課税合計",
                           standard_item_id="ytd_taxable_amount"),
    PayrollItemAliasRecord(alias_id="alias-ytd-social-insurance",
                           raw_item_name="社会保険料累計",
                           standard_item_id="ytd_social_insurance"),
    PayrollItemAliasRecord(alias_id="alias-ytd-income-tax",
                           raw_item_name="所得税累計",
                           standard_item_id="ytd_income_tax"),
)


def classify_statement_type(label: str | None) -> StatementType:
    """Classify only explicit labels; an absent or ambiguous label stays ``other``."""
    normalized = "".join((label or "").split())
    if any(marker in normalized for marker in ("賞与", "ボーナス")):
        return "bonus"
    if any(marker in normalized for marker in ("調整明細", "給与調整")):
        return "adjustment"
    if any(marker in normalized for marker in ("給与明細", "給与", "給料")):
        return "salary"
    return "other"


def resolve_alias(
    raw_item_name: str,
    aliases: Iterable[PayrollItemAliasRecord],
    employer_id: str | None = None,
) -> str | None:
    """Resolve an active employer alias before an active common alias."""
    matches = [alias for alias in aliases
               if alias.active and alias.raw_item_name == raw_item_name]
    if employer_id is not None:
        specific = next((alias for alias in matches if alias.employer_id == employer_id), None)
        if specific:
            return specific.standard_item_id
    common = next((alias for alias in matches if alias.employer_id is None), None)
    return common.standard_item_id if common else None


def decide_duplicate(
    candidate: PayrollStatementRecord,
    existing: Iterable[PayrollStatementRecord],
) -> DuplicateDecision:
    """Return a decision without changing either candidate or existing records."""
    records = tuple(existing)
    if candidate.source_file_id:
        match = next((record for record in records
                      if record.source_file_id == candidate.source_file_id), None)
        if match:
            return DuplicateDecision(status="duplicate", reason="source_file_id",
                                     matched_statement_id=match.statement_id)
    if candidate.content_hash:
        match = next((record for record in records
                      if record.content_hash == candidate.content_hash), None)
        if match:
            return DuplicateDecision(status="duplicate", reason="content_hash",
                                     matched_statement_id=match.statement_id)
    if candidate.employer_id and candidate.pay_period:
        match = next((record for record in records
                      if record.employer_id == candidate.employer_id
                      and record.pay_period == candidate.pay_period
                      and record.statement_type == candidate.statement_type), None)
        if match:
            return DuplicateDecision(status="needs_review", reason="statement_key",
                                     matched_statement_id=match.statement_id)
    return DuplicateDecision(status="new", reason="none")


_SECTION_MAP: dict[str, ItemSection] = {
    "earnings": "earning",
    "deductions": "deduction",
    "attendance": "attendance",
    "summary": "reference",
    "unknown": "unknown",
    "earning": "earning",
    "deduction": "deduction",
    "reference": "reference",
}


def _convert_item(
    item: PayrollItem,
    statement_id: str,
    display_order: int,
    aliases: Iterable[PayrollItemAliasRecord],
    standard_items: Iterable[PayrollStandardItemRecord],
    employer_id: str | None,
    extraction_method: Literal["pdf_text", "ocr"],
) -> PayrollStatementItemRecord:
    standard_item_id = (
        resolve_alias(item.raw_item_name, aliases, employer_id)
        or item.standard_item_candidate
    )
    standard = next(
        (record for record in standard_items
         if record.active and record.standard_item_id == standard_item_id),
        None,
    )
    section = standard.section if standard else _SECTION_MAP.get(item.section, "unknown")
    uncertain = (
        item.needs_review
        or standard_item_id is None
        or (extraction_method == "ocr" and section == "reference")
    )
    return PayrollStatementItemRecord(
        statement_id=statement_id,
        raw_item_name=item.raw_item_name,
        standard_item_id=standard_item_id,
        section=section,
        raw_value=item.raw_value,
        value=None if uncertain else item.value,
        confidence=item.confidence,
        needs_review=uncertain,
        review_status="pending" if uncertain else "not_required",
        display_order=display_order,
    )


def phase_a_to_storage_candidate(
    preview: PayrollPreview,
    *,
    employer_id: str | None = None,
    statement_label: str | None = None,
    source_type: Literal["drive", "local", "other"] = "other",
    source_file_id: str | None = None,
    content_hash: str | None = None,
    aliases: Iterable[PayrollItemAliasRecord] = INITIAL_ALIASES,
    standard_items: Iterable[PayrollStandardItemRecord] = INITIAL_STANDARD_ITEMS,
    parser_version: str = PARSER_VERSION,
    file_name: str | None = None,
    employee: str | None = None,
) -> PayrollStorageCandidate:
    """Convert Phase A output to a storage candidate without I/O or mutation."""
    statement_type = classify_statement_type(statement_label)
    statement = PayrollStatementRecord(
        employer_id=employer_id,
        statement_type=statement_type,
        pay_period=preview.pay_period,
        pay_date=preview.pay_date,
        gross_pay=preview.gross_pay,
        total_deductions=preview.total_deductions,
        net_pay=preview.net_pay,
        parse_status=preview.parse_status,
        source_type=source_type,
        source_file_id=source_file_id,
        content_hash=content_hash,
        parser_version=parser_version,
    )
    items = [_convert_item(
        item, statement.statement_id, index, aliases, standard_items, employer_id,
        preview.extraction_method,
    )
             for index, item in enumerate(preview.items)]
    statement.needs_review = (
        preview.parse_status != "success"
        or statement_type == "other"
        or any(item.needs_review for item in items)
    )
    return PayrollStorageCandidate(
        statement=statement,
        items=items,
        file_name=file_name,
        parse_method=preview.extraction_method,
        employee=employee,
    )
