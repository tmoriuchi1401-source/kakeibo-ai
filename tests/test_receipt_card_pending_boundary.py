"""Actual parent/native paths with synthetic pending card and unchanged writers."""
from copy import deepcopy
import json
import pytest

from app import production_flow as flow
from app.drive_run_state import StateError
from test_isolated_paypay_intake import quarantine, main_env
from test_production_integration import integrated
from test_receipt_hosted_connection import native, RAW, SID


def test_manual_receipts_cannot_avoid_global_pending_guard(integrated,monkeypatch,capsys):
    f=integrated;quarantine(f);main_env(monkeypatch,'receipts')
    before=deepcopy(f.payloads)
    with pytest.raises(SystemExit):flow.main()
    assert not json.loads(capsys.readouterr().out)['success']
    assert not f.calls and not f.swrites and not f.dwrites and not f.ai_calls
    assert f.payloads==before


def test_native_receipt_cannot_treat_unresolved_card_as_disjoint(monkeypatch):
    runtime,_,_,_,db,_,parent=native(monkeypatch)
    parent.begin('aupay_card','c'*32)
    card=deepcopy(parent.value['sources']['aupay_card'])
    with pytest.raises(StateError,match='source_reconciliation_required'):
        runtime.process_bytes(RAW,'image/png',SID)
    assert db.calls==0
    assert parent.value['sources']['aupay_card']==card


def test_pending_created_during_post_stops_next_append_without_gap_fill(monkeypatch):
    runtime,_,_,_,db,_,parent=native(monkeypatch)
    original=db.append_raw
    def first_then_pending(title,rows):
        original(title,rows)
        if db.calls==1:parent.begin('aupay_card','c'*32)
    db.append_raw=first_then_pending
    with pytest.raises(StateError,match='write_unknown'):
        runtime.process_bytes(RAW,'image/png',SID)
    assert db.calls==1
    before=deepcopy(db.rows)
    with pytest.raises(StateError,match='unknown_no_retry'):runtime.owner.read()
    assert db.calls==1 and db.rows==before
