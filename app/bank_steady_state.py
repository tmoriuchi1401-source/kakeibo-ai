"""Daily bank-PDF preview and exact external selection manifest."""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
from pathlib import Path
from typing import Iterable

from .bank_pdf_pipeline import BankPdfPipeline
from .bank_income import deposit_decisions, income_summary
from .bank_reconciliation import (
    BankPreviewPlan,
    BankShadowResult,
    ConfirmedInternalTransfers,
    ConfirmedNonOwnClassifications,
    build_bank_preview_plan,
    build_bank_shadow_result,
)
from .reconciliation import parse_import_rows


STEADY_STATE_MAX_ROWS = 100
STEADY_STATE_TARGET_SHEET = "取込データ"
MANIFEST_SCHEMA_VERSION = 1


def pdf_digest(path: str | Path) -> str:
    return hashlib.sha256(Path(path).expanduser().resolve().read_bytes()).hexdigest()


@dataclass(frozen=True)
class BankDailyPreview:
    summary: dict
    candidate_identities: tuple[str, ...]
    pdf_sha256: str
    target_spreadsheet_id: str
    expected_git_head: str
    expense_candidate_identities: tuple[str, ...] = ()


def _daily_summary(
    shadow: BankShadowResult,
    plan: BankPreviewPlan,
    *,
    pdf_sha256: str,
    target_spreadsheet_id: str,
    expected_git_head: str,
) -> dict:
    new_by_classification = Counter()
    for decision in shadow.decisions:
        identity = decision.classification.transaction.source_row_identity
        if identity in plan.candidate_identities:
            new_by_classification[decision.classification.classification] += 1
    review_reasons = Counter(
        decision.classification.reason
        for decision in shadow.decisions
        if decision.classification.classification == "needs_review"
    )
    classification = Counter(
        decision.classification.classification for decision in shadow.decisions
    )
    return {
        "read_only": True,
        "parsed": plan.parsed,
        "existing_duplicate": plan.existing_duplicate,
        "new_income": new_by_classification["income"],
        "new_expense": new_by_classification["expense"],
        "new_loan_repayment": new_by_classification["loan_repayment"],
        "card_settlement_suppressed": classification["card_settlement"],
        "own_transfer_suppressed": classification["transfer"],
        "cash_withdrawal_suppressed": classification["cash_withdrawal"],
        "reimbursement_suppressed": classification["reimbursement"],
        "operator_confirmed_non_own_review": review_reasons[
            "operator_confirmed_non_own_review"
        ],
        "true_unknown": sum(
            count for reason, count in review_reasons.items()
            if reason != "operator_confirmed_non_own_review"
        ),
        "collision": plan.ambiguous_collision,
        "total_proposed_writes": plan.new_plan_candidates,
        "write_attempted": 0,
        "candidate_count": len(plan.candidate_identities),
        "pdf_sha256": pdf_sha256,
        "target_spreadsheet_id": target_spreadsheet_id,
        "expected_git_head": expected_git_head,
    }


def build_bank_daily_preview(
    db,
    path: str | Path,
    *,
    target_spreadsheet_id: str,
    expected_git_head: str,
    account_alias: str | None = None,
    confirmed_internal_transfers: ConfirmedInternalTransfers = frozenset(),
    confirmed_non_own_classifications: ConfirmedNonOwnClassifications = frozenset(),
    card_statement_authorities: Iterable = (),
) -> BankDailyPreview:
    existing = parse_import_rows(db.get(f"{STEADY_STATE_TARGET_SHEET}!A2:L"))
    parsed = BankPdfPipeline().parse(
        path,
        account_alias=account_alias,
        existing_identities={row.import_id for row in existing},
    )
    shadow = build_bank_shadow_result(
        parsed,
        existing,
        confirmed_internal_transfers=confirmed_internal_transfers,
        confirmed_non_own_classifications=confirmed_non_own_classifications,
        card_statement_authorities=tuple(card_statement_authorities),
    )
    plan = build_bank_preview_plan(shadow, existing)
    if len(plan.candidate_identities) > STEADY_STATE_MAX_ROWS:
        raise RuntimeError("bank_steady_state_candidate_upper_bound_exceeded")
    expense_candidate_identities = tuple(sorted(
        decision.classification.transaction.source_row_identity
        for decision in shadow.decisions
        if (
            decision.classification.transaction.source_row_identity
            in plan.candidate_identities
            and decision.classification.classification == "expense"
            and decision.write_eligibility == "preview_candidate"
        )
    ))
    income_decisions, income_duplicates = deposit_decisions(
        parsed.transactions,
        confirmed_internal_transfers=confirmed_internal_transfers,
        confirmed_non_own_classifications=confirmed_non_own_classifications,
    )
    summary = _daily_summary(
        shadow,
        plan,
        pdf_sha256=pdf_digest(path),
        target_spreadsheet_id=target_spreadsheet_id,
        expected_git_head=expected_git_head,
    )
    # Independent, read-only household-income assessment of new PDF deposits.
    # Existing import/expense eligibility, manifests and processed flags stay unchanged.
    summary["household_income"] = income_summary(income_decisions, income_duplicates)
    existing_import_ids = {row.import_id for row in existing}
    summary["household_income"]["confirmed_import_candidates"] = sum(
        decision.outcome == "confirmed_income"
        and decision.transaction.source_row_identity not in existing_import_ids
        for decision in income_decisions
    )
    return BankDailyPreview(
        summary=summary,
        candidate_identities=plan.candidate_identities,
        pdf_sha256=pdf_digest(path),
        target_spreadsheet_id=target_spreadsheet_id,
        expected_git_head=expected_git_head,
        expense_candidate_identities=expense_candidate_identities,
    )


@dataclass(frozen=True)
class BankDailySelectionManifest:
    pdf_sha256: str
    source_identities: tuple[str, ...]
    target_spreadsheet_id: str
    expected_git_head: str
    target_worksheet: str = STEADY_STATE_TARGET_SHEET
    schema_version: int = MANIFEST_SCHEMA_VERSION

    def validate(self) -> None:
        if self.schema_version != MANIFEST_SCHEMA_VERSION:
            raise RuntimeError("bank_steady_state_manifest_schema_invalid")
        if len(self.pdf_sha256) != 64 or any(
            char not in "0123456789abcdef" for char in self.pdf_sha256
        ):
            raise RuntimeError("bank_steady_state_manifest_pdf_digest_invalid")
        if not self.source_identities or len(self.source_identities) > STEADY_STATE_MAX_ROWS:
            raise RuntimeError("bank_steady_state_manifest_row_bound_invalid")
        if len(set(self.source_identities)) != len(self.source_identities):
            raise RuntimeError("bank_steady_state_manifest_duplicate_identity")
        if any(not value or len(value) > 256 or any(
            ord(char) < 32 or char.isspace() for char in value
        ) for value in self.source_identities):
            raise RuntimeError("bank_steady_state_manifest_identity_invalid")
        if not self.target_spreadsheet_id:
            raise RuntimeError("bank_steady_state_manifest_target_invalid")
        if len(self.expected_git_head) != 40 or any(
            char not in "0123456789abcdef" for char in self.expected_git_head
        ):
            raise RuntimeError("bank_steady_state_manifest_git_head_invalid")
        if self.target_worksheet != STEADY_STATE_TARGET_SHEET:
            raise RuntimeError("bank_steady_state_manifest_sheet_invalid")


def manifest_path(state_dir: str | Path) -> Path:
    path = Path(state_dir).expanduser().resolve()
    return path / "bank-steady-state-selection.json"


def freeze_manifest(
    path: str | Path,
    preview: BankDailyPreview,
    *,
    repository_root: str | Path,
) -> BankDailySelectionManifest:
    destination = Path(path).expanduser().resolve()
    repo = Path(repository_root).resolve()
    try:
        destination.relative_to(repo)
    except ValueError:
        pass
    else:
        raise RuntimeError("bank_steady_state_manifest_must_be_outside_repository")
    manifest = BankDailySelectionManifest(
        pdf_sha256=preview.pdf_sha256,
        source_identities=preview.candidate_identities,
        target_spreadsheet_id=preview.target_spreadsheet_id,
        expected_git_head=preview.expected_git_head,
    )
    manifest.validate()
    if destination.exists():
        current = load_manifest(destination)
        if current != manifest:
            raise RuntimeError("bank_steady_state_manifest_changed_repreview_required")
        return current
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("x", encoding="utf-8") as handle:
        json.dump({
            "schema_version": manifest.schema_version,
            "pdf_sha256": manifest.pdf_sha256,
            "source_identities": manifest.source_identities,
            "target_spreadsheet_id": manifest.target_spreadsheet_id,
            "target_worksheet": manifest.target_worksheet,
            "expected_git_head": manifest.expected_git_head,
        }, handle, sort_keys=True)
    return manifest


def load_manifest(path: str | Path) -> BankDailySelectionManifest:
    values = json.loads(Path(path).expanduser().resolve().read_text(encoding="utf-8"))
    manifest = BankDailySelectionManifest(
        pdf_sha256=str(values.get("pdf_sha256", "")),
        source_identities=tuple(values.get("source_identities", ())),
        target_spreadsheet_id=str(values.get("target_spreadsheet_id", "")),
        expected_git_head=str(values.get("expected_git_head", "")),
        target_worksheet=str(values.get("target_worksheet", "")),
        schema_version=int(values.get("schema_version", 0)),
    )
    manifest.validate()
    return manifest
