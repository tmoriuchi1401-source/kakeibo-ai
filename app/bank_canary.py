"""Exact-one, write-free production canary preparation for bank PDF rows."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import re

from .aupay_card_executor import ExistingCanonicalRecord, SheetsCanonicalIdentityReader
from .aupay_card_writer import (
    ReadOnlySheetsTargetInspector,
    TargetBinding,
    validate_target_binding,
)
from .bank_pdf_pipeline import DEFAULT_ACCOUNT_ALIAS, BankPdfPipeline, SOURCE
from .bank_reconciliation import (
    BankPreviewPlan,
    BankShadowResult,
    ConfirmedInternalTransfers,
    build_bank_preview_plan,
    build_bank_shadow_result,
)
from .canonical_import import materialize_import_row
from .reconciliation import parse_import_rows
from .sheets import HEADERS, SheetsDB
from .transaction_plan import Transaction


CANARY_TARGET_SHEET = "取込データ"
CANARY_MAX_ROWS = 1
BANK_BATCH_ROWS = 5
BANK_LOAN_ROWS = 4
BANK_INITIAL_BACKFILL_ROWS = 51
BANK_BATCH_ROW_BOUNDS = frozenset({
    BANK_LOAN_ROWS, BANK_BATCH_ROWS, BANK_INITIAL_BACKFILL_ROWS,
})
CANARY_CLASSIFICATIONS = frozenset({"income", "expense"})
LOAN_CLASSIFICATION = "loan_repayment"
LOAN_EXPENSE_CATEGORY = ("住まい", "住宅ローン")
_BANK_CANARY_PLAN_AUTHORITY = object()
_BANK_BATCH_PLAN_AUTHORITY = object()


@dataclass(frozen=True)
class BankCanaryAuthority:
    """Operator-selected, read-only authority envelope for one source identity."""

    selected_source_identity: str
    target_spreadsheet_id: str = field(repr=False)
    target_worksheet: str = CANARY_TARGET_SHEET
    max_rows: int = CANARY_MAX_ROWS
    authority_mode: str = "bank_canary_preparation"

    def validate(self) -> None:
        if (
            not self.selected_source_identity
            or len(self.selected_source_identity) > 256
            or not re.fullmatch(r"[^\s\x00-\x1f]+", self.selected_source_identity)
        ):
            raise RuntimeError("bank_canary_source_identity_required")
        if not self.target_spreadsheet_id:
            raise RuntimeError("bank_canary_target_spreadsheet_required")
        if self.target_worksheet != CANARY_TARGET_SHEET:
            raise RuntimeError("bank_canary_target_sheet_invalid")
        if self.max_rows != CANARY_MAX_ROWS:
            raise RuntimeError("bank_canary_max_rows_must_be_one")
        if self.authority_mode != "bank_canary_preparation":
            raise RuntimeError("bank_canary_production_authority_forbidden")


@dataclass(frozen=True, init=False)
class BankCanaryPlan:
    """A single canonical row projection; it is not a production capability."""

    authority: BankCanaryAuthority = field(repr=False)
    transaction: Transaction = field(repr=False)
    classification: str
    write_eligibility: str
    import_status: str
    target_binding: TargetBinding = field(repr=False)
    planned_rows: int = 1
    authorized_rows: int = 1
    _authority: object = field(repr=False, compare=False)

    def __new__(cls, *args, **kwargs):
        raise TypeError("bank_canary_plan_is_builder_only")

    @classmethod
    def _create(cls, *, authority_token: object, **values) -> "BankCanaryPlan":
        if authority_token is not _BANK_CANARY_PLAN_AUTHORITY:
            raise TypeError("bank_canary_plan_is_builder_only")
        plan = object.__new__(cls)
        for name, value in values.items():
            object.__setattr__(plan, name, value)
        object.__setattr__(plan, "_authority", _BANK_CANARY_PLAN_AUTHORITY)
        return plan

    def materialize(self, *, imported_at) -> tuple[tuple, ...]:
        validate_bank_canary_plan(self)
        row = materialize_import_row(
            self.transaction, imported_at=imported_at, status=self.import_status,
        )
        if len(row) != len(HEADERS[CANARY_TARGET_SHEET]):
            raise RuntimeError("bank_canary_row_schema_invalid")
        return (tuple(row),)


@dataclass(frozen=True)
class BankBatchAuthority:
    """Explicit authority envelope for one exact, supported identity set."""

    selected_source_identities: tuple[str, ...]
    target_spreadsheet_id: str = field(repr=False)
    target_worksheet: str = CANARY_TARGET_SHEET
    min_rows: int = BANK_BATCH_ROWS
    max_rows: int = BANK_BATCH_ROWS
    authority_mode: str = "bank_batch_preparation"

    def validate(self) -> None:
        expected_rows = self.max_rows
        if self.min_rows != expected_rows or expected_rows not in BANK_BATCH_ROW_BOUNDS:
            if self.min_rows == BANK_BATCH_ROWS:
                raise RuntimeError("bank_batch_rows_bound_must_be_five")
            raise RuntimeError("bank_batch_row_bound_invalid")
        if len(self.selected_source_identities) != expected_rows:
            if expected_rows == BANK_BATCH_ROWS:
                raise RuntimeError("bank_batch_requires_exactly_five_identities")
            raise RuntimeError("bank_batch_requires_exact_authorized_identities")
        if len(set(self.selected_source_identities)) != expected_rows:
            raise RuntimeError("bank_batch_selector_duplicate_identity")
        for identity in self.selected_source_identities:
            if (
                not identity
                or len(identity) > 256
                or not re.fullmatch(r"[^\s\x00-\x1f]+", identity)
            ):
                raise RuntimeError("bank_batch_source_identity_required")
        if not self.target_spreadsheet_id:
            raise RuntimeError("bank_batch_target_spreadsheet_required")
        if self.target_worksheet != CANARY_TARGET_SHEET:
            raise RuntimeError("bank_batch_target_sheet_invalid")
        expected_mode = {
            BANK_LOAN_ROWS: "bank_loan_repayment_preparation",
            BANK_BATCH_ROWS: "bank_batch_preparation",
            BANK_INITIAL_BACKFILL_ROWS: "bank_initial_backfill",
        }[expected_rows]
        if self.authority_mode != expected_mode:
            raise RuntimeError("bank_batch_production_authority_forbidden")


@dataclass(frozen=True)
class BankBatchItem:
    transaction: Transaction = field(repr=False)
    classification: str
    projected_classification: str
    write_eligibility: str
    import_status: str


@dataclass(frozen=True, init=False)
class BankBatchPlan:
    """Projection-only exact batch plan; it never carries write capability."""

    authority: BankBatchAuthority = field(repr=False)
    items: tuple[BankBatchItem, ...] = field(repr=False)
    planned_rows: int = BANK_BATCH_ROWS
    authorized_rows: int = BANK_BATCH_ROWS
    _authority: object = field(repr=False, compare=False)

    def __new__(cls, *args, **kwargs):
        raise TypeError("bank_batch_plan_is_builder_only")

    @classmethod
    def _create(cls, *, authority_token: object, **values) -> "BankBatchPlan":
        if authority_token is not _BANK_BATCH_PLAN_AUTHORITY:
            raise TypeError("bank_batch_plan_is_builder_only")
        plan = object.__new__(cls)
        for name, value in values.items():
            object.__setattr__(plan, name, value)
        object.__setattr__(plan, "_authority", _BANK_BATCH_PLAN_AUTHORITY)
        return plan

    def materialize(self, *, imported_at) -> tuple[tuple, ...]:
        validate_bank_batch_plan(self)
        rows = tuple(
            tuple(materialize_import_row(
                item.transaction,
                imported_at=imported_at,
                status=item.import_status,
            ))
            for item in self.items
        )
        if len(rows) != self.authority.max_rows or any(
            len(row) != len(HEADERS[CANARY_TARGET_SHEET]) for row in rows
        ):
            raise RuntimeError("bank_batch_row_schema_invalid")
        return rows


@dataclass(frozen=True)
class BankBatchDryRunResult:
    authority_mode: str
    selected: int
    planned: int
    authorized: int
    withheld: int
    existing_duplicate: int
    ambiguous_collision: int
    target_binding_valid: bool
    target_header_valid: bool
    selected_identities_absent: bool
    max_writes: int
    income: int
    expense: int
    loan_repayment: int = 0
    projected_expense: int = 0
    write_attempted: int = 0
    external_write_count: int = 0

    def summary(self) -> dict:
        result = {
            "authority_mode": self.authority_mode,
            "selected": self.selected,
            "planned": self.planned,
            "authorized": self.authorized,
            "withheld": self.withheld,
            "existing_duplicate": self.existing_duplicate,
            "ambiguous_collision": self.ambiguous_collision,
            "target_binding_valid": self.target_binding_valid,
            "target_header_valid": self.target_header_valid,
            "selected_identities_absent": self.selected_identities_absent,
            "max_writes": self.max_writes,
            "income": self.income,
            "expense": self.expense,
            "write_attempted": self.write_attempted,
            "external_write_count": self.external_write_count,
        }
        if self.loan_repayment:
            result.update({
                "loan_repayment": self.loan_repayment,
                "projected_expense": self.projected_expense,
            })
        return result


@dataclass(frozen=True)
class BankLoanPreviewResult:
    selected: int
    planned: int
    write_eligible: int
    canonical_expense_rows: int
    existing_duplicate: int
    ambiguous_collision: int
    target_binding_valid: bool
    target_header_valid: bool
    category_authority_valid: bool
    category_major: str
    category_minor: str
    write_attempted: int = 0
    external_write_count: int = 0

    def summary(self) -> dict:
        return {
            "selected": self.selected,
            "planned": self.planned,
            "write_eligible": self.write_eligible,
            "canonical_expense_rows": self.canonical_expense_rows,
            "existing_duplicate": self.existing_duplicate,
            "ambiguous_collision": self.ambiguous_collision,
            "target_binding_valid": self.target_binding_valid,
            "target_header_valid": self.target_header_valid,
            "expense_category": {
                "major": self.category_major, "minor": self.category_minor,
            },
            "category_authority_valid": self.category_authority_valid,
            "write_attempted": self.write_attempted,
            "external_write_count": self.external_write_count,
        }


@dataclass(frozen=True)
class BankCanaryDryRunResult:
    authority_mode: str
    classification: str
    write_eligibility: str
    target_worksheet: str
    planned: int
    authorized: int
    withheld: int
    existing_duplicate: int
    ambiguous_collision: int
    target_binding_valid: bool
    target_header_valid: bool
    selected_identity_absent: bool
    max_writes: int
    external_write_count: int = 0

    def summary(self) -> dict:
        return {
            "authority_mode": self.authority_mode,
            "classification": self.classification,
            "write_eligibility": self.write_eligibility,
            "target_worksheet": self.target_worksheet,
            "planned": self.planned,
            "authorized": self.authorized,
            "withheld": self.withheld,
            "existing_duplicate": self.existing_duplicate,
            "ambiguous_collision": self.ambiguous_collision,
            "target_binding_valid": self.target_binding_valid,
            "target_header_valid": self.target_header_valid,
            "selected_identity_absent": self.selected_identity_absent,
            "max_writes": self.max_writes,
            "external_write_count": self.external_write_count,
        }


def _matching_decisions(shadow: BankShadowResult, identity: str):
    return tuple(
        decision for decision in shadow.decisions
        if decision.classification.transaction.source_row_identity == identity
    )


def build_bank_canary_plan(
    shadow: BankShadowResult,
    preview: BankPreviewPlan,
    authority: BankCanaryAuthority,
) -> BankCanaryPlan:
    """Select exactly one already-eligible stable identity and fail closed."""
    authority.validate()
    matches = _matching_decisions(shadow, authority.selected_source_identity)
    if not matches:
        raise RuntimeError("bank_canary_selector_zero_matches")
    if len(matches) != 1:
        raise RuntimeError("bank_canary_selector_multiple_matches")
    decision = matches[0]
    classification = decision.classification.classification
    if classification not in CANARY_CLASSIFICATIONS:
        raise RuntimeError("bank_canary_classification_withheld")
    if decision.write_eligibility != "preview_candidate":
        raise RuntimeError("bank_canary_write_eligibility_withheld")
    candidate_matches = preview.candidate_identities.count(
        authority.selected_source_identity,
    )
    if candidate_matches == 0:
        raise RuntimeError("bank_canary_candidate_duplicate_or_collision")
    if candidate_matches != 1:
        raise RuntimeError("bank_canary_candidate_identity_not_unique")

    transaction = decision.classification.transaction.to_canonical()
    if transaction.source != SOURCE or transaction.identity != authority.selected_source_identity:
        raise RuntimeError("bank_canary_source_identity_changed")
    if (
        classification == "income"
        and (transaction.transaction_kind != "deposit" or transaction.amount_yen <= 0)
    ) or (
        classification == "expense"
        and (transaction.transaction_kind != "withdrawal" or transaction.amount_yen >= 0)
    ):
        raise RuntimeError("bank_canary_semantics_invalid")

    binding = TargetBinding(
        expected_spreadsheet_id=authority.target_spreadsheet_id,
        expected_worksheet=authority.target_worksheet,
    )
    plan = BankCanaryPlan._create(
        authority_token=_BANK_CANARY_PLAN_AUTHORITY,
        authority=authority,
        transaction=transaction,
        classification=classification,
        write_eligibility="eligible",
        import_status=f"bank_{classification}",
        target_binding=binding,
    )
    return validate_bank_canary_plan(plan)


def validate_bank_canary_plan(value: object) -> BankCanaryPlan:
    if type(value) is not BankCanaryPlan:
        raise TypeError("bank_canary_plan_required")
    if getattr(value, "_authority", None) is not _BANK_CANARY_PLAN_AUTHORITY:
        raise TypeError("unauthorized_bank_canary_plan")
    value.authority.validate()
    value.target_binding.validate()
    if (
        value.target_binding.expected_spreadsheet_id
        != value.authority.target_spreadsheet_id
        or value.target_binding.expected_worksheet != CANARY_TARGET_SHEET
        or value.target_binding.expected_header != tuple(HEADERS[CANARY_TARGET_SHEET])
    ):
        raise RuntimeError("bank_canary_target_binding_mismatch")
    if value.transaction.identity != value.authority.selected_source_identity:
        raise RuntimeError("bank_canary_source_identity_changed")
    if value.classification not in CANARY_CLASSIFICATIONS:
        raise RuntimeError("bank_canary_classification_withheld")
    if value.write_eligibility != "eligible":
        raise RuntimeError("bank_canary_write_eligibility_withheld")
    if value.import_status != f"bank_{value.classification}":
        raise RuntimeError("bank_canary_import_status_invalid")
    if value.planned_rows != 1 or value.authorized_rows != 1:
        raise RuntimeError("bank_canary_exactly_one_row_required")
    if value.planned_rows > value.authority.max_rows:
        raise RuntimeError("bank_canary_batch_exceeds_authority")
    return value


def _validate_bank_transaction_semantics(transaction: Transaction, classification: str) -> None:
    if transaction.source != SOURCE:
        raise RuntimeError("bank_batch_source_changed")
    if (
        classification == "income"
        and (transaction.transaction_kind != "deposit" or transaction.amount_yen <= 0)
    ) or (
        classification == "expense"
        and (transaction.transaction_kind != "withdrawal" or transaction.amount_yen >= 0)
    ):
        raise RuntimeError("bank_batch_semantics_invalid")


def build_bank_batch_plan(
    shadow: BankShadowResult,
    preview: BankPreviewPlan,
    authority: BankBatchAuthority,
) -> BankBatchPlan:
    """Bind an explicit supported identity set to eligible canonical projections."""
    authority.validate()
    items: list[BankBatchItem] = []
    for identity in authority.selected_source_identities:
        matches = _matching_decisions(shadow, identity)
        if not matches:
            raise RuntimeError("bank_batch_selector_zero_matches")
        if len(matches) != 1:
            raise RuntimeError("bank_batch_selector_multiple_matches")
        decision = matches[0]
        classification = decision.classification.classification
        loan_mode = authority.max_rows == BANK_LOAN_ROWS
        allowed_classifications = (
            frozenset({LOAN_CLASSIFICATION}) if loan_mode
            else CANARY_CLASSIFICATIONS
        )
        if classification not in allowed_classifications:
            raise RuntimeError("bank_batch_classification_withheld")
        if decision.write_eligibility != "preview_candidate":
            raise RuntimeError("bank_batch_write_eligibility_withheld")
        if preview.candidate_identities.count(identity) != 1:
            raise RuntimeError("bank_batch_candidate_duplicate_or_collision")
        transaction = decision.classification.transaction.to_canonical()
        if transaction.identity != identity:
            raise RuntimeError("bank_batch_source_identity_changed")
        projected_classification = (
            "expense" if classification == LOAN_CLASSIFICATION else classification
        )
        _validate_bank_transaction_semantics(transaction, projected_classification)
        items.append(BankBatchItem(
            transaction=transaction,
            classification=classification,
            projected_classification=projected_classification,
            write_eligibility="eligible",
            import_status=(
                "bank_loan_repayment"
                if classification == LOAN_CLASSIFICATION
                else f"bank_{classification}"
            ),
        ))
    plan = BankBatchPlan._create(
        authority_token=_BANK_BATCH_PLAN_AUTHORITY,
        authority=authority,
        items=tuple(items),
        planned_rows=authority.max_rows,
        authorized_rows=authority.max_rows,
    )
    return validate_bank_batch_plan(plan)


def validate_bank_batch_plan(value: object) -> BankBatchPlan:
    if type(value) is not BankBatchPlan:
        raise TypeError("bank_batch_plan_required")
    if getattr(value, "_authority", None) is not _BANK_BATCH_PLAN_AUTHORITY:
        raise TypeError("unauthorized_bank_batch_plan")
    value.authority.validate()
    expected_rows = value.authority.max_rows
    if value.planned_rows != expected_rows or value.authorized_rows != expected_rows:
        raise RuntimeError("bank_batch_requires_exact_authorized_rows")
    if len(value.items) != expected_rows:
        raise RuntimeError("bank_batch_requires_exact_authorized_items")
    if tuple(item.transaction.identity for item in value.items) != (
        value.authority.selected_source_identities
    ):
        raise RuntimeError("bank_batch_selector_order_changed")
    expected_header = tuple(HEADERS[CANARY_TARGET_SHEET])
    binding = TargetBinding(
        expected_spreadsheet_id=value.authority.target_spreadsheet_id,
        expected_worksheet=value.authority.target_worksheet,
    )
    binding.validate()
    if binding.expected_header != expected_header:
        raise RuntimeError("bank_batch_target_binding_mismatch")
    loan_mode = expected_rows == BANK_LOAN_ROWS
    allowed_classifications = (
        frozenset({LOAN_CLASSIFICATION}) if loan_mode else CANARY_CLASSIFICATIONS
    )
    for item in value.items:
        if item.classification not in allowed_classifications:
            raise RuntimeError("bank_batch_classification_withheld")
        expected_projection = (
            "expense" if item.classification == LOAN_CLASSIFICATION
            else item.classification
        )
        if item.projected_classification != expected_projection:
            raise RuntimeError("bank_batch_projected_classification_invalid")
        if item.write_eligibility != "eligible":
            raise RuntimeError("bank_batch_write_eligibility_withheld")
        expected_status = (
            "bank_loan_repayment"
            if item.classification == LOAN_CLASSIFICATION
            else f"bank_{item.classification}"
        )
        if item.import_status != expected_status:
            raise RuntimeError("bank_batch_import_status_invalid")
        _validate_bank_transaction_semantics(
            item.transaction, item.projected_classification,
        )
    return value


def dry_run_bank_batch(
    plan: BankBatchPlan,
    db: SheetsDB,
    *,
    imported_at,
) -> BankBatchDryRunResult:
    """Perform complete exact-set pre-read and row projection without a writer."""
    plan = validate_bank_batch_plan(plan)
    binding = TargetBinding(
        expected_spreadsheet_id=plan.authority.target_spreadsheet_id,
        expected_worksheet=plan.authority.target_worksheet,
    )
    inspector = ReadOnlySheetsTargetInspector(db)
    validate_target_binding(binding, inspector.inspect(binding.expected_worksheet))
    identities = tuple(item.transaction.identity for item in plan.items)
    reads = SheetsCanonicalIdentityReader(db).read_identities(identities)
    for item in plan.items:
        observation = reads.get(item.transaction.identity)
        if observation is None or not observation.readable:
            raise RuntimeError("bank_batch_identity_readback_unavailable")
        if observation.records:
            if (
                len(observation.records) == 1
                and observation.records[0] == ExistingCanonicalRecord.from_candidate(
                    item.transaction,
                )
            ):
                raise RuntimeError("bank_batch_existing_identity_duplicate")
            raise RuntimeError("bank_batch_existing_identity_collision")
    from .canonical_one_row_production import (
        SealedCanonicalOneRowTransport,
        project_bank_bounded_batch,
    )

    projected = project_bank_bounded_batch(plan)
    transport = SealedCanonicalOneRowTransport(
        db,
        binding=binding,
        inspector=None,
        key_provider=None,
        journal=None,
        clock=lambda: imported_at,
        max_rows=plan.authority.max_rows,
    )
    rows = transport.prepare_rows(projected.candidates, imported_at=imported_at)
    expected_rows = plan.authority.max_rows
    if len(rows) != expected_rows or any(
        len(row) != len(HEADERS[CANARY_TARGET_SHEET]) for row in rows
    ):
        raise RuntimeError("bank_batch_row_schema_invalid")
    income = sum(item.classification == "income" for item in plan.items)
    expense = sum(item.classification == "expense" for item in plan.items)
    loan_repayment = sum(
        item.classification == LOAN_CLASSIFICATION for item in plan.items
    )
    return BankBatchDryRunResult(
        authority_mode=plan.authority.authority_mode,
        selected=expected_rows,
        planned=expected_rows,
        authorized=expected_rows,
        withheld=0,
        existing_duplicate=0,
        ambiguous_collision=0,
        target_binding_valid=True,
        target_header_valid=True,
        selected_identities_absent=True,
        max_writes=plan.authority.max_rows,
        income=income,
        expense=expense,
        loan_repayment=loan_repayment,
        projected_expense=sum(
            item.projected_classification == "expense" for item in plan.items
        ) if loan_repayment else 0,
    )


def dry_run_bank_canary(
    plan: BankCanaryPlan,
    db: SheetsDB,
    *,
    imported_at,
) -> BankCanaryDryRunResult:
    """Run all read-side checks and row projection, then stop before writer dispatch."""
    plan = validate_bank_canary_plan(plan)
    inspector = ReadOnlySheetsTargetInspector(db)
    snapshot = inspector.inspect(CANARY_TARGET_SHEET)
    validate_target_binding(plan.target_binding, snapshot)
    reads = SheetsCanonicalIdentityReader(db).read_identities(
        (plan.transaction.identity,),
    )
    observation = reads.get(plan.transaction.identity)
    if observation is None or not observation.readable:
        raise RuntimeError("bank_canary_identity_readback_unavailable")
    if observation.records:
        if (
            len(observation.records) == 1
            and observation.records[0] == ExistingCanonicalRecord.from_candidate(
                plan.transaction,
            )
        ):
            raise RuntimeError("bank_canary_existing_identity_duplicate")
        raise RuntimeError("bank_canary_existing_identity_collision")
    rows = plan.materialize(imported_at=imported_at)
    if len(rows) != 1 or len(rows[0]) != len(HEADERS[CANARY_TARGET_SHEET]):
        raise RuntimeError("bank_canary_exactly_one_row_required")
    return BankCanaryDryRunResult(
        authority_mode=plan.authority.authority_mode,
        classification=plan.classification,
        write_eligibility=plan.write_eligibility,
        target_worksheet=plan.target_binding.expected_worksheet,
        planned=1,
        authorized=1,
        withheld=0,
        existing_duplicate=0,
        ambiguous_collision=0,
        target_binding_valid=True,
        target_header_valid=True,
        selected_identity_absent=True,
        max_writes=plan.authority.max_rows,
    )


def preview_bank_loan_repayments(
    shadow: BankShadowResult,
    preview: BankPreviewPlan,
    db: SheetsDB,
    *,
    imported_at,
    expected_rows: int = 4,
) -> BankLoanPreviewResult:
    """Validate all clear loan repayments up to the writer boundary, read-only."""
    decisions = tuple(
        decision for decision in shadow.decisions
        if decision.classification.classification == LOAN_CLASSIFICATION
    )
    if len(decisions) != expected_rows:
        raise RuntimeError("bank_loan_preview_expected_row_count_changed")
    if preview.ambiguous_collision:
        raise RuntimeError("bank_loan_preview_collision")
    if any(decision.write_eligibility != "preview_candidate" for decision in decisions):
        raise RuntimeError("bank_loan_preview_write_eligibility_withheld")
    transactions = tuple(
        decision.classification.transaction.to_canonical()
        for decision in decisions
    )
    identities = tuple(transaction.identity for transaction in transactions)
    if len(set(identities)) != expected_rows or any(
        preview.candidate_identities.count(identity) != 1 for identity in identities
    ):
        raise RuntimeError("bank_loan_preview_identity_not_unique")
    if any(
        transaction.source != SOURCE
        or transaction.transaction_kind != "withdrawal"
        or transaction.amount_yen >= 0
        for transaction in transactions
    ):
        raise RuntimeError("bank_loan_preview_expense_semantics_invalid")

    binding = TargetBinding(
        expected_spreadsheet_id=str(db.sid),
        expected_worksheet=CANARY_TARGET_SHEET,
    )
    validate_target_binding(
        binding,
        ReadOnlySheetsTargetInspector(db).inspect(CANARY_TARGET_SHEET),
    )
    observations = SheetsCanonicalIdentityReader(db).read_identities(identities)
    for transaction in transactions:
        observation = observations.get(transaction.identity)
        if observation is None or not observation.readable:
            raise RuntimeError("bank_loan_preview_identity_readback_unavailable")
        if observation.records:
            exact = (
                len(observation.records) == 1
                and observation.records[0]
                == ExistingCanonicalRecord.from_candidate(transaction)
            )
            raise RuntimeError(
                "bank_loan_preview_existing_identity_duplicate"
                if exact else "bank_loan_preview_existing_identity_collision"
            )
    categories = set(db.categories())
    if LOAN_EXPENSE_CATEGORY not in categories:
        raise RuntimeError("bank_loan_preview_housing_category_unavailable")
    rows = tuple(
        materialize_import_row(
            transaction,
            imported_at=imported_at,
            status="bank_loan_repayment",
        )
        for transaction in transactions
    )
    if len(rows) != expected_rows or any(
        len(row) != len(HEADERS[CANARY_TARGET_SHEET]) for row in rows
    ):
        raise RuntimeError("bank_loan_preview_row_schema_invalid")
    return BankLoanPreviewResult(
        selected=expected_rows,
        planned=expected_rows,
        write_eligible=expected_rows,
        canonical_expense_rows=expected_rows,
        existing_duplicate=0,
        ambiguous_collision=0,
        target_binding_valid=True,
        target_header_valid=True,
        category_authority_valid=True,
        category_major=LOAN_EXPENSE_CATEGORY[0],
        category_minor=LOAN_EXPENSE_CATEGORY[1],
    )


class BankCanaryPreparationPipeline:
    """Build and preflight a one-row bank canary using read-only Sheets state."""

    def __init__(self, db: SheetsDB):
        self.db = db

    def _context(
        self,
        path: str | Path,
        *,
        account_alias: str,
        confirmed_internal_transfers: ConfirmedInternalTransfers,
        card_statement_authorities=(),
    ) -> tuple[BankShadowResult, BankPreviewPlan]:
        existing_rows = self.db.get("取込データ!A2:L")
        existing = parse_import_rows(existing_rows)
        parsed = BankPdfPipeline().parse(
            path,
            account_alias=account_alias,
            existing_identities={row.import_id for row in existing},
        )
        shadow = build_bank_shadow_result(
            parsed,
            existing,
            confirmed_internal_transfers=confirmed_internal_transfers,
            card_statement_authorities=tuple(card_statement_authorities),
        )
        return shadow, build_bank_preview_plan(shadow, existing)

    def loan_dry_run(
        self,
        path: str | Path,
        *,
        imported_at,
        expected_rows: int = 4,
        account_alias: str = DEFAULT_ACCOUNT_ALIAS,
        confirmed_internal_transfers=frozenset(),
        card_statement_authorities=(),
    ) -> dict:
        shadow, preview = self._context(
            path,
            account_alias=account_alias,
            confirmed_internal_transfers=confirmed_internal_transfers,
            card_statement_authorities=card_statement_authorities,
        )
        result = preview_bank_loan_repayments(
            shadow, preview, self.db,
            imported_at=imported_at,
            expected_rows=expected_rows,
        ).summary()
        result.update({
            "parsed": preview.parsed,
            "existing_bank_duplicates": preview.existing_duplicate,
            "new_plan_candidates": preview.new_plan_candidates,
            "withheld_by_classification": preview.withheld_by_classification,
        })
        return result

    def loan_candidate_identities(
        self,
        path: str | Path,
        *,
        account_alias: str = DEFAULT_ACCOUNT_ALIAS,
        confirmed_internal_transfers: ConfirmedInternalTransfers = frozenset(),
        card_statement_authorities=(),
    ) -> tuple[str, ...]:
        """Discover the current four loans only for one-time manifest freezing."""
        shadow, preview = self._context(
            path,
            account_alias=account_alias,
            confirmed_internal_transfers=confirmed_internal_transfers,
            card_statement_authorities=card_statement_authorities,
        )
        identities = tuple(
            decision.classification.transaction.source_row_identity
            for decision in shadow.decisions
            if decision.classification.classification == LOAN_CLASSIFICATION
        )
        if len(identities) != BANK_LOAN_ROWS or len(set(identities)) != BANK_LOAN_ROWS:
            raise RuntimeError("bank_loan_manifest_requires_exactly_four_identities")
        if any(preview.candidate_identities.count(identity) != 1 for identity in identities):
            raise RuntimeError("bank_loan_manifest_duplicate_or_collision")
        return identities

    def loan_batch_dry_run(
        self,
        path: str | Path,
        *,
        selected_source_identities: tuple[str, ...],
        imported_at,
        account_alias: str = DEFAULT_ACCOUNT_ALIAS,
        confirmed_internal_transfers: ConfirmedInternalTransfers = frozenset(),
        card_statement_authorities=(),
    ) -> dict:
        """Preflight the exact private four-loan manifest without write authority."""
        shadow, preview = self._context(
            path,
            account_alias=account_alias,
            confirmed_internal_transfers=confirmed_internal_transfers,
            card_statement_authorities=card_statement_authorities,
        )
        authority = BankBatchAuthority(
            selected_source_identities=tuple(selected_source_identities),
            target_spreadsheet_id=str(self.db.sid),
            min_rows=BANK_LOAN_ROWS,
            max_rows=BANK_LOAN_ROWS,
            authority_mode="bank_loan_repayment_preparation",
        )
        plan = build_bank_batch_plan(shadow, preview, authority)
        if LOAN_EXPENSE_CATEGORY not in set(self.db.categories()):
            raise RuntimeError("bank_loan_preview_housing_category_unavailable")
        result = dry_run_bank_batch(plan, self.db, imported_at=imported_at)
        summary = result.summary()
        summary.update({
            "classification": LOAN_CLASSIFICATION,
            "projected_classification": "expense",
            "expense_category": {
                "major": LOAN_EXPENSE_CATEGORY[0],
                "minor": LOAN_EXPENSE_CATEGORY[1],
            },
            "category_authority_valid": True,
            "parsed": preview.parsed,
            "existing_bank_duplicates": preview.existing_duplicate,
            "new_plan_candidates": preview.new_plan_candidates,
            "withheld_by_classification": preview.withheld_by_classification,
        })
        return summary

    def candidate_identities(
        self,
        path: str | Path,
        *,
        account_alias: str = DEFAULT_ACCOUNT_ALIAS,
        confirmed_internal_transfers: ConfirmedInternalTransfers = frozenset(),
    ) -> dict:
        _, preview = self._context(
            path,
            account_alias=account_alias,
            confirmed_internal_transfers=confirmed_internal_transfers,
        )
        return {
            "selector": "stable_source_identity",
            "candidate_identity_count": len(preview.candidate_identities),
            "candidate_identities": list(preview.candidate_identities),
            "external_write_count": 0,
        }

    def dry_run(
        self,
        path: str | Path,
        *,
        selected_source_identity: str,
        imported_at,
        account_alias: str = DEFAULT_ACCOUNT_ALIAS,
        confirmed_internal_transfers: ConfirmedInternalTransfers = frozenset(),
    ) -> dict:
        shadow, preview = self._context(
            path,
            account_alias=account_alias,
            confirmed_internal_transfers=confirmed_internal_transfers,
        )
        authority = BankCanaryAuthority(
            selected_source_identity=selected_source_identity,
            target_spreadsheet_id=str(self.db.sid),
        )
        plan = build_bank_canary_plan(shadow, preview, authority)
        result = dry_run_bank_canary(plan, self.db, imported_at=imported_at)
        summary = result.summary()
        summary.update({
            "parsed": preview.parsed,
            "eligible_before_dedupe": preview.eligible_before_dedupe,
            "new_plan_candidates": preview.new_plan_candidates,
            "withheld_by_classification": preview.withheld_by_classification,
        })
        return summary

    def batch_dry_run(
        self,
        path: str | Path,
        *,
        selected_source_identities: tuple[str, ...],
        imported_at,
        account_alias: str = DEFAULT_ACCOUNT_ALIAS,
        confirmed_internal_transfers: ConfirmedInternalTransfers = frozenset(),
    ) -> dict:
        """Prepare exactly five explicit identities and stop before any writer."""
        shadow, preview = self._context(
            path,
            account_alias=account_alias,
            confirmed_internal_transfers=confirmed_internal_transfers,
        )
        authority = BankBatchAuthority(
            selected_source_identities=tuple(selected_source_identities),
            target_spreadsheet_id=str(self.db.sid),
        )
        plan = build_bank_batch_plan(shadow, preview, authority)
        result = dry_run_bank_batch(plan, self.db, imported_at=imported_at)
        summary = result.summary()
        summary.update({
            "parsed": preview.parsed,
            "remaining_eligible_new_candidates": preview.new_plan_candidates,
            "eligible_before_dedupe": preview.eligible_before_dedupe,
            "withheld_by_classification": preview.withheld_by_classification,
        })
        return summary

    def replay_status(
        self,
        path: str | Path,
        *,
        selected_source_identity: str,
        account_alias: str = DEFAULT_ACCOUNT_ALIAS,
        confirmed_internal_transfers: ConfirmedInternalTransfers = frozenset(),
    ) -> dict:
        """Confirm the selected identity is suppressed without exposing row data."""
        shadow, preview = self._context(
            path,
            account_alias=account_alias,
            confirmed_internal_transfers=confirmed_internal_transfers,
        )
        matches = _matching_decisions(shadow, selected_source_identity)
        if len(matches) != 1:
            raise RuntimeError("bank_canary_replay_selector_not_unique")
        existing = parse_import_rows(self.db.get("取込データ!A2:L"))
        selected_existing = sum(
            row.import_id == selected_source_identity for row in existing
        )
        return {
            "parsed": preview.parsed,
            "existing_duplicate": preview.existing_duplicate,
            "new_plan_candidates": preview.new_plan_candidates,
            "selected_existing_duplicate": selected_existing,
            "selected_new_plan_candidate": preview.candidate_identities.count(
                selected_source_identity,
            ),
            "write_attempted": 0,
        }

    def batch_replay_status(
        self,
        path: str | Path,
        *,
        selected_source_identities: tuple[str, ...],
        account_alias: str = DEFAULT_ACCOUNT_ALIAS,
        confirmed_internal_transfers: ConfirmedInternalTransfers = frozenset(),
    ) -> dict:
        """Verify all five exact identities are suppressed, returning counts only."""
        selected = tuple(selected_source_identities)
        selected_count = len(selected)
        authority = BankBatchAuthority(
            selected_source_identities=selected,
            target_spreadsheet_id=str(self.db.sid),
            min_rows=selected_count,
            max_rows=selected_count,
            authority_mode={
                BANK_LOAN_ROWS: "bank_loan_repayment_preparation",
                BANK_BATCH_ROWS: "bank_batch_preparation",
                BANK_INITIAL_BACKFILL_ROWS: "bank_initial_backfill",
            }.get(selected_count, "invalid"),
        )
        authority.validate()
        shadow, preview = self._context(
            path,
            account_alias=account_alias,
            confirmed_internal_transfers=confirmed_internal_transfers,
        )
        if any(len(_matching_decisions(shadow, identity)) != 1 for identity in selected):
            raise RuntimeError("bank_batch_replay_selector_not_unique")
        existing = parse_import_rows(self.db.get("取込データ!A2:L"))
        existing_ids = tuple(row.import_id for row in existing)
        return {
            "parsed": preview.parsed,
            "existing_duplicate": preview.existing_duplicate,
            "new_plan_candidates": preview.new_plan_candidates,
            "withheld_by_classification": preview.withheld_by_classification,
            "ambiguous_collision": preview.ambiguous_collision,
            "selected_existing_duplicate": sum(
                existing_ids.count(identity) for identity in selected
            ),
            "selected_new_plan_candidate": sum(
                preview.candidate_identities.count(identity) for identity in selected
            ),
            "write_attempted": 0,
        }
