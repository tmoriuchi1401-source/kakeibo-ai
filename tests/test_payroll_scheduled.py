from dataclasses import replace
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

import pytest

from app.payroll_production_runner import (
    PayrollProductionRunReport,
    PayrollProductionStatementResult,
)
from app.payroll_scheduled import (
    CONFIG_VERSION,
    PayrollScheduledConfig,
    PayrollScheduledLockError,
    build_payroll_scheduled_summary,
    load_payroll_scheduled_config,
    run_payroll_scheduled_scan,
    single_run_lock,
    write_new_payroll_scheduled_config,
)


def report(*results, invocations=0, actual=0):
    counts = {}
    for result in results:
        counts[result.outcome] = counts.get(result.outcome, 0) + 1
    return PayrollProductionRunReport(
        statement_count=len(results), outcome_counts=counts,
        writer_candidate_count=sum(x.writer_candidate for x in results),
        writer_invocation_count=invocations,
        actual_header_rows=actual, actual_item_rows=0, actual_update_rows=0,
        reconciliation_source="external_journal",
        reconciliation_reload_reason="reconciliation_journal_loaded",
        reconciliation_record_count=1, results=results,
    )


def statement(outcome, suffix, *, review=0, ready=False):
    return PayrollProductionStatementResult(
        statement_id=f"statement-{suffix}", content_hash=suffix * 64,
        outcome=outcome, reason=outcome.lower(), review_item_count=review,
        planned_header_rows=1 if ready else 0,
        planned_item_rows=1 if ready else 0, writer_candidate=ready,
    )


def external_config(tmp_path):
    root = tmp_path / "repo"
    external = tmp_path / "external"
    root.mkdir()
    external.mkdir()
    files = {}
    for name in ("credential.json", "review.json", "review.key", "recon.json", "recon.key"):
        files[name] = external / name
        files[name].write_text("fixture", encoding="ascii")
    config = PayrollScheduledConfig(
        config_version=CONFIG_VERSION,
        spreadsheet_id="sheet-id", payroll_drive_folder_id="folder-id",
        google_service_account_file=files["credential.json"],
        review_journal_file=files["review.json"],
        review_hmac_key_file=files["review.key"],
        review_source_content_hash="a" * 64,
        reconciliation_journal_file=files["recon.json"],
        reconciliation_hmac_key_file=files["recon.key"],
        log_directory=external / "logs", lock_file=external / "run.lock",
    )
    return root, external, config


def test_external_config_is_exclusive_and_survives_fresh_reload(tmp_path):
    root, external, config = external_config(tmp_path)
    path = external / "scheduled.json"

    assert write_new_payroll_scheduled_config(
        config, path, repository_root=root,
    ) == path
    loaded = load_payroll_scheduled_config(path, repository_root=root)

    assert loaded == config
    with pytest.raises(ValueError, match="scheduled_config_already_exists"):
        write_new_payroll_scheduled_config(config, path, repository_root=root)


def test_config_and_outputs_must_be_outside_repository(tmp_path):
    root, external, config = external_config(tmp_path)
    repo_config = root / "scheduled.json"
    external_path = external / "scheduled.json"

    with pytest.raises(ValueError, match="outside_repository"):
        write_new_payroll_scheduled_config(config, repo_config, repository_root=root)
    bad = replace(config, log_directory=root / "logs")
    with pytest.raises(ValueError, match="log_directory_invalid"):
        write_new_payroll_scheduled_config(bad, external_path, repository_root=root)


def test_summary_has_all_outcomes_and_no_statement_identifier():
    values = (
        statement("WRITE_READY", "1", ready=True),
        statement("EXACT_DUPLICATE", "2"),
        statement("ALTERNATE_SOURCE_DUPLICATE", "3"),
        statement("NEEDS_REVIEW", "4", review=13),
        statement("CONFLICT", "5"),
    )
    summary = build_payroll_scheduled_summary(
        report(*values), now=datetime(2026, 9, 11, tzinfo=timezone.utc),
    )

    assert summary["scanned_source_count"] == 5
    assert summary["outcome_counts"] == {
        "WRITE_READY": 1, "EXACT_DUPLICATE": 1,
        "ALTERNATE_SOURCE_DUPLICATE": 1, "NEEDS_REVIEW": 1,
        "CONFLICT": 1,
    }
    assert summary["writer_candidate_count"] == 1
    assert summary["writer_invocation_count"] == 0
    assert summary["actual_header_rows"] == summary["actual_item_rows"] == 0
    encoded = json.dumps(summary)
    assert "statement-1" not in encoded
    assert len(summary["write_ready"][0]["source_identifier_digest"]) == 64


def test_scheduled_scan_writes_privacy_safe_local_log_and_releases_lock(tmp_path):
    _, _, config = external_config(tmp_path)
    ready = statement("WRITE_READY", "6", ready=True)

    result = run_payroll_scheduled_scan(config, lambda: report(ready))

    assert result.log_path.is_file()
    assert not config.lock_file.exists()
    saved = json.loads(result.log_path.read_text(encoding="ascii"))
    assert saved["write_ready"][0]["outcome"] == "WRITE_READY"
    assert saved["writer_invocation_count"] == 0
    assert saved["actual_header_rows"] == 0


def test_existing_lock_skips_second_run_without_invoking_scan(tmp_path):
    _, _, config = external_config(tmp_path)
    invoked = 0

    with single_run_lock(config.lock_file):
        with pytest.raises(PayrollScheduledLockError, match="already_locked"):
            run_payroll_scheduled_scan(
                config, lambda: (_ for _ in ()).throw(AssertionError()),
            )
    assert invoked == 0
    assert not config.lock_file.exists()


@pytest.mark.parametrize("invocations,actual", [(1, 0), (0, 1)])
def test_scheduled_scan_rejects_any_write_boundary_activity(
    tmp_path, invocations, actual,
):
    _, _, config = external_config(tmp_path)
    duplicate = statement("EXACT_DUPLICATE", "7")

    with pytest.raises(RuntimeError, match="write_boundary_violated"):
        run_payroll_scheduled_scan(
            config,
            lambda: report(
                duplicate, invocations=invocations, actual=actual,
            ),
        )
    assert not config.lock_file.exists()
    assert not config.log_directory.exists()


def test_cli_scheduled_command_uses_explicit_config_and_prints_summary(
    tmp_path, monkeypatch, capsys,
):
    from app import cli

    _, _, config = external_config(tmp_path)
    ready = statement("WRITE_READY", "8", ready=True)

    class Reader:
        def __init__(self, spreadsheet_id):
            assert spreadsheet_id == "sheet-id"

        def snapshot(self):
            return "snapshot"

    monkeypatch.setattr(cli, "load_payroll_scheduled_config", lambda *a, **k: config)
    monkeypatch.setattr(cli, "PayrollSheetsReadRepository", Reader)
    monkeypatch.setattr(
        cli, "drive_storage_candidates",
        lambda folder, snapshot, **kwargs: ["candidate"],
    )
    monkeypatch.setattr(
        cli, "run_payroll_production_preview",
        lambda candidates, snapshot, **kwargs: report(ready),
    )
    monkeypatch.setattr(sys, "argv", [
        "app.cli", "payroll-production-scheduled", "--config-file", "external.json",
    ])

    cli.main()

    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "completed"
    assert output["scanned_source_count"] == 1
    assert output["writer_candidate_count"] == 1
    assert output["writer_invocation_count"] == 0
    assert output["actual_header_rows"] == 0


def test_cli_invalid_config_has_stable_failure_exit_without_error_detail(
    monkeypatch, capsys,
):
    from app import cli

    monkeypatch.setattr(
        cli, "load_payroll_scheduled_config",
        lambda *a, **k: (_ for _ in ()).throw(ValueError("secret-detail")),
    )
    monkeypatch.setattr(sys, "argv", [
        "app.cli", "payroll-production-scheduled", "--config-file", "bad.json",
    ])

    with pytest.raises(SystemExit) as stopped:
        cli.main()

    assert stopped.value.code == 2
    output = capsys.readouterr().out
    assert "secret-detail" not in output
    assert json.loads(output)["error_category"] == "ValueError"
