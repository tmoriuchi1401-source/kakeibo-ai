from dataclasses import asdict
from datetime import date, datetime, timezone
import sys

import pytest

from app import cli
from app.paypay_evidence_bundle import EvidenceVerificationResult
from app.coverage_confirmation import (
    COVERAGE_REASON_OPERATIONAL_ONLY,
    COVERAGE_STATUS_USER_CONFIRMED,
    ConfirmationIdentity,
    CoverageConfirmationIdentityResolution,
    CoverageConfirmationRecord,
    StoredCoverageConfirmation,
    coverage_confirmation_id,
)
from app.payment_coverage_manifest import (
    CoverageManifest,
    _prepare_payment_coverage_manifests,
    assemble_payment_coverage_manifests,
    classify_evidence,
    csv_manifest,
    gmail_manifest,
    manifest_for_required_window,
    preview_payment_coverage_manifests,
)
from app.paypay_operational_coverage import preview_operational_evidence


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


class _Resolver:
    def __init__(self, response=None, *, error=None):
        self.response = response
        self.error = error
        self.calls = []

    def resolve(self, identity):
        self.calls.append(identity)
        if self.error:
            raise self.error
        return self.response(identity) if callable(self.response) else self.response


def _confirmation(path, *, provider="paypay", start="2026-08-01",
                  end="2026-08-31", drive_file_id=None):
    record = CoverageConfirmationRecord(
        schema_version="1", provider=provider,
        content_sha256=__import__("hashlib").sha256(path.read_bytes()).hexdigest(),
        confirmed_start=start, confirmed_end=end, range_source="user_confirmed",
        confirmed_at=datetime(2026, 8, 31, tzinfo=timezone.utc),
        confirmation_version="1", source_filename=path.name,
        drive_file_id=drive_file_id,
    )
    stored = StoredCoverageConfirmation(
        coverage_confirmation_id(record.identity), record,
        COVERAGE_STATUS_USER_CONFIRMED, COVERAGE_REASON_OPERATIONAL_ONLY,
        datetime(2026, 8, 31, tzinfo=timezone.utc),
    )
    return record, CoverageConfirmationIdentityResolution(
        "exact_match", "exact_identity_match", stored,
    )


def test_no_evidence_is_unknown():
    result = preview_payment_coverage_manifests()
    assert result["unknown_count"] == 4
    assert all(row["completion_status"] == "unknown" for row in result["manifests"])


def test_object_assembly_is_reusable_before_preview_serialization(tmp_path):
    paypay_path = _paypay(tmp_path)
    card_path = _card(tmp_path)
    manifests = assemble_payment_coverage_manifests([
        csv_manifest(
            paypay_path,
            "paypay",
            paypay_operational_evidence=preview_operational_evidence(paypay_path),
        ),
        csv_manifest(card_path, "au_pay_card"),
    ])

    assert isinstance(manifests, list)
    assert all(isinstance(item, CoverageManifest) for item in manifests)
    assert [item.source for item in manifests] == [
        "paypay", "au_pay_card", "amazon_gmail", "au_pay_gmail",
    ]
    assert [item.coverage_basis for item in manifests] == [
        "transaction_date", "billing_cycle", "message_date", "message_date",
    ]

    preview = preview_payment_coverage_manifests(
        paypay_csvs=[str(paypay_path)],
        au_pay_card_csvs=[str(card_path)],
    )
    assert preview["manifests"] == [asdict(item) for item in manifests]


def test_object_assembly_preserves_placeholders_and_evidence_classification(tmp_path):
    path = _paypay(tmp_path)
    manifests = assemble_payment_coverage_manifests([
        csv_manifest(path, "paypay"),
        csv_manifest(path, "paypay"),
    ])

    assert [item.source for item in manifests] == [
        "paypay", "paypay", "au_pay_card", "amazon_gmail", "au_pay_gmail",
    ]
    assert manifests[1].completeness_reason == "duplicate_evidence"
    assert manifests[2].coverage_basis == "billing_cycle"
    assert manifests[2].completeness_reason == "no_completion_evidence"


def test_raw_preparation_returns_reusable_manifest_result(tmp_path):
    paypay_path = _paypay(tmp_path)
    card_path = _card(tmp_path)

    prepared = _prepare_payment_coverage_manifests(
        paypay_csvs=[str(paypay_path)],
        au_pay_card_csvs=[str(card_path)],
    )

    assert isinstance(prepared.manifests, list)
    assert all(isinstance(item, CoverageManifest) for item in prepared.manifests)
    assert [item.source for item in prepared.manifests] == [
        "paypay", "au_pay_card", "amazon_gmail", "au_pay_gmail",
    ]
    assert len(prepared.paypay_operational_evidences) == 1
    assert prepared.paypay_evidence_verifications == [None]
    assert prepared.duplicate_evidence_count == 0
    assert prepared.conflicting_evidence_count == 0
    assert prepared.operational_duplicate_count == 0
    assert prepared.operational_conflict_count == 0


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


def test_stored_confirmation_supplies_manifest_scope_without_completeness(tmp_path):
    path = _paypay(tmp_path, "Transactions_20260801-20260831.csv")
    record, resolution = _confirmation(path)
    resolver = _Resolver(resolution)

    result = preview_payment_coverage_manifests(
        paypay_csvs=[str(path)], confirmation_resolver=resolver,
    )

    evidence = result["paypay_operational_evidence"][0]
    manifest = next(row for row in result["manifests"] if row["source"] == "paypay")
    assert (evidence["requested_start"], evidence["requested_end"]) == (
        record.confirmed_start, record.confirmed_end,
    )
    assert evidence["operational_coverage"] == "usable"
    assert (manifest["coverage_start"], manifest["coverage_end"]) == (
        record.confirmed_start, record.confirmed_end,
    )
    assert manifest["completion_status"] == "unknown"
    assert manifest["completeness_proven"] is False
    assert manifest["completeness_reason"] == (
        "explicit_user_confirmation_not_provider_completeness"
    )
    assert resolver.calls == [record.identity]


def test_confirmation_not_found_preserves_unconfirmed_behavior(tmp_path):
    path = _paypay(tmp_path, "Transactions_20260801-20260831.csv")
    resolver = _Resolver(CoverageConfirmationIdentityResolution(
        "not_found", "confirmation_not_found",
    ))
    result = preview_payment_coverage_manifests(
        paypay_csvs=[str(path)], confirmation_resolver=resolver,
    )
    evidence = result["paypay_operational_evidence"][0]
    assert evidence["operational_coverage"] == "needs_confirmation"
    assert evidence["reason"] == "filename_range_requires_confirmation"


@pytest.mark.parametrize("response,error,reason", [
    (CoverageConfirmationIdentityResolution("invalid_store", "duplicate_identity"),
     None, "coverage_confirmation_store_invalid"),
    (None, RuntimeError("read failed"), "coverage_confirmation_lookup_failed"),
])
def test_confirmation_failure_fails_closed_per_evidence(
    tmp_path, response, error, reason,
):
    path = _paypay(tmp_path, "Transactions_20260801-20260831.csv")
    result = preview_payment_coverage_manifests(
        paypay_csvs=[str(path)],
        confirmation_resolver=_Resolver(response, error=error),
    )
    evidence = result["paypay_operational_evidence"][0]
    assert evidence["operational_coverage"] == "rejected"
    assert evidence["reason"] == reason


def test_manual_and_stored_ranges_must_match(tmp_path):
    path = _paypay(tmp_path, "Transactions_20260801-20260831.csv")
    _, resolution = _confirmation(path)
    same = preview_payment_coverage_manifests(
        paypay_csvs=[str(path)],
        paypay_confirmed_ranges=["2026-08-01:2026-08-31"],
        confirmation_resolver=_Resolver(resolution),
    )["paypay_operational_evidence"][0]
    different = preview_payment_coverage_manifests(
        paypay_csvs=[str(path)],
        paypay_confirmed_ranges=["2026-08-02:2026-08-31"],
        confirmation_resolver=_Resolver(resolution),
    )["paypay_operational_evidence"][0]
    assert same["operational_coverage"] == "usable"
    assert different["operational_coverage"] == "rejected"
    assert different["reason"] == "coverage_confirmation_range_conflict"


def test_wrong_sha_and_wrong_provider_do_not_match(tmp_path):
    path = _paypay(tmp_path, "Transactions_20260801-20260831.csv")
    wrong_path = _paypay(tmp_path, "wrong.csv", tx="TX-WRONG")
    wrong_record, _ = _confirmation(wrong_path)

    def only_exact(identity):
        if identity == wrong_record.identity:
            return _confirmation(wrong_path)[1]
        return CoverageConfirmationIdentityResolution(
            "not_found", "confirmation_not_found",
        )

    resolver = _Resolver(only_exact)
    result = preview_payment_coverage_manifests(
        paypay_csvs=[str(path)], confirmation_resolver=resolver,
    )
    assert resolver.calls[0].provider == "paypay"
    assert resolver.calls[0].content_sha256 != wrong_record.content_sha256
    assert result["paypay_operational_evidence"][0]["range_confirmed"] is False


def test_null_drive_id_and_same_sha_files_share_one_lookup(tmp_path):
    first = _paypay(tmp_path, "Transactions_20260801-20260831.csv")
    duplicate_dir = tmp_path / "duplicate"
    duplicate_dir.mkdir()
    second = duplicate_dir / first.name
    second.write_bytes(first.read_bytes())
    record, resolution = _confirmation(first, drive_file_id=None)
    resolver = _Resolver(resolution)
    result = preview_payment_coverage_manifests(
        paypay_csvs=[str(first), str(second)], confirmation_resolver=resolver,
    )
    assert record.drive_file_id is None
    assert resolver.calls == [ConfirmationIdentity("paypay", record.content_sha256)]
    assert all(item["range_confirmed"] for item in result["paypay_operational_evidence"])


@pytest.mark.parametrize("name,start,end,reason", [
    ("Transactions_20260701-20260731.csv", "2026-08-01", "2026-08-31",
     "confirmed_range_conflicts_with_filename_candidate"),
    ("paypay.csv", "2026-08-02", "2026-08-31",
     "transaction_outside_requested_range"),
])
def test_stored_confirmation_does_not_bypass_operational_rejects(
    tmp_path, name, start, end, reason,
):
    path = _paypay(tmp_path, name)
    _, resolution = _confirmation(path, start=start, end=end)
    evidence = preview_payment_coverage_manifests(
        paypay_csvs=[str(path)], confirmation_resolver=_Resolver(resolution),
    )["paypay_operational_evidence"][0]
    assert evidence["operational_coverage"] in {"needs_confirmation", "rejected"}
    assert evidence["reason"] == reason


def test_stored_confirmation_does_not_bypass_parse_error(tmp_path):
    path = tmp_path / "bad.csv"
    path.write_text("not,a,paypay,csv\n", encoding="utf-8")
    _, resolution = _confirmation(path)
    result = preview_payment_coverage_manifests(
        paypay_csvs=[str(path)], confirmation_resolver=_Resolver(resolution),
    )
    evidence = result["paypay_operational_evidence"][0]
    manifest = next(row for row in result["manifests"] if row["source"] == "paypay")
    assert evidence["operational_coverage"] == "rejected"
    assert evidence["reason"] == "parse_error"
    assert manifest["completion_status"] == "unknown"
    assert manifest["completeness_proven"] is False
