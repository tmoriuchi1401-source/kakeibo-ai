from __future__ import annotations

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
