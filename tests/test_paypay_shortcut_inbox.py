import json
import sys

import pytest

from app import cli
from app.paypay_shortcut_inbox import (
    parse_shortcut_metadata,
    preview_shortcut_inbox,
    preview_shortcut_ingest_folder,
)


HEADER = (
    "取引日,出金金額（円）,入金金額（円）,海外出金金額,通貨,変換レート（円）,利用国,"
    "取引内容,取引先,取引方法,支払い区分,利用者,取引番号\n"
)


def _metadata(ingest_id, filename="Transactions_20260801-20260831.csv", **overrides):
    data = {
        "schema_version": "1",
        "ingest_id": ingest_id,
        "requested_start": "2026-08-01",
        "requested_end": "2026-08-31",
        "range_confirmed": True,
        "range_source": "user_confirmed",
        "original_filename": filename,
        "shortcut_version": "1.0",
        "shared_at": "2026-08-29T06:30:12Z",
    }
    data.update(overrides)
    return data


def _ingest(root, ingest_id="ingest-1", *, dates=None, filename=None,
            metadata=True, metadata_overrides=None):
    folder = root / ingest_id
    folder.mkdir(parents=True)
    filename = filename or "Transactions_20260801-20260831.csv"
    csv_path = folder / filename
    rows = "".join(
        f"{day} 12:00,100,,,,,,支払い,店,PayPay残高,一回払い,本人,TX-{index}\n"
        for index, day in enumerate(dates or [])
    )
    csv_path.write_text(HEADER + rows, encoding="utf-8-sig")
    if metadata:
        metadata_data = _metadata(ingest_id, filename)
        metadata_data.update(metadata_overrides or {})
        (folder / "metadata.kakeibo.json").write_text(json.dumps(
            metadata_data,
        ), encoding="utf-8")
    return folder, csv_path


def test_valid_csv_and_metadata_are_paired_and_usable(tmp_path):
    folder, _ = _ingest(tmp_path, dates=["2026/08/10"])
    result = preview_shortcut_ingest_folder(folder)
    assert result.pairing_status == "paired"
    assert result.metadata_valid is True
    assert result.csv_parse_status == "success"
    assert result.operational_coverage == "usable"
    assert result.completion_status == "unknown"
    assert result.completeness_proven is False


def test_csv_only_is_parsed_and_needs_confirmation(tmp_path):
    folder, _ = _ingest(tmp_path, dates=["2026/08/10"], metadata=False)
    result = preview_shortcut_ingest_folder(folder)
    assert result.pairing_status == "csv_only"
    assert result.metadata_reason == "metadata_missing"
    assert result.csv_parse_status == "success"
    assert result.operational_coverage == "needs_confirmation"


def test_metadata_only_is_orphan_and_has_no_operational_evidence(tmp_path):
    folder = tmp_path / "ingest-1"
    folder.mkdir()
    (folder / "metadata.kakeibo.json").write_text(
        json.dumps(_metadata("ingest-1")), encoding="utf-8",
    )
    result = preview_shortcut_ingest_folder(folder)
    assert result.pairing_status == "metadata_only"
    assert result.orphan is True
    assert result.operational_evidence is None
    assert result.operational_coverage is None


def test_malformed_metadata_falls_back_to_csv_only_analysis(tmp_path):
    folder, _ = _ingest(tmp_path, dates=["2026/08/10"], metadata=False)
    (folder / "metadata.kakeibo.json").write_text("not json", encoding="utf-8")
    result = preview_shortcut_ingest_folder(folder)
    assert result.pairing_status == "metadata_invalid"
    assert result.metadata_reason == "malformed_metadata"
    assert result.csv_parse_status == "success"
    assert result.operational_coverage == "needs_confirmation"


def test_duplicate_json_key_is_rejected():
    text = json.dumps(_metadata("ingest-1"))
    text = text.replace('"schema_version": "1"',
                        '"schema_version": "1", "schema_version": "1"')
    with pytest.raises(ValueError, match="duplicate_json_key"):
        parse_shortcut_metadata(text)


@pytest.mark.parametrize("overrides,reason", [
    ({"schema_version": "2"}, "unsupported_schema_version"),
    ({"schema_version": 1}, "invalid_type:schema_version"),
    ({"range_confirmed": "yes"}, "invalid_type:range_confirmed"),
    ({"range_source": 1}, "invalid_type:range_source"),
    ({"requested_start": None}, "requested_range_must_be_both_or_neither"),
    ({"requested_start": None, "requested_end": None}, "confirmed_range_requires_dates"),
    ({"range_confirmed": False}, "user_confirmed_requires_confirmation"),
    ({"shared_at": "2026-08-29T15:30:12+09:00"}, "invalid_shared_at"),
])
def test_metadata_schema_validation(overrides, reason):
    with pytest.raises(ValueError, match=reason):
        parse_shortcut_metadata(json.dumps(_metadata("ingest-1", **overrides)))


def test_unknown_metadata_field_is_rejected():
    data = _metadata("ingest-1")
    data["extra"] = "no"
    with pytest.raises(ValueError, match="unknown_or_missing_metadata_fields"):
        parse_shortcut_metadata(json.dumps(data))


def test_ingest_id_mismatch_ignores_metadata_and_parses_csv(tmp_path):
    folder, _ = _ingest(
        tmp_path, dates=["2026/08/10"],
        metadata_overrides={"ingest_id": "another-ingest"},
    )
    result = preview_shortcut_ingest_folder(folder)
    assert result.pairing_status == "ingest_id_mismatch"
    assert result.metadata_valid is False
    assert result.operational_coverage == "needs_confirmation"


def test_metadata_only_ingest_id_mismatch_is_diagnosed(tmp_path):
    folder = tmp_path / "ingest-1"
    folder.mkdir()
    (folder / "metadata.kakeibo.json").write_text(
        json.dumps(_metadata("another-ingest")), encoding="utf-8",
    )
    result = preview_shortcut_ingest_folder(folder)
    assert result.pairing_status == "ingest_id_mismatch"
    assert result.orphan is True
    assert result.operational_evidence is None


def test_original_filename_mismatch_ignores_metadata(tmp_path):
    folder, _ = _ingest(
        tmp_path, dates=["2026/08/10"],
        metadata_overrides={"original_filename": "another.csv"},
    )
    result = preview_shortcut_ingest_folder(folder)
    assert result.pairing_status == "original_filename_mismatch"
    assert result.operational_coverage == "needs_confirmation"


def test_two_csvs_are_ambiguous_and_not_selected(tmp_path):
    folder, _ = _ingest(tmp_path)
    (folder / "second.csv").write_text(HEADER, encoding="utf-8-sig")
    result = preview_shortcut_ingest_folder(folder)
    assert result.ambiguous is True
    assert result.csv_filename is None
    assert result.csv_parse_status == "not_parsed"
    assert result.operational_coverage == "rejected"


def test_two_metadata_files_are_ambiguous(tmp_path):
    folder, _ = _ingest(tmp_path)
    (folder / "second.kakeibo.json").write_text(
        json.dumps(_metadata("ingest-1")), encoding="utf-8",
    )
    result = preview_shortcut_ingest_folder(folder)
    assert result.ambiguous is True
    assert result.pairing_status == "ambiguous"


def test_unexpected_files_only_are_ambiguous(tmp_path):
    folder = tmp_path / "ingest-1"
    folder.mkdir()
    (folder / "note.txt").write_text("unexpected")
    result = preview_shortcut_ingest_folder(folder)
    assert result.ambiguous is True
    assert result.unexpected_files == ("note.txt",)


def test_unconfirmed_filename_candidate_needs_confirmation(tmp_path):
    folder, _ = _ingest(tmp_path, dates=["2026/08/10"], metadata_overrides={
        "range_confirmed": False, "range_source": "filename_candidate",
    })
    result = preview_shortcut_ingest_folder(folder)
    assert result.operational_coverage == "needs_confirmation"
    assert result.operational_reason == "range_not_confirmed"


def test_no_filename_candidate_and_no_confirmed_range_needs_confirmation(tmp_path):
    folder, _ = _ingest(
        tmp_path, filename="paypay.csv", dates=["2026/08/10"],
        metadata_overrides={
            "requested_start": None, "requested_end": None,
            "range_confirmed": False, "range_source": None,
        },
    )
    result = preview_shortcut_ingest_folder(folder)
    assert result.operational_coverage == "needs_confirmation"
    assert result.operational_reason == "requested_range_missing"


def test_transaction_outside_confirmed_range_is_rejected(tmp_path):
    folder, _ = _ingest(tmp_path, dates=["2026/07/31"])
    result = preview_shortcut_ingest_folder(folder)
    assert result.operational_coverage == "rejected"
    assert result.operational_reason == "transaction_outside_requested_range"


def test_empty_csv_with_confirmed_range_is_usable(tmp_path):
    folder, _ = _ingest(tmp_path)
    result = preview_shortcut_ingest_folder(folder)
    assert result.operational_evidence.row_count == 0
    assert result.operational_coverage == "usable"
    assert result.completion_status == "unknown"
    assert result.completeness_proven is False


def test_inbox_duplicate_and_conflict_classification(tmp_path):
    _, first = _ingest(tmp_path, "ingest-1", dates=["2026/08/10"])
    folder2, second = _ingest(tmp_path, "ingest-2", dates=["2026/08/10"])
    second.write_bytes(first.read_bytes())
    duplicate = preview_shortcut_inbox(tmp_path)
    assert duplicate["duplicate_count"] == 1
    assert duplicate["conflict_count"] == 0
    assert duplicate["ingests"][1]["operational_reason"] == "duplicate_operational_evidence"

    second.write_bytes(second.read_bytes() + b"\n")
    conflict = preview_shortcut_inbox(tmp_path)
    assert conflict["duplicate_count"] == 0
    assert conflict["conflict_count"] == 1
    assert all(item["operational_coverage"] == "rejected"
               for item in conflict["ingests"])


def test_cli_preview_is_local_read_only(tmp_path, monkeypatch, capsys):
    _ingest(tmp_path, dates=["2026/08/10"])
    monkeypatch.setattr(
        sys, "argv", ["kakeibo", "paypay-shortcut-inbox-preview", str(tmp_path)],
    )
    monkeypatch.setattr(
        cli, "Settings", lambda: pytest.fail("credentials must not be loaded"),
    )
    cli.main()
    output = capsys.readouterr().out
    assert "'read_only': True" in output
    assert "'operational_coverage': 'usable'" in output
