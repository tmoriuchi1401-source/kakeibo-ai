from copy import deepcopy
from dataclasses import replace
import hashlib

import pytest

from app.bank_review_source import BankReviewSourceReader
from test_bank_review_candidates import evidence


FOLDER = "A" * 20


def rig(monkeypatch, *, interest=False):
    parsed, options = evidence(description="普通預金利息" if interest else "振込 特典", imported=False)
    class DB:
        def __init__(self):
            self.ledgers = {"取込データ!A2:L": [], "収入明細!A2:J": [], "支出明細!A2:M": []}
            self.reads = 0
            self.on_read = None
        def get(self, rng):
            self.reads += 1
            if self.on_read:
                self.on_read(self, rng)
            return deepcopy(self.ledgers[rng])
    class Drive:
        def __init__(self):
            self.items = [{"id": "synthetic_pdf", "name": "synthetic.pdf", "mimeType": "application/pdf",
                           "parents": [FOLDER], "size": "13", "modifiedTime": "2026-10-01T00:00:00Z"}]
            self.token = None
            self.reads = 0
            self.on_read = None
        def files(self): return self
        def list(self, **kwargs):
            assert "trashed=false" in kwargs["q"] and kwargs["pageSize"] == 21
            self.reads += 1
            if self.on_read:
                self.on_read(self)
            return self
        def execute(self):
            return {"files": deepcopy(self.items), "nextPageToken": self.token}
    db, drive = DB(), Drive()
    reader = BankReviewSourceReader(db, drive, FOLDER, legacy_rules={}, download=lambda _: b"synthetic pdf")
    monkeypatch.setattr("app.bank_review_source.BankPdfPipeline.parse", lambda *args, **kwargs: parsed)
    return db, drive, reader, parsed


def test_group_binds_actual_pdf_digest_and_reads_source_and_ledgers_twice(monkeypatch):
    db, drive, reader, parsed = rig(monkeypatch)
    source = reader()
    assert len(source.groups) == len(source.transactions) == 1
    assert source.groups[0]["members"][0]["pdf_sha256"] == hashlib.sha256(b"synthetic pdf").hexdigest()
    assert source.files[0]["parsed"] == parsed and drive.reads == 2 and db.reads == 6
    assert not source.confirmed_income


def test_unposted_interest_is_separate_settlement_candidate_not_a_meaning_question(monkeypatch):
    _, _, reader, _ = rig(monkeypatch, interest=True)
    source = reader()
    assert not source.groups and len(source.confirmed_income) == 1
    assert source.confirmed_income[0].category == "利息"


@pytest.mark.parametrize("fault", ["issue", "balance", "zero", "collision"])
def test_original_anomalies_cannot_offer_group_approval(monkeypatch, fault):
    _, _, reader, parsed = rig(monkeypatch)
    altered = {"issue": replace(parsed, issues=(object(),)),
               "balance": replace(parsed, balance_consistency_failures=1),
               "zero": replace(parsed, transactions=()),
               "collision": replace(parsed, transactions=parsed.transactions * 2)}[fault]
    monkeypatch.setattr("app.bank_review_source.BankPdfPipeline.parse", lambda *args, **kwargs: altered)
    with pytest.raises(ValueError):
        reader()


@pytest.mark.parametrize("fault", ["pagination", "overflow", "duplicate", "parent", "processed", "mime", "size"])
def test_drive_scope_and_bounds_fail_closed(monkeypatch, fault):
    _, drive, reader, _ = rig(monkeypatch)
    if fault == "pagination": drive.token = "next"
    if fault == "overflow": drive.items = [{**drive.items[0], "id": str(i)} for i in range(21)]
    if fault == "duplicate": drive.items *= 2
    if fault == "parent": drive.items[0]["parents"] = ["B" * 20]
    if fault == "processed": drive.items[0]["appProperties"] = {"kakeiboBankPdfProcessedAt": "2026-10-01"}
    if fault == "mime": drive.items[0]["mimeType"] = "text/plain"
    if fault == "size": drive.items[0]["size"] = str(21 * 1024 * 1024)
    with pytest.raises(ValueError):
        reader()


def test_source_metadata_change_during_read_does_not_offer_stale_groups(monkeypatch):
    _, drive, reader, _ = rig(monkeypatch)
    def changed(current):
        if current.reads == 2:
            current.items[0]["modifiedTime"] = "2026-10-01T00:01:00Z"
    drive.on_read = changed
    with pytest.raises(ValueError, match="source_changed_during_read"):
        reader()


def test_ledger_change_during_read_does_not_offer_stale_groups(monkeypatch):
    db, _, reader, _ = rig(monkeypatch)
    def changed(current, rng):
        if current.reads == 5:
            current.ledgers[rng] = [["changed"]]
    db.on_read = changed
    with pytest.raises(ValueError, match="source_changed_during_read"):
        reader()


def test_existing_import_content_mismatch_is_not_only_a_duplicate(monkeypatch):
    db, _, reader, parsed = rig(monkeypatch)
    tx = parsed.transactions[0]
    db.ledgers["取込データ!A2:L"] = [[tx.source_row_identity, "", tx.source, tx.source_row_identity,
        tx.transaction_date, tx.description, tx.signed_amount + 1, "銀行口座", "bank_income", "", tx.source_row_hash, ""]]
    with pytest.raises(ValueError, match="existing_import_mismatch"):
        reader()
