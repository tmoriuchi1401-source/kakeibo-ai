from copy import deepcopy
import hashlib
import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from app import ledger_daily_migration as migration
from app.amazon_money import MoneyError
from app.drive_run_state import StateError
from app.private_state_bindings import wrap, unwrap
from test_amazon_money_migration import DAY, statement_fixture
from test_projection_refresh import Store


SHA = "b" * 40


def environment():
    return {"GITHUB_ACTIONS": "true", "GITHUB_REF": "refs/heads/main",
        "GITHUB_REPOSITORY": "tmoriuchi1401-source/kakeibo-ai", "GITHUB_EVENT_NAME": "workflow_dispatch",
        "GITHUB_SHA": SHA, "KAKEIBO_VALIDATED_MAIN_SHA": SHA, "KAKEIBO_LEGACY_DISABLED": "true"}


@pytest.mark.parametrize("field,value", [("GITHUB_ACTIONS", "false"), ("GITHUB_REF", "refs/heads/topic"),
    ("GITHUB_REPOSITORY", "other/repo"), ("GITHUB_EVENT_NAME", "schedule"),
    ("GITHUB_SHA", "c" * 40), ("KAKEIBO_VALIDATED_MAIN_SHA", "c" * 40), ("RUNNER_DEBUG", "1")])
def test_main_boundary_rejects_wrong_context_even_for_public_key_info(field, value):
    env = dict(environment(), **{field: value})
    with pytest.raises(StateError, match="validated_main_required"):
        migration.validate_boundary(env, SHA, SHA, "key-info")


@pytest.mark.parametrize("head,expected", [("c" * 40, SHA), (SHA, "c" * 40), ("", "")])
def test_checkout_expected_and_validated_sha_must_match(head, expected):
    with pytest.raises(StateError, match="validated_main_required"):
        migration.validate_boundary(environment(), head, expected, "inspect")


@pytest.mark.parametrize("operation", ["inspect", "initialize"])
@pytest.mark.parametrize("field,value", [("KAKEIBO_PRODUCTION_ENABLED", "true"),
    ("KAKEIBO_SCHEDULE_ENABLED", "true"), ("KAKEIBO_LEGACY_DISABLED", "false"),
    ("KAKEIBO_AMAZON_MONEY_MODE", "confirmed-v1"), ("KAKEIBO_DAILY_CORRECTIONS_MODE", "fixed-id-v1")])
def test_migration_requires_frozen_inactive_writers(operation, field, value):
    env = dict(environment(), **{field: value})
    with pytest.raises(StateError, match="freeze_required|mode_must_be_inactive"):
        migration.validate_boundary(env, SHA, SHA, operation)
    # This operation only hashes the public part of the already configured key.
    migration.validate_boundary(env, SHA, SHA, "key-info")


def fixture():
    raw = statement_fixture()
    # Also retain an unresolved old match; no synthetic payment is inferred.
    notice = deepcopy(raw["imports"][0])
    notice[0], notice[3], notice[8], notice[9] = "unknown-import", "unknown-import", "matched_amazon", ""
    raw["imports"].append(notice)
    backup = dict(deepcopy(raw), spreadsheet_id="synthetic-backup")
    return raw, backup, Store()


def invoke(store, raw, backup, **kwargs):
    return migration.migrate(store, lambda: deepcopy(raw), lambda: deepcopy(backup), cutover_day=DAY, **kwargs)


def test_inspect_initialize_and_replay_preserve_all_finance_values_and_personal_state():
    raw, backup, store = fixture()
    original = deepcopy(raw)
    report = invoke(store, raw, backup)
    assert store.writes == [] and not store.data
    assert set(report) == {"manifest_sha256", "counts"}
    assert report["counts"] == {"money_initialized": 0, "expense_rows_written": 0, "import_rows_written": 0}
    result = invoke(store, raw, backup, apply=True, expected_commitment=report["manifest_sha256"])
    assert result["counts"]["money_initialized"] == 1
    assert store.writes == ["money-migration", "money-notices", "money"]
    assert store.data["money"]["legacy"]["order"]["state"] == "settled"
    assert len(store.data["money"]["records"]) == 3
    # A human acknowledgment and an unrelated pending request survive replay.
    next(iter(store.data["money-notices"]["notices"].values()))["state"] = "held"
    store.data["corrections"] = {"requests": {"synthetic-pending": {"state": "pending", "input": "preserve"}}}
    before, writes = deepcopy(store.data), list(store.writes)
    replay = invoke(store, raw, backup, apply=True, expected_commitment=report["manifest_sha256"])
    assert replay["counts"]["money_initialized"] == 0
    assert store.data == before and store.writes == writes and raw == original
    rendered = json.dumps(report)
    assert raw["spreadsheet_id"] not in rendered and "本人" not in rendered and "1000" not in rendered


@pytest.mark.parametrize("table,column", [("expenses", 0), ("expenses", 4), ("expenses", 5),
    ("expenses", 11), ("imports", 8), ("imports", 9)])
def test_stale_backup_or_changed_personal_input_blocks_before_any_write(table, column):
    raw, backup, store = fixture()
    backup[table][0][column] = "changed"
    with pytest.raises(StateError, match="backup_snapshot_mismatch"):
        invoke(store, raw, backup, apply=True, expected_commitment="a" * 64)
    assert store.writes == []


def test_commitment_includes_exact_backup_identity_and_cutover_date():
    raw, backup, store = fixture()
    digest = invoke(store, raw, backup)["manifest_sha256"]
    other_backup = dict(backup, spreadsheet_id="another-backup")
    with pytest.raises(StateError, match="commitment_mismatch"):
        invoke(store, raw, other_backup, apply=True, expected_commitment=digest)
    with pytest.raises(StateError, match="commitment_mismatch"):
        migration.migrate(store, lambda: raw, lambda: backup, cutover_day="2026-09-21",
                          apply=True, expected_commitment=digest)
    with pytest.raises(StateError, match="commitment_required"):
        invoke(store, raw, backup, apply=True)
    assert store.writes == []


def test_backup_changed_during_operation_is_not_reported_as_verified():
    raw, backup, store = fixture()
    calls = 0
    def read_backup():
        nonlocal calls
        calls += 1
        value = deepcopy(backup)
        if calls > 1:
            value["expenses"][0][11] = "backup changed"
        return value
    with pytest.raises(StateError, match="backup_snapshot_mismatch"):
        migration.migrate(store, lambda: raw, read_backup, cutover_day=DAY)
    assert not store.writes


@pytest.mark.parametrize("key", ["money-migration", "money-notices", "money"])
@pytest.mark.parametrize("after_save", [False, True])
def test_lost_write_response_restarts_from_saved_intent_without_duplicate(key, after_save):
    raw, backup, store = fixture()
    digest = invoke(store, raw, backup)["manifest_sha256"]
    store.fail_key, store.after_save = key, after_save
    with pytest.raises(RuntimeError, match="synthetic_write_failure"):
        invoke(store, raw, backup, apply=True, expected_commitment=digest)
    if key != "money":
        assert "money" not in store.data
    invoke(store, raw, backup, apply=True, expected_commitment=digest)
    assert store.writes == ["money-migration", "money-notices", "money"]
    assert len(store.data["money"]["records"]) == 3


@pytest.mark.parametrize("change_at", [2, 3, 4])
def test_changes_during_initialize_never_rewrite_canonical_or_reset_saved_intent(change_at):
    raw, backup, store = fixture()
    digest = invoke(store, raw, backup)["manifest_sha256"]
    reads = 0
    def read():
        nonlocal reads
        reads += 1
        result = deepcopy(raw)
        if reads >= change_at:
            result["expenses"][0][11] = "本人の最新入力"
        return result
    with pytest.raises(MoneyError, match="snapshot_changed"):
        migration.migrate(store, read, lambda: backup, cutover_day=DAY, apply=True, expected_commitment=digest)
    if change_at == 2:
        assert store.writes == []
    elif change_at == 3:
        assert "money-migration" in store.data and "money" not in store.data
    else:
        assert "money" in store.data  # late change is reported, not destructively undone


class Metadata:
    def __init__(self):
        self.source = {"permissions": [{"id": "owner", "type": "user"}, {"id": "sa", "type": "user"}]}
        self.backup = {"permissions": deepcopy(self.source["permissions"]),
                       "mimeType": "application/vnd.google-apps.spreadsheet", "trashed": False}
    def files(self):
        return self
    def get(self, fileId, **kwargs):
        assert kwargs["supportsAllDrives"] is True
        def execute(**args):
            assert args == {"num_retries": 0}
            return self.source if fileId == "source" else self.backup
        return SimpleNamespace(execute=execute)


@pytest.mark.parametrize("failure", ["same", "public", "new-user", "domain", "trashed", "not-native", "no-grants"])
def test_backup_must_be_separate_native_and_within_source_sharing(failure):
    service, backup_id = Metadata(), "backup"
    if failure == "same": backup_id = "source"
    elif failure == "trashed": service.backup["trashed"] = True
    elif failure == "not-native": service.backup["mimeType"] = "application/json"
    elif failure == "no-grants": service.backup["permissions"] = []
    else: service.backup["permissions"].append({"id": "other", "type": {"public": "anyone", "new-user": "user", "domain": "domain"}[failure]})
    with pytest.raises(StateError, match="backup_required|backup_sharing_mismatch"):
        migration.verify_backup_access(service, "source", backup_id)


@pytest.fixture(scope="module")
def private_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048).private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()


def test_key_info_uses_only_public_fingerprint_and_backup_ciphertext_has_distinct_label(private_key, monkeypatch):
    key = serialization.load_pem_private_key(private_key.encode(), password=None)
    expected = hashlib.sha256(key.public_key().public_bytes(serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo)).hexdigest()
    monkeypatch.setattr("app.settings.service_account_source", lambda: (None, {"private_key": private_key}))
    monkeypatch.setattr("app.google_clients.drive_service", lambda: pytest.fail("key-info must not call Google"))
    assert migration.run({}, operation="key-info") == {"public_key_sha256": expected}
    cipher = wrap(migration.BACKUP_VARIABLE, "synthetic-backup", private_key)
    assert unwrap(migration.BACKUP_VARIABLE, cipher, private_key) == "synthetic-backup"
    with pytest.raises(StateError, match="decode_failed"):
        unwrap("KAKEIBO_DAILY_SPREADSHEET_ID", cipher, private_key)


@pytest.mark.parametrize("message,expected", [("private ID and amount", "ledger_daily_migration_failed"),
    ("migration_commitment_mismatch", "migration_commitment_mismatch")])
def test_entrypoint_sanitizes_exception_and_has_no_success_on_failure(message, expected, monkeypatch, capsys):
    for name, value in environment().items(): monkeypatch.setenv(name, value)
    monkeypatch.setattr(migration.subprocess, "check_output", lambda *a, **kw: SHA)
    monkeypatch.setattr("sys.argv", ["migration", "inspect", "--expected-sha", SHA])
    def fail(*args, **kwargs): raise RuntimeError(message)
    monkeypatch.setattr(migration, "run", fail)
    with pytest.raises(SystemExit) as error: migration.main()
    assert error.value.code == 1
    output = capsys.readouterr()
    assert json.loads(output.out) == {"success": False, "error": expected} and output.err == ""


def test_workflow_is_manual_same_lock_and_minimal_existing_credentials():
    path = Path(__file__).resolve().parents[1] / ".github/workflows/ledger-daily-migration.yml"
    raw = path.read_text(encoding="utf-8")
    workflow = yaml.load(raw, Loader=yaml.BaseLoader)
    assert set(workflow["on"]) == {"workflow_dispatch"}
    assert workflow["permissions"] == {"contents": "read"}
    assert workflow["concurrency"] == {"group": "kakeibo-production", "cancel-in-progress": "false", "queue": "max"}
    assert "runner.debug == '1'" in raw
    assert "GMAIL" not in raw and "GEMINI" not in raw and "upload-artifact" not in raw
    secrets = re.findall(r"secrets\.([A-Z_]+)", raw)
    assert set(secrets) == {"SPREADSHEET_ID", "GOOGLE_SERVICE_ACCOUNT_JSON"}
    guard = workflow["jobs"]["migrate"]["if"]
    assert "github.sha == inputs.expected_sha" in guard and "github.sha == vars.KAKEIBO_VALIDATED_MAIN_SHA" in guard
    assert "vars.KAKEIBO_PRODUCTION_ENABLED != 'true'" in guard
    assert "vars.KAKEIBO_SCHEDULE_ENABLED != 'true'" in guard
    assert "vars.KAKEIBO_LEGACY_DISABLED == 'true'" in guard


def test_actual_runtime_uses_read_only_sheets_and_precreated_drive_files(private_key, monkeypatch):
    from test_projection_store import Drive
    from test_projection_cache import provision
    raw, backup, _ = fixture()
    raw["spreadsheet_id"] = "source"
    backup["spreadsheet_id"] = "synthetic-backup"
    class NativeDrive(Drive):
        def get(self, fileId, **kwargs):
            if fileId == "synthetic-backup":
                return self.req({"mimeType": "application/vnd.google-apps.spreadsheet", "permissions": [self.owner]})
            return super().get("folder" if fileId == "synthetic-folder" else fileId, **kwargs)
    drive = NativeDrive()
    provision(drive)
    calls = []
    class ReadOnlyDrive:
        def files(self): return self
        def get(self, **kwargs): return drive.get(**kwargs)
        def list(self, **kwargs): return drive.list(**kwargs)
        def get_media(self, **kwargs): return drive.get_media(**kwargs)
    class ReadOnlySheets:
        def spreadsheets(self): return self
        def values(self): return self
        def get(self, spreadsheetId, **kwargs):
            rows = raw if spreadsheetId == "source" else backup
            calls.append((spreadsheetId, deepcopy(kwargs)))
            if "range" in kwargs:
                rng = kwargs["range"]
                title = rng.split("!")[0].strip("'")
                key = {"支出明細": "expenses", "取込データ": "imports"}[title]
                first, last = [int(v) for v in re.findall(r"\d+", rng)]
                assert last - first < 2000
                value = {"values": deepcopy(rows[key][first - 2:last - 1])}
            else:
                value = {"sheets": [{"properties": {"title": title, "gridProperties": {"rowCount": len(rows[key]) + 1}}}
                    for title, key in [("支出明細", "expenses"), ("取込データ", "imports")]]}
            return SimpleNamespace(execute=lambda: value)
    selected = []
    def service(kind, value):
        selected.append(kind)
        return value
    monkeypatch.setattr("app.settings.service_account_source", lambda: (None, {"private_key": private_key}))
    monkeypatch.setattr("app.google_clients.drive_service", lambda: service("drive-write", drive))
    monkeypatch.setattr("app.google_clients.read_only_drive_service", lambda: service("drive-read", ReadOnlyDrive()))
    monkeypatch.setattr("app.google_clients.read_only_sheets_service", lambda: service("sheets-read", ReadOnlySheets()))
    monkeypatch.setattr("app.google_clients.sheets_service", lambda: pytest.fail("No writable Sheets credential allowed"))
    env = {"SPREADSHEET_ID": "source", "KAKEIBO_PROJECTION_FOLDER_ID": wrap("KAKEIBO_PROJECTION_FOLDER_ID", "synthetic-folder", private_key)}
    encrypted_backup = wrap(migration.BACKUP_VARIABLE, "synthetic-backup", private_key)
    initial = deepcopy(drive.payloads)
    report = migration.run(env, operation="inspect", cutover_day=DAY, encrypted_backup=encrypted_backup)
    assert selected == ["drive-read", "sheets-read"] and drive.payloads == initial
    result = migration.run(env, operation="initialize", cutover_day=DAY, encrypted_backup=encrypted_backup,
        expected_commitment=report["manifest_sha256"])
    assert selected[-2:] == ["drive-write", "sheets-read"] and result["counts"]["money_initialized"] == 1
    assert drive.create_count == 0 and len(drive.rows) == 24
    changed = {drive.rows[k]["name"] for k, v in drive.payloads.items() if v != initial[k]}
    assert changed == {"kakeibo-projection-money.json", "kakeibo-projection-money-migration.json", "kakeibo-projection-money-notices.json"}
    saved = deepcopy(drive.payloads)
    replay = migration.run(env, operation="initialize", cutover_day=DAY, encrypted_backup=encrypted_backup,
        expected_commitment=report["manifest_sha256"])
    assert replay["counts"]["money_initialized"] == 0 and drive.payloads == saved
    assert {sid for sid, _ in calls} == {"source", "synthetic-backup"}
