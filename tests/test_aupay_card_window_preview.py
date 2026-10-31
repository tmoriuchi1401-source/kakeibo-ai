"""Bounded historical card Preview; synthetic Gmail/Sheets and no live network."""
import base64
from copy import deepcopy
from datetime import datetime, timedelta
import json
import sys
from types import SimpleNamespace

import pytest

from app import aupay_card_recurring as card, cli
from test_aupay_card_recurring import components, raw_message, Request, JST
from test_amazon_money import mail
from test_amazon_money_runtime import opt_in

NOW = datetime(2026, 10, 31, 19, 20, 40, tzinfo=JST)
CHECKPOINT = datetime(2026, 10, 15, 8, 55, 56, tzinfo=JST)
START = CHECKPOINT - timedelta(days=2)
END = START + timedelta(days=14)


def parts(tmp_path, *, max_batch=100):
    result = components(tmp_path, max_batch=max_batch)
    path = result[4].path
    policy = json.loads(path.read_text(encoding="utf-8"))
    policy.update(overlap_seconds=172800, max_window_seconds=1209600, max_messages=500)
    path.write_text(json.dumps(policy), encoding="utf-8")
    result[2].record(dict(run_id="checkpoint", status="noop",
        source_window_start=(CHECKPOINT-timedelta(days=1)).isoformat(),
        source_window_end=CHECKPOINT.isoformat()), advance_checkpoint=True)
    return result


class Gmail:
    def __init__(self, raws=(), *, malformed=None, missing_raw=False, wrong_id=False):
        self.raws = list(raws)
        self.list_calls = []
        self.gets = []
        self.malformed, self.missing_raw, self.wrong_id = malformed, missing_raw, wrong_id
    def users(self): return self
    def messages(self): return self
    def list(self, **kw):
        self.list_calls.append(kw)
        if self.malformed is not None: return Request(self.malformed)
        start = int(kw.get("pageToken") or 0)
        stop = min(start + kw["maxResults"], len(self.raws))
        response = {"messages": [{"id": str(i)} for i in range(start, stop)]}
        if stop < len(self.raws): response["nextPageToken"] = str(stop)
        return Request(response)
    def get(self, **kw):
        self.gets.append(kw["id"])
        return Request({"id": "unexpected" if self.wrong_id else kw["id"],
                        "raw": "" if self.missing_raw else self.raws[int(kw["id"])]})


def run(result, gmail, **kw):
    repo, state_dir, state, key, authority, db = result
    return card.run_recurring_ingestion(
        gmail_service=gmail, db=db, state=state, key_provider=key,
        authority_provider=authority, state_dir=state_dir, repo_root=repo,
        now=NOW, dry_run=True, preview_window=(START, END), sleeper=lambda _: None, **kw,
    )


def test_fixed_first_window_uses_real_now_and_retains_normal_guard(tmp_path):
    p = parts(tmp_path); policy = p[4].load()
    with pytest.raises(RuntimeError, match="incremental_window_exceeds_authority"):
        card.build_incremental_window(policy, p[2], NOW)
    window = card.build_explicit_preview_window(policy, p[2], NOW, START, END)
    assert window.start == START and window.end == END
    assert (window.end-window.start).total_seconds() == policy.max_window_seconds
    assert f"after:{int(START.timestamp())}" in window.query_representation
    assert f"before:{int(END.timestamp())}" in window.query_representation
    assert p[2].successful_window_end() == CHECKPOINT


@pytest.mark.parametrize("start,end,now,reason", [
    (START+timedelta(seconds=1), END, NOW, "checkpoint_mismatch"),
    (START-timedelta(seconds=1), END, NOW, "checkpoint_mismatch"),
    (START, END+timedelta(seconds=1), NOW, "exceeds_authority"),
    (START, CHECKPOINT, NOW, "does_not_advance_coverage"),
    (START, END, END-timedelta(seconds=1), "in_future"),
    (START.replace(tzinfo=None), END, NOW, "timezone_required"),
    (START, END, NOW.replace(tzinfo=None), "timezone_required"),
    (START, END.replace(microsecond=1), NOW, "second_precision_required"),
    (START, END, datetime(2027, 9, 13, tzinfo=JST), "authority_expired_or_not_started"),
])
def test_invalid_window_never_changes_checkpoint(tmp_path, start, end, now, reason):
    p = parts(tmp_path)
    before = (p[1]/"recurring.sqlite3").read_bytes()
    with pytest.raises(RuntimeError, match=reason):
        card.build_explicit_preview_window(p[4].load(), p[2], now, start, end)
    assert (p[1]/"recurring.sqlite3").read_bytes() == before


def test_changed_or_missing_checkpoint_is_not_substituted(tmp_path):
    p = parts(tmp_path); policy = p[4].load()
    for cp in (None, CHECKPOINT+timedelta(seconds=1)):
        with pytest.raises(RuntimeError, match="checkpoint_required|checkpoint_mismatch"):
            card.build_explicit_preview_window(policy, SimpleNamespace(successful_window_end=lambda:cp), NOW, START, END)


def test_explicit_window_rejects_apply_before_reading_authority():
    class Forbidden:
        def load(self): raise AssertionError("authority_must_not_be_read")
    with pytest.raises(RuntimeError, match="explicit_card_window_requires_preview"):
        card.run_recurring_ingestion(gmail_service=None, db=None, state=None,
            key_provider=Forbidden(), authority_provider=Forbidden(), state_dir="unused", repo_root="unused",
            now=NOW, dry_run=False, preview_window=(START, END))


def test_canary_cannot_be_combined_with_explicit_window(tmp_path):
    p = parts(tmp_path)
    with pytest.raises(RuntimeError, match="explicit_card_window_canary_forbidden"):
        run(p, Gmail(), money_canary=True)


def test_preview_has_no_financial_transport_or_checkpoint_advance(tmp_path, monkeypatch):
    p = parts(tmp_path); gmail = Gmail([raw_message()])
    def forbidden(*_, **__): raise AssertionError("write_path_forbidden")
    for name in ("create_batch_projection", "create_batch_manifest", "issue_recurring_batch_capability",
                 "execute_production_batch_once", "SealedSheetsBatchTransport", "SqliteBatchManifestStore",
                 "SqliteBatchCapabilityStore", "SqliteBatchJournal", "SqliteLeaseManager"):
        monkeypatch.setattr(card, name, forbidden)
    result = run(p, gmail)
    assert result["failure"] == result["written"] == result["write_requests"] == p[5].write_calls == 0
    assert result["new_eligible"] == 1 and result["status"] == "dry_run_ready"
    assert p[2].successful_window_end() == CHECKPOINT and p[5].rows == []
    assert not any((p[1]/n).exists() for n in ("manifests.sqlite3", "capabilities.sqlite3", "journal.sqlite3", "leases.sqlite3"))
    # Existing Preview records one local dry-run summary, not a successful checkpoint.
    import sqlite3
    with sqlite3.connect(p[2].path) as con:
        assert con.execute("SELECT COUNT(*) FROM recurring_runs").fetchone()[0] == 2
    assert gmail.list_calls[0]["maxResults"] == 100


@pytest.mark.parametrize("count,success", [(500, True), (501, False)])
def test_gmail_limit_never_truncates_to_a_successful_preview(tmp_path, count, success):
    p = parts(tmp_path); gmail = Gmail([raw_message()]*count)
    result = run(p, gmail)
    assert (result["failure"] == 0) is success
    assert len(gmail.gets) == 500
    assert p[2].successful_window_end() == CHECKPOINT and p[5].write_calls == 0
    if not success: assert result["failure_reason"] == "incremental_gmail_collection_incomplete"


@pytest.mark.parametrize("kwargs", [
    {"malformed": {"messages": [{"id":"0"}, {"id":"0"}]}},
    {"malformed": {"messages": [{"id":" 0"}]}},
    {"malformed": {"messages": [], "nextPageToken":"more"}},
    {"malformed": {"messages": [{"id":str(i)} for i in range(101)]}},
    {"wrong_id": True}, {"missing_raw": True},
])
def test_bad_collection_stops_without_financial_writes(tmp_path, kwargs):
    p = parts(tmp_path); result = run(p, Gmail([raw_message()], **kwargs))
    assert result["failure"] == 1 and result["failure_reason"] == "incremental_gmail_collection_incomplete"
    assert p[5].write_calls == 0 and p[2].successful_window_end() == CHECKPOINT


def unique_messages(count):
    raws=[]
    for i in range(count):
        raw=base64.urlsafe_b64decode(raw_message(amount=str(1000+i))+"===")
        raw=raw.replace(b"<recurring-fixture@example.invalid>",f"<item-{i}@example.invalid>".encode())
        raws.append(base64.urlsafe_b64encode(raw).decode())
    return raws


@pytest.mark.parametrize("count,success", [(100, True), (101, False)])
def test_batch_limit_is_not_a_candidate_slice(tmp_path, count, success):
    p=parts(tmp_path); result=run(p,Gmail(unique_messages(count)))
    assert (result["failure"] == 0) is success
    assert p[5].write_calls == 0 and p[2].successful_window_end() == CHECKPOINT
    if not success: assert result["failure_reason"] == "new_eligible_exceeds_bounded_batch"


def test_authority_status_restriction_is_checked_during_preview(tmp_path):
    p=parts(tmp_path); path=p[4].path
    policy=json.loads(path.read_text());policy['allowed_statuses']=['matched_receipt']
    path.write_text(json.dumps(policy),encoding='utf-8')
    result=run(p,Gmail([raw_message()]))
    assert result['failure_reason']=='recurring_authority_scope_mismatch'
    assert p[5].write_calls==0 and p[2].successful_window_end()==CHECKPOINT


def amazon_message():
    body="本会員さま ご利用分\nNo.1 --------\n▼ご利用日\n2026年9月11日\n▼ご利用先\nAMAZON.CO.JP\n▼ご利用金額\n1000円"
    return base64.urlsafe_b64encode(mail("【ご利用詳細】au PAY カード",body,sender="info@kddi-fs.com")).decode()


def test_money_preview_uses_no_apply_prepare_or_projection_writes(tmp_path, monkeypatch):
    store,ledger,writer=opt_in(monkeypatch);before=deepcopy(store.data)
    def forbidden(*_,**__):raise AssertionError('money_write_forbidden')
    monkeypatch.setattr(writer,'apply',forbidden);writer.prepare=forbidden
    p=parts(tmp_path);result=run(p,Gmail([amazon_message()]))
    assert result['failure']==0 and result['money_eligible']==1
    assert store.data==before and ledger.calls==[] and p[5].write_calls==0
    assert p[2].successful_window_end()==CHECKPOINT


def test_money_and_legacy_share_the_same_batch_limit(tmp_path, monkeypatch):
    store,ledger,writer=opt_in(monkeypatch);before=deepcopy(store.data)
    p=parts(tmp_path);result=run(p,Gmail(unique_messages(100)+[amazon_message()]))
    assert result['failure']==1 and result['failure_reason']=='money_batch_limit_exceeded'
    assert store.data==before and ledger.calls==[] and p[2].successful_window_end()==CHECKPOINT


def test_money_pending_stops_before_gmail(tmp_path, monkeypatch):
    _,ledger,writer=opt_in(monkeypatch)
    monkeypatch.setattr(writer,'_book',lambda:{'records':{'synthetic':{'state':'pending'}}})
    p=parts(tmp_path);gmail=Gmail();result=run(p,gmail)
    assert result['failure_reason']=='money_reconciliation_required'
    assert gmail.list_calls==[] and ledger.calls==[] and p[2].successful_window_end()==CHECKPOINT


@pytest.mark.parametrize("flags,reason", [
    (["--preview-window-start",START.isoformat()],"preview_window_both_bounds_required"),
    (["--preview-window-start",START.isoformat(),"--preview-window-end",END.isoformat(),"--money-canary"],"explicit_card_window_canary_forbidden"),
])
def test_cli_invalid_preview_bounds_stop_before_state_initialization(monkeypatch, capsys, flags, reason):
    monkeypatch.setattr(cli,'Settings',lambda:pytest.fail('settings_must_not_be_loaded'))
    monkeypatch.setattr(sys,'argv',['kakeibo','card-gmail-recurring','--dry-run',*flags])
    with pytest.raises(SystemExit):cli.main()
    assert json.loads(capsys.readouterr().out)['failure_reason']==reason


def test_cli_explicit_window_without_dry_run_is_rejected(monkeypatch,capsys):
    monkeypatch.setattr(cli,'Settings',lambda:pytest.fail('settings_must_not_be_loaded'))
    monkeypatch.setattr(sys,'argv',['kakeibo','card-gmail-recurring','--preview-window-start',START.isoformat(),'--preview-window-end',END.isoformat()])
    with pytest.raises(SystemExit):cli.main()
    assert json.loads(capsys.readouterr().out)['failure_reason']=='explicit_card_window_requires_preview'
