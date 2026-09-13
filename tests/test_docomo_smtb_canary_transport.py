from dataclasses import fields
from datetime import datetime, timezone

import pytest

from app.aupay_card_writer import (
    PersistentAuditKey,
    TargetBinding,
    canonical_candidate_reference_v2,
)
from app.bank_canary import BankBatchAuthority, build_bank_batch_plan
from app.bank_canary_production import (
    project_bank_bounded_batch as production_project_bank_bounded_batch,
)
from app.bank_pdf_pipeline import (
    DOCOMO_SMTB_SOURCE,
    BankPdfResult,
    NormalizedBankTransaction,
)
from app.bank_reconciliation import (
    ASSET_FORMATION_CATEGORY,
    build_bank_preview_plan,
    build_bank_shadow_result,
)
from app.canonical_one_row_production import (
    create_canonical_five_row_manifest,
    project_bank_bounded_batch,
)


NOW = datetime(2026, 9, 13, 12, 0, tzinfo=timezone.utc)
KEY = PersistentAuditKey("docomo-smtb-canary-test", b"d" * 32)


def _plan():
    transaction = NormalizedBankTransaction(
        source=DOCOMO_SMTB_SOURCE,
        account_alias="docomo-smtb-primary",
        transaction_date="2026-09-01",
        description="SBIハイブリッド預金",
        signed_amount=-1000,
        source_page=1,
        source_row=1,
        source_row_identity="bankpdf:docomo-smtb:test:asset-formation",
        source_row_hash="a" * 64,
        transaction_kind="withdrawal",
    )
    parsed = BankPdfResult(
        pages=1,
        candidate_rows=1,
        transactions=(transaction,),
        issues=(),
        balance_consistency_failures=0,
        duplicate_candidates=0,
        canonical_transactions=(transaction.to_canonical(),),
    )
    authority = frozenset({(
        "SBIハイブリッド預金",
        "outgoing",
        "docomo-smtb-primary",
        "expense",
    )})
    shadow = build_bank_shadow_result(
        parsed, [], confirmed_non_own_classifications=authority,
    )
    preview = build_bank_preview_plan(shadow, [])
    return build_bank_batch_plan(
        shadow,
        preview,
        BankBatchAuthority(
            selected_source_identities=(transaction.source_row_identity,),
            target_spreadsheet_id="sheet-id",
            min_rows=1,
            max_rows=1,
            authority_mode="bank_steady_state",
        ),
    )


def _copy_frozen(value, **changes):
    clone = object.__new__(type(value))
    for item in fields(value):
        object.__setattr__(
            clone, item.name, changes.get(item.name, getattr(value, item.name)),
        )
    return clone


def test_docomo_sbi_canary_binds_asset_formation_policy_to_approval_reference():
    batch = production_project_bank_bounded_batch(_plan())
    candidate = batch.candidates[0]

    assert candidate.transaction_kind == "expense"
    assert candidate.amount_yen < 0
    assert candidate.category == ASSET_FORMATION_CATEGORY
    assert candidate.write_eligibility == "eligible"

    baseline = canonical_candidate_reference_v2(candidate, KEY)
    changed = _copy_frozen(candidate, category=("", ""))
    assert canonical_candidate_reference_v2(changed, KEY) != baseline


def test_docomo_sbi_canary_manifest_accepts_only_supported_production_branch():
    batch = project_bank_bounded_batch(_plan())
    binding = TargetBinding(expected_spreadsheet_id="sheet-id")

    manifest = create_canonical_five_row_manifest(
        batch,
        binding=binding,
        audit_key=KEY,
        expected_git_head="a" * 40,
        expected_branch="agent/bank-pdf-docomo-smtb",
        created_at=NOW,
        run_id="11111111-1111-4111-8111-111111111111",
    )
    assert manifest.max_rows == 1
    assert manifest.expected_branch == "agent/bank-pdf-docomo-smtb"

    with pytest.raises(RuntimeError, match="branch_invalid"):
        create_canonical_five_row_manifest(
            batch,
            binding=binding,
            audit_key=KEY,
            expected_git_head="a" * 40,
            expected_branch="agent/unrelated",
            created_at=NOW,
            run_id="22222222-2222-4222-8222-222222222222",
        )
