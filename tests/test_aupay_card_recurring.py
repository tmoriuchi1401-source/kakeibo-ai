from datetime import datetime, timedelta
import base64
from email.message import EmailMessage
import json
from zoneinfo import ZoneInfo

from app.aupay_card_batch import ProtectedRecurringAuthorityProvider
from app.aupay_card_production import ProtectedAuditKeyProvider
from app.aupay_card_recurring import (
    SqliteRecurringRunState,
    build_incremental_window,
    run_recurring_ingestion,
)
from app.sheets import HEADERS


JST = ZoneInfo("Asia/Tokyo")
NOW = datetime(2026, 9, 12, 6, 0, tzinfo=JST)


def raw_message(merchant="匿名店舗", amount="1,200", *, negative=False):
    message = EmailMessage()
    message["Subject"] = "【ご利用詳細】au PAY カード"
    message["Message-ID"] = "<recurring-fixture@example.invalid>"
    marker = f"-{amount}円(返品)" if negative else f"{amount}円"
    message.set_content(f"""▼カード情報
au PAY カード
本会員さま ご利用分

No.001--------
▼ご利用日
2026年9月11日
▼ご利用金額
{marker}
▼ご利用先
{merchant}
""")
    return base64.urlsafe_b64encode(message.as_bytes()).decode().rstrip("=")


class Request:
    def __init__(self, value): self.value = value
    def execute(self): return self.value


class Messages:
    def __init__(self, raw=None, *, truncated=False):
        self.raw = raw
        self.truncated = truncated
        self.list_calls = []

    def list(self, **kwargs):
        self.list_calls.append(kwargs)
        if self.raw is None:
            return Request({"messages": []})
        value = {"messages": [{"id": "gmail-1"}]}
        if self.truncated:
            value["nextPageToken"] = "more"
        raw, self.raw = self.raw, None
        self.returned_raw = raw
        return Request(value)

    def get(self, **kwargs): return Request({"raw": self.returned_raw})


class Gmail:
    def __init__(self, raw=None, *, truncated=False):
        self.api = Messages(raw, truncated=truncated)
    def users(self): return self
    def messages(self): return self.api


class Values:
    def __init__(self, db): self.db, self.body = db, None
    def append(self, **kwargs): self.body = kwargs["body"]; return self
    def execute(self):
        self.db.write_calls += 1
        self.db.rows.extend(self.body["values"])
        return {"updates": {"updatedRows": len(self.body["values"])}}


class Spreadsheets:
    def __init__(self, db): self.api = Values(db)
    def values(self): return self.api


class Service:
    def __init__(self, db): self.api = Spreadsheets(db)
    def spreadsheets(self): return self.api


class DB:
    def __init__(self):
        self.sid = "sheet-production"
        self.rows = []
        self.write_calls = 0
        self.svc = Service(self)

    def get(self, rng):
        if rng == "取込データ!A1:L1": return [HEADERS["取込データ"]]
        if rng == "取込データ!A2:L": return self.rows
        if rng == "Amazon注文!A2:M": return []
        raise AssertionError(rng)


def components(tmp_path, *, max_batch=10):
    repo = tmp_path / "repo"; repo.mkdir(parents=True)
    state_dir = tmp_path / "state"; state_dir.mkdir()
    key = state_dir / "key.json"
    key.write_text(json.dumps({
        "key_id": "recurring-test-key",
        "key_b64": base64.b64encode(b"k" * 32).decode(),
    }), encoding="utf-8")
    authority = state_dir / "authority.json"
    authority.write_text(json.dumps({
        "policy_id": "daily-aupay-card-v1", "source": "au_pay_card_gmail",
        "expected_spreadsheet_id": "sheet-production",
        "expected_worksheet": "取込データ",
        "allowed_statuses": ["auto_expense", "matched_receipt",
                             "transfer_aupay_charge", "matched_amazon"],
        "max_batch_size": max_batch, "max_messages": max_batch,
        "overlap_seconds": 7200, "max_window_seconds": 7 * 86400,
        "initial_start": (NOW - timedelta(days=1)).isoformat(),
        "valid_from": (NOW - timedelta(days=30)).isoformat(),
        "expires_at": (NOW + timedelta(days=365)).isoformat(),
    }), encoding="utf-8")
    state = SqliteRecurringRunState(state_dir / "recurring.sqlite3", repo_root=repo)
    return (
        repo, state_dir, state, ProtectedAuditKeyProvider(key, repo_root=repo),
        ProtectedRecurringAuthorityProvider(authority, repo_root=repo), DB(),
    )


def run(parts, gmail, *, now=NOW, dry_run=False):
    repo, state_dir, state, key, authority, db = parts
    return run_recurring_ingestion(
        gmail_service=gmail, db=db, state=state, key_provider=key,
        authority_provider=authority, state_dir=state_dir, repo_root=repo,
        now=now, dry_run=dry_run, sleeper=lambda _delay: None,
    )


def test_window_is_absolute_jst_epoch_and_overlaps_last_success(tmp_path):
    parts = components(tmp_path)
    policy = parts[4].load()
    first = build_incremental_window(policy, parts[2], NOW)
    assert first.timezone_name == "Asia/Tokyo"
    assert f"after:{int(first.start.timestamp())}" in first.query_representation
    assert "newer_than:" not in first.query_representation
    parts[2].record({
        "run_id": "checkpoint", "status": "noop",
        "source_window_start": first.start.isoformat(),
        "source_window_end": first.end.isoformat(),
    }, advance_checkpoint=True)
    second = build_incremental_window(policy, parts[2], NOW + timedelta(days=1))
    assert second.start == first.end - timedelta(hours=2)


def test_new_item_is_written_once_and_overlap_rerun_is_safe_noop(tmp_path):
    parts = components(tmp_path)
    first_gmail = Gmail(raw_message())
    first = run(parts, first_gmail)
    assert first["status"] == "complete"
    assert first["new_eligible"] == first["written"] == 1
    assert first["already_present"] == 0
    assert first["write_requests"] == 1
    assert first["capability_final"] == "sealed"
    assert first["journal_final"] == "confirmed"
    assert first["lease_final"] == "released"
    assert "after:" in first_gmail.api.list_calls[0]["q"]

    second = run(parts, Gmail(raw_message()), now=NOW + timedelta(days=1))
    assert second["status"] == "noop"
    assert second["new_eligible"] == second["written"] == 0
    assert second["already_present"] == 1
    assert second["manifest_final"] == "not_created"
    assert second["capability_final"] == "not_issued"
    assert parts[5].write_calls == 1


def test_returns_and_amazon_unmatched_never_enter_automatic_batch(tmp_path):
    returned = components(tmp_path / "return")
    return_result = run(returned, Gmail(raw_message(negative=True)))
    assert return_result["status"] == "noop"
    assert return_result["withheld"] == 1
    assert return_result["written"] == returned[5].write_calls == 0

    amazon = components(tmp_path / "amazon")
    amazon_result = run(amazon, Gmail(raw_message(merchant="AMAZON.CO.JP")))
    assert amazon_result["status"] == "noop"
    assert amazon_result["review"] == 1
    assert amazon_result["written"] == amazon[5].write_calls == 0


def test_dry_run_and_incomplete_collection_do_not_advance_checkpoint(tmp_path):
    parts = components(tmp_path, max_batch=1)
    dry = run(parts, Gmail(raw_message()), dry_run=True)
    assert dry["status"] == "dry_run_ready"
    assert parts[2].successful_window_end() is None
    assert parts[5].write_calls == 0

    failed = run(parts, Gmail(raw_message(), truncated=True))
    assert failed["status"] == "failed"
    assert failed["failure"] == 1
    assert failed["write_requests"] == 0
    assert parts[2].successful_window_end() is None
    assert parts[5].write_calls == 0
