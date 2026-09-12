from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

from app import cli
from app.cli import print_drive_receipt_results


def test_drive_receipts_cli_hides_filename_for_privacy_blocked_result(capsys):
    filename = "山田太郎_病院領収書.pdf"
    print_drive_receipt_results(
        [
            (
                filename,
                {
                    "status": "privacy_blocked",
                    "classification": "medical",
                    "gemini_allowed": False,
                },
            )
        ]
    )

    output = capsys.readouterr().out
    assert filename not in output
    assert "privacy_blocked" in output
    assert "medical" in output


def test_drive_receipts_cli_keeps_existing_filename_output_for_normal_result(capsys):
    filename = "normal-receipt.png"
    print_drive_receipt_results([(filename, {"status": "imported", "total": 100})])

    output = capsys.readouterr().out
    assert filename in output
    assert "imported" in output


def test_receipt_cli_defers_gemini_until_pipeline_use(tmp_path, monkeypatch, capsys):
    image = tmp_path / "medical-receipt.png"
    image.write_bytes(b"synthetic medical bytes")
    settings = SimpleNamespace()
    db = object()
    make = Mock(return_value=(settings, db, None))
    pipeline = Mock()
    pipeline.process_bytes.return_value = {
        "status": "privacy_blocked",
        "classification": "medical",
        "gemini_allowed": False,
    }
    make_pipeline = Mock(return_value=pipeline)
    monkeypatch.setattr(cli, "make", make)
    monkeypatch.setattr(cli, "make_receipt_pipeline", make_pipeline)
    monkeypatch.setattr(
        "sys.argv",
        ["kakeibo-ai", "receipt", str(image), "--source-classification", "medical"],
    )

    cli.main()

    make.assert_called_once_with(False)
    make_pipeline.assert_called_once_with(settings, db, None)
    assert "privacy_blocked" in capsys.readouterr().out


def test_drive_receipts_cli_defers_gemini_until_each_receipt_use(monkeypatch):
    settings = SimpleNamespace(
        receipt_drive_folder_id="folder-id-12345",
        processed_drive_folder_id="",
        validate=Mock(),
    )
    db = object()
    make = Mock(return_value=(settings, db, None))
    pipeline = object()
    process = Mock(return_value=[])
    monkeypatch.setattr(cli, "make", make)
    monkeypatch.setattr(cli, "make_receipt_pipeline", Mock(return_value=pipeline))
    monkeypatch.setattr(cli, "process_inbox", process)
    monkeypatch.setattr("sys.argv", ["kakeibo-ai", "drive-receipts"])

    cli.main()

    make.assert_called_once_with(False)
    settings.validate.assert_called_once_with(need_drive=True)
    process.assert_called_once_with(
        "folder-id-12345", pipeline, "", known_source_classification=None
    )
