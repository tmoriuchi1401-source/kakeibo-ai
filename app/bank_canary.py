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
CANARY_CLASSIFICATIONS = frozenset({"income", "expense"})
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
    """Explicit, read-only authority envelope for exactly five identities."""

    selected_source_identities: tuple[str, ...]
    target_spreadsheet_id: str = field(repr=False)
    target_worksheet: str = CANARY_TARGET_SHEET
    min_rows: int = BANK_BATCH_ROWS
    max_rows: int = BANK_BATCH_ROWS
    authority_mode: str = "bank_batch_preparation"

    def validate(self) -> None:
        if len(self.selected_source_identities) != BANK_BATCH_ROWS:
            raise RuntimeError("bank_batch_requires_exactly_five_identities")
        if len(set(self.selected_source_identities)) != BANK_BATCH_ROWS:
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
        if self.min_rows != BANK_BATCH_ROWS or self.max_rows != BANK_BATCH_ROWS:
            raise RuntimeError("bank_batch_rows_bound_must_be_five")
        if self.authority_mode != "bank_batch_preparation":
            raise RuntimeError("bank_batch_production_authority_forbidden")


@dataclass(frozen=True)
class BankBatchItem:
    transaction: Transaction = field(repr=False)
    classification: str
    write_eligibility: str
    import_status: str


@dataclass(frozen=True, init=False)
class BankBatchPlan:
    """Projection-only five-row plan; it never carries write capability."""

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
        if len(rows) != BANK_BATCH_ROWS or any(
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
    write_attempted: int = 0
    external_write_count: int = 0

    def summary(self) -> dict:
        return {
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
    """Bind five explicit stable identities to eligible canonical projections."""
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
        if classification not in CANARY_CLASSIFICATIONS:
            raise RuntimeError("bank_batch_classification_withheld")
        if decision.write_eligibility != "preview_candidate":
            raise RuntimeError("bank_batch_write_eligibility_withheld")
        if preview.candidate_identities.count(identity) != 1:
            raise RuntimeError("bank_batch_candidate_duplicate_or_collision")
        transaction = decision.classification.transaction.to_canonical()
        if transaction.identity != identity:
            raise RuntimeError("bank_batch_source_identity_changed")
        _validate_bank_transaction_semantics(transaction, classification)
        items.append(BankBatchItem(
            transaction=transaction,
            classification=classification,
            write_eligibility="eligible",
            import_status=f"bank_{classification}",
        ))
    plan = BankBatchPlan._create(
        authority_token=_BANK_BATCH_PLAN_AUTHORITY,
        authority=authority,
        items=tuple(items),
    )
    return validate_bank_batch_plan(plan)


def validate_bank_batch_plan(value: object) -> BankBatchPlan:
    if type(value) is not BankBatchPlan:
        raise TypeError("bank_batch_plan_required")
    if getattr(value, "_authority", None) is not _BANK_BATCH_PLAN_AUTHORITY:
        raise TypeError("unauthorized_bank_batch_plan")
    value.authority.validate()
    if value.planned_rows != BANK_BATCH_ROWS or value.authorized_rows != BANK_BATCH_ROWS:
        raise RuntimeError("bank_batch_requires_exactly_five_rows")
    if len(value.items) != BANK_BATCH_ROWS:
        raise RuntimeError("bank_batch_requires_exactly_five_items")
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
    for item in value.items:
        if item.classification not in CANARY_CLASSIFICATIONS:
            raise RuntimeError("bank_batch_classification_withheld")
        if item.write_eligibility != "eligible":
            raise RuntimeError("bank_batch_write_eligibility_withheld")
        if item.import_status != f"bank_{item.classification}":
            raise RuntimeError("bank_batch_import_status_invalid")
        _validate_bank_transaction_semantics(item.transaction, item.classification)
    return value


def dry_run_bank_batch(
    plan: BankBatchPlan,
    db: SheetsDB,
    *,
    imported_at,
) -> BankBatchDryRunResult:
    """Perform complete five-item pre-read and row projection without a writer."""
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
    rows = plan.materialize(imported_at=imported_at)
    if len(rows) != BANK_BATCH_ROWS or any(
        len(row) != len(HEADERS[CANARY_TARGET_SHEET]) for row in rows
    ):
        raise RuntimeError("bank_batch_row_schema_invalid")
    income = sum(item.classification == "income" for item in plan.items)
    expense = sum(item.classification == "expense" for item in plan.items)
    return BankBatchDryRunResult(
        authority_mode=plan.authority.authority_mode,
        selected=BANK_BATCH_ROWS,
        planned=BANK_BATCH_ROWS,
        authorized=BANK_BATCH_ROWS,
        withheld=0,
        existing_duplicate=0,
        ambiguous_collision=0,
        target_binding_valid=True,
        target_header_valid=True,
        selected_identities_absent=True,
        max_writes=plan.authority.max_rows,
        income=income,
        expense=expense,
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


class BankCanaryPreparationPipeline:
    """Build and preflight a one-row bank canary using read-only Sheets state."""

    def __init__(self, db: SheetsDB):
        self.db = db

    def _context(
        self,
        path: str | Path,
        *,
        account_alias: str,
        confirmed_internal_transfers: frozenset[tuple[str, str]],
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
        )
        return shadow, build_bank_preview_plan(shadow, existing)

    def candidate_identities(
        self,
        path: str | Path,
        *,
        account_alias: str = DEFAULT_ACCOUNT_ALIAS,
        confirmed_internal_transfers: frozenset[tuple[str, str]] = frozenset(),
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
        confirmed_internal_transfers: frozenset[tuple[str, str]] = frozenset(),
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
        confirmed_internal_transfers: frozenset[tuple[str, str]] = frozenset(),
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
        confirmed_internal_transfers: frozenset[tuple[str, str]] = frozenset(),
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
