import hashlib

from app.paypay_operational_coverage import (
    classify_operational_evidence,
    extract_filename_range,
    preview_operational_evidence,
)
from app.payment_coverage_manifest import preview_payment_coverage_manifests


HEADER = (
    "取引日,出金金額（円）,入金金額（円）,海外出金金額,通貨,変換レート（円）,利用国,"
    "取引内容,取引先,取引方法,支払い区分,利用者,取引番号\n"
)


def _csv(tmp_path, name="Transactions_20260801-20260831.csv", dates=None, tx="TX-1"):
    path = tmp_path / name
    rows = "".join(
        f"{day} 12:00,100,,,,,,支払い,店,PayPay残高,一回払い,本人,{tx}-{index}\n"
        for index, day in enumerate(dates or [])
    )
    path.write_text(HEADER + rows, encoding="utf-8-sig")
    return path


def _confirmed(path, start="2026-08-01", end="2026-08-31"):
    return preview_operational_evidence(
        path, requested_start=start, requested_end=end,
        range_source="user_confirmed", range_confirmed=True,
    )


def test_filename_range_extraction_success_and_failure():
    assert extract_filename_range("Transactions_20250820-20260820.csv") == (
        "2025-08-20", "2026-08-20",
    )
    assert extract_filename_range("paypay_20250820-20260820.csv") is None
    assert extract_filename_range("Transactions_20260230-20260820.csv") is None


def test_filename_candidate_alone_needs_confirmation(tmp_path):
    evidence = preview_operational_evidence(_csv(tmp_path, dates=["2026/08/10"]))
    assert evidence.requested_start == "2026-08-01"
    assert evidence.requested_end == "2026-08-31"
    assert evidence.range_source == "filename_candidate"
    assert evidence.operational_coverage == "needs_confirmation"
    assert evidence.reason == "filename_range_requires_confirmation"


def test_user_confirmed_range_is_usable_and_hash_is_computed(tmp_path):
    path = _csv(tmp_path, dates=["2026/08/10", "2026/08/20"])
    evidence = _confirmed(path)
    assert evidence.operational_coverage == "usable"
    assert evidence.reason == "operational_checks_passed"
    assert evidence.csv_sha256 == hashlib.sha256(path.read_bytes()).hexdigest()
    assert evidence.row_count == 2
    assert (evidence.transaction_min_date, evidence.transaction_max_date) == (
        "2026-08-10", "2026-08-20",
    )


def test_transaction_outside_requested_range_is_rejected(tmp_path):
    path = _csv(tmp_path, dates=["2026/07/31", "2026/08/10"])
    evidence = _confirmed(path)
    assert evidence.operational_coverage == "rejected"
    assert evidence.reason == "transaction_outside_requested_range"


def test_empty_csv_with_confirmed_range_is_usable_candidate(tmp_path):
    evidence = _confirmed(_csv(tmp_path))
    assert evidence.row_count == 0
    assert evidence.transaction_min_date is None
    assert evidence.transaction_max_date is None
    assert evidence.operational_coverage == "usable"


def test_transaction_min_max_never_replace_confirmed_scope(tmp_path):
    evidence = _confirmed(_csv(tmp_path, dates=["2026/08/10", "2026/08/20"]))
    assert (evidence.requested_start, evidence.requested_end) == (
        "2026-08-01", "2026-08-31",
    )
    assert (evidence.transaction_min_date, evidence.transaction_max_date) != (
        evidence.requested_start, evidence.requested_end,
    )


def test_parse_failure_is_rejected_but_hash_is_available(tmp_path):
    path = tmp_path / "Transactions_20260801-20260831.csv"
    path.write_text("not,a,paypay,csv\n", encoding="utf-8")
    evidence = _confirmed(path)
    assert evidence.operational_coverage == "rejected"
    assert evidence.reason == "parse_error"
    assert evidence.parse_error
    assert evidence.csv_sha256 == hashlib.sha256(path.read_bytes()).hexdigest()


def test_duplicate_same_period_and_hash(tmp_path):
    first = _csv(tmp_path, "first.csv", ["2026/08/10"])
    second = tmp_path / "second.csv"
    second.write_bytes(first.read_bytes())
    classified, duplicates, conflicts = classify_operational_evidence([
        _confirmed(first), _confirmed(second),
    ])
    assert (duplicates, conflicts) == (1, 0)
    assert classified[1].reason == "duplicate_operational_evidence"
    assert classified[1].operational_coverage == "usable"


def test_conflict_same_period_and_different_hash_rejects_all(tmp_path):
    first = _csv(tmp_path, "first.csv", ["2026/08/10"], "TX-A")
    second = _csv(tmp_path, "second.csv", ["2026/08/11"], "TX-B")
    classified, duplicates, conflicts = classify_operational_evidence([
        _confirmed(first), _confirmed(second),
    ])
    assert (duplicates, conflicts) == (0, 1)
    assert all(item.operational_coverage == "rejected" for item in classified)
    assert all(item.reason == "conflicting_operational_evidence" for item in classified)


def test_unconfirmed_filename_candidates_do_not_create_period_conflict(tmp_path):
    first = _csv(tmp_path, "Transactions_20260801-20260831.csv", ["2026/08/10"])
    second = _csv(tmp_path, "other.csv", ["2026/08/11"])
    second_named = tmp_path / "copy" / "Transactions_20260801-20260831.csv"
    second_named.parent.mkdir()
    second_named.write_bytes(second.read_bytes())
    classified, duplicates, conflicts = classify_operational_evidence([
        preview_operational_evidence(first), preview_operational_evidence(second_named),
    ])
    assert (duplicates, conflicts) == (0, 0)
    assert all(item.operational_coverage == "needs_confirmation" for item in classified)


def test_confirmed_filename_contradiction_needs_confirmation(tmp_path):
    path = _csv(tmp_path, dates=["2026/08/10"])
    evidence = _confirmed(path, "2026-08-02", "2026-08-31")
    assert evidence.operational_coverage == "needs_confirmation"
    assert evidence.reason == "confirmed_range_conflicts_with_filename_candidate"


def test_preview_usable_stays_cryptographically_unknown(tmp_path):
    path = _csv(tmp_path, dates=["2026/08/10"])
    result = preview_payment_coverage_manifests(
        paypay_csvs=[str(path)],
        paypay_confirmed_ranges=["2026-08-01:2026-08-31"],
    )
    operational = result["paypay_operational_evidence"][0]
    manifest = next(row for row in result["manifests"] if row["source"] == "paypay")
    assert operational["operational_coverage"] == "usable"
    assert manifest["operational_coverage"] == "usable"
    assert manifest["completion_status"] == "unknown"
    assert manifest["completeness_proven"] is False
    assert manifest["coverage_start"] == manifest["coverage_end"] == "2026-08-10"
