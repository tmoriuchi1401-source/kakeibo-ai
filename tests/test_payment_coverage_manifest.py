from datetime import date
import sys

import pytest

from app import cli
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
    manifest = csv_manifest(_paypay(tmp_path), "paypay", export_scope_proven=True)
    assert manifest_for_required_window(
        manifest, date(2026, 8, 1), date(2026, 8, 2),
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
    manifest = csv_manifest(path, "paypay", export_scope_proven=True)
    assert manifest.parse_error
    assert manifest.completion_status == "unknown"
    assert manifest.completeness_proven is False


def test_explicit_complete_evidence_fixture(tmp_path):
    manifest = csv_manifest(_paypay(tmp_path), "paypay", export_scope_proven=True)
    assert manifest.completion_status == "complete"
    assert manifest.completeness_proven is True


def test_only_full_window_is_usable_for_future_complete(tmp_path):
    manifest = csv_manifest(_paypay(tmp_path), "paypay", export_scope_proven=True)
    assert manifest_for_required_window(
        manifest, date(2026, 8, 1), date(2026, 8, 1),
        coverage_basis="transaction_date",
    ) == "complete"
    assert manifest_for_required_window(
        manifest, date(2026, 8, 1), date(2026, 8, 2),
        coverage_basis="transaction_date",
    ) == "incomplete"


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
