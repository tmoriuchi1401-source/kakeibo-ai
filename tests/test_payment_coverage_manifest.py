from datetime import date
import sys

import pytest

from app import cli
from app.paypay_evidence_bundle import EvidenceVerificationResult
from app.payment_coverage_manifest import (
    CoverageManifest,
    classify_evidence,
    csv_manifest,
    gmail_manifest,
    manifest_for_required_window,
    preview_payment_coverage_manifests,
)


PAYPAY_HEADER = (
    "取引日,出金金額（円）,入金金額（円）,海外出金金額,通貨,変換レート（円）,利用国,"
    "取引内容,取引先,取引方法,支払い区分,利用者,取引番号\n"
)


def _paypay(tmp_path, name="paypay_202608.csv", tx="TX-1"):
    path = tmp_path / name
    path.write_text(
        PAYPAY_HEADER
        + f'2026/08/01 12:34,"1,200",,,,,,支払い,テスト店,PayPay残高,一回払い,本人,{tx}\n',
        encoding="utf-8-sig",
    )
    return path


def _card(tmp_path, name="auPAY_Card_202608.csv", amount="1200"):
    path = tmp_path / name
    text = (
        "請求情報\n"
        "利用日,利用店舗,利用額（円）,支払い区分,ご利用者,摘要\n"
        f"2026/08/01,テスト店,{amount},1回払い,本人,\n"
    )
    path.write_text(text, encoding="cp932")
    return path


def _captured(start="2026-08-01", end="2026-08-31"):
    return EvidenceVerificationResult(
        accepted=True, reason="captured_not_provider_verified",
        trust_tier="captured", evidence_id="a" * 64,
        requested_start=start, requested_end=end,
        evidence_id_valid=True, csv_hash_valid=True,
        status_image_hash_valid=True, signature_valid=True,
        candidate_complete=True, completeness_proven=False,
    )


def test_no_evidence_is_unknown():
    result = preview_payment_coverage_manifests()
    assert result["unknown_count"] == 4
    assert all(row["completion_status"] == "unknown" for row in result["manifests"])


def test_period_without_full_export_proof_is_unknown(tmp_path):
    manifest = csv_manifest(_paypay(tmp_path), "paypay")
    assert manifest.coverage_start == manifest.coverage_end == "2026-08-01"
    assert manifest.completion_status == "unknown"
    assert manifest.candidate_complete is True
    assert manifest.completeness_reason == "export_scope_not_proven"


def test_outside_required_window_is_incomplete(tmp_path):
    path = _paypay(tmp_path)
    manifest = csv_manifest(path, "paypay", paypay_evidence_verification=_captured())
    assert manifest_for_required_window(
        manifest, date(2026, 7, 31), date(2026, 8, 2),
        coverage_basis="transaction_date",
    ) == "incomplete"


def test_same_period_and_hash_is_duplicate(tmp_path):
    path = _paypay(tmp_path)
    manifests, duplicates, conflicts = classify_evidence([
        csv_manifest(path, "paypay"), csv_manifest(path, "paypay"),
    ])
    assert (duplicates, conflicts) == (1, 0)
    assert manifests[1].completeness_reason == "duplicate_evidence"


def test_same_period_with_different_hash_is_conflict(tmp_path):
    first = csv_manifest(_paypay(tmp_path, "first.csv", "TX-1"), "paypay")
    second = csv_manifest(_paypay(tmp_path, "second.csv", "TX-2"), "paypay")
    manifests, duplicates, conflicts = classify_evidence([first, second])
    assert (duplicates, conflicts) == (0, 1)
    assert all(item.completeness_reason == "conflicting_evidence" for item in manifests)


def test_parse_error_cannot_be_complete(tmp_path):
    path = tmp_path / "bad.csv"
    path.write_text("not,a,paypay,file\n", encoding="utf-8")
    manifest = csv_manifest(path, "paypay", paypay_evidence_verification=_captured())
    assert manifest.parse_error
    assert manifest.completion_status == "unknown"
    assert manifest.completeness_proven is False


def test_captured_evidence_never_completes_manifest(tmp_path):
    path = _paypay(tmp_path)
    manifest = csv_manifest(path, "paypay", paypay_evidence_verification=_captured())
    assert manifest.completion_status == "unknown"
    assert manifest.completeness_proven is False
    assert manifest.candidate_complete is True
    assert manifest.coverage_start == "2026-08-01"
    assert manifest.coverage_end == "2026-08-31"


def test_captured_full_window_is_not_usable_for_complete(tmp_path):
    path = _paypay(tmp_path)
    manifest = csv_manifest(path, "paypay", paypay_evidence_verification=_captured())
    assert manifest_for_required_window(
        manifest, date(2026, 8, 1), date(2026, 8, 1),
        coverage_basis="transaction_date",
    ) == "unknown"
    assert manifest_for_required_window(
        manifest, date(2026, 8, 1), date(2026, 9, 1),
        coverage_basis="transaction_date",
    ) == "incomplete"


def test_transaction_rows_do_not_shrink_captured_export_scope(tmp_path):
    path = _paypay(tmp_path)
    manifest = csv_manifest(path, "paypay", paypay_evidence_verification=_captured())
    assert (manifest.coverage_start, manifest.coverage_end) == (
        "2026-08-01", "2026-08-31",
    )
    assert manifest.row_count == 1


def test_zero_transaction_export_can_be_candidate_with_captured_evidence(tmp_path):
    path = tmp_path / "Transactions_20260801-20260831.csv"
    path.write_text(PAYPAY_HEADER, encoding="utf-8-sig")
    manifest = csv_manifest(path, "paypay", paypay_evidence_verification=_captured())
    assert manifest.row_count == 0
    assert manifest.completion_status == "unknown"
    assert manifest.candidate_complete is True


def test_rejected_evidence_does_not_supply_scope(tmp_path):
    path = _paypay(tmp_path)
    rejected = EvidenceVerificationResult(False, "signature_invalid")
    manifest = csv_manifest(
        path, "paypay", paypay_evidence_verification=rejected,
    )
    assert manifest.completion_status == "unknown"
    assert manifest.completeness_reason == "signature_invalid"
    assert manifest.coverage_start == manifest.coverage_end == "2026-08-01"


def test_observed_row_outside_claimed_scope_is_rejected(tmp_path):
    path = _paypay(tmp_path)
    manifest = csv_manifest(
        path, "paypay",
        paypay_evidence_verification=_captured("2026-08-02", "2026-08-31"),
    )
    assert manifest.completion_status == "unknown"
    assert manifest.completeness_reason == "observed_transaction_outside_export_scope"


def test_same_captured_scope_reexport_duplicate_and_conflict(tmp_path):
    first = _paypay(tmp_path, "first.csv", "TX-1")
    same = tmp_path / "same.csv"
    same.write_bytes(first.read_bytes())
    second = _paypay(tmp_path, "second.csv", "TX-2")
    manifests, duplicates, conflicts = classify_evidence([
        csv_manifest(first, "paypay", paypay_evidence_verification=_captured()),
        csv_manifest(same, "paypay", paypay_evidence_verification=_captured()),
    ])
    assert (duplicates, conflicts) == (1, 0)
    manifests, duplicates, conflicts = classify_evidence([
        csv_manifest(first, "paypay", paypay_evidence_verification=_captured()),
        csv_manifest(second, "paypay", paypay_evidence_verification=_captured()),
    ])
    assert (duplicates, conflicts) == (0, 1)
    assert all(item.completion_status == "unknown" for item in manifests)


def test_paypay_and_card_coverage_basis_are_not_interchangeable(tmp_path):
    paypay = csv_manifest(_paypay(tmp_path), "paypay")
    card = csv_manifest(_card(tmp_path), "au_pay_card")
    assert paypay.coverage_basis == "transaction_date"
    assert card.coverage_basis == "billing_cycle"
    assert card.coverage_start is None and card.coverage_end is None
    assert card.source_period_label == "2026-08"
    assert manifest_for_required_window(
        card, date(2026, 8, 1), date(2026, 8, 1),
        coverage_basis="transaction_date",
    ) == "unknown"


@pytest.mark.parametrize("source", ["amazon_gmail", "au_pay_gmail"])
def test_gmail_sources_cannot_be_complete_under_current_conditions(source):
    manifest = gmail_manifest(source)
    assert manifest.completion_status == "unknown"
    assert manifest.completeness_proven is False
    assert "pagination" in manifest.completeness_reason


def test_preview_and_cli_are_local_read_only(monkeypatch, capsys):
    result = preview_payment_coverage_manifests()
    assert result["read_only"] is True
    monkeypatch.setattr(sys, "argv", ["kakeibo", "payment-coverage-manifest-preview"])
    monkeypatch.setattr(
        cli, "Settings", lambda: pytest.fail("credentials must not be loaded"),
    )
    cli.main()
    assert "'read_only': True" in capsys.readouterr().out


def test_model_rejects_unproven_complete():
    with pytest.raises(ValueError, match="completeness_proven"):
        CoverageManifest(source="paypay", completion_status="complete")
