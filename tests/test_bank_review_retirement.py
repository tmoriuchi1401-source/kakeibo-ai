from copy import deepcopy
import hashlib
from types import SimpleNamespace

import pytest

from app.bank_review_groups import make_groups
from app.bank_review_retirement import BankReviewRetirementReader
from app.bank_review_ui import build_rows
from test_bank_review_ui import transaction

INBOX, PROCESSED = "A" * 20, "B" * 20
DATA = b"synthetic original"


def rig():
    group = make_groups([transaction(pdf_sha256=hashlib.sha256(DATA).hexdigest())])[0]
    rows = build_rows([group])
    rows[0][2:6] = ["対象外", "", "今回のみ", False]
    class Drive:
        def __init__(self):
            self.item = {"id": "synthetic_file_id", "mimeType": "application/pdf", "size": str(len(DATA)),
                "parents": [PROCESSED], "modifiedTime": "2026-10-01T01:00:00Z", "trashed": False,
                "appProperties": {"kakeiboBankPdfProcessedAt": "2026-10-01T01:00:00+00:00"}}
            self.reads = 0
            self.on_read = None
        def files(self): return self
        def get(self, **kwargs):
            assert kwargs["fileId"] == "synthetic_file_id"
            self.reads += 1
            if self.on_read: self.on_read(self)
            return self
        def execute(self): return deepcopy(self.item)
    drive = Drive()
    return drive, rows, SimpleNamespace(groups=[]), BankReviewRetirementReader(drive, INBOX, PROCESSED, download=lambda _: DATA)


def test_exact_processed_original_is_read_twice_without_any_write_or_folder_scan():
    drive, rows, source, reader = rig()
    result = reader(rows, source)
    assert result["synthetic_file_id"]["pdf_sha256"] == hashlib.sha256(DATA).hexdigest()
    assert len(result["synthetic_file_id"]["fingerprint"]) == 64 and drive.reads == 2


@pytest.mark.parametrize("fault", ["inbox", "both", "elsewhere", "unmarked", "bad_marker", "naive_marker", "mime", "trashed", "size", "bytes", "id"])
def test_disappearance_or_unverified_original_is_not_proof_of_processed(fault):
    drive, rows, source, reader = rig()
    if fault == "inbox": drive.item["parents"] = [INBOX]
    if fault == "both": drive.item["parents"] = [INBOX, PROCESSED]
    if fault == "elsewhere": drive.item["parents"] = ["C" * 20]
    if fault == "unmarked": drive.item["appProperties"] = {}
    if fault == "bad_marker": drive.item["appProperties"]["kakeiboBankPdfProcessedAt"] = "old"
    if fault == "naive_marker": drive.item["appProperties"]["kakeiboBankPdfProcessedAt"] = "2026-10-01T01:00:00"
    if fault == "mime": drive.item["mimeType"] = "text/plain"
    if fault == "trashed": drive.item["trashed"] = True
    if fault == "size": drive.item["size"] = str(21 * 1024 * 1024)
    if fault == "bytes": reader.download = lambda _: b"different original"
    if fault == "id": drive.item["id"] = "different_file"
    assert reader(rows, source) == {}


def test_metadata_changed_while_downloaded_is_fail_closed():
    drive, rows, source, reader = rig()
    def changed(current):
        if current.reads == 2: current.item["modifiedTime"] = "2026-10-01T02:00:00Z"
    drive.on_read = changed
    with pytest.raises(ValueError, match="original_changed"):
        reader(rows, source)


@pytest.mark.parametrize("skip", ["financial", "unsent", "current"])
def test_only_removed_saved_nonfinancial_candidates_read_processed_originals(skip):
    drive, rows, source, reader = rig()
    if skip == "financial": rows[0][2:4] = ["収入", "利息"]
    if skip == "unsent": rows[0][5] = True
    if skip == "current": source.groups = [{"key": rows[0][6]}]
    assert reader(rows, source) == {} and drive.reads == 0


def test_missing_file_retains_review_but_transient_api_error_does_not_trigger_write():
    drive, rows, source, reader = rig()
    class Missing(Exception): resp = SimpleNamespace(status=404)
    def missing(*args): raise Missing()
    drive.on_read = missing
    assert reader(rows, source) == {}
    def transient(*args): raise TimeoutError("private data")
    drive.on_read = transient
    with pytest.raises(ValueError, match="^bank_review_retirement_read_failed$"):
        reader(rows, source)


@pytest.mark.parametrize("point", ["download", "second_metadata"])
def test_private_api_failure_is_replaced_by_a_fixed_reason(point):
    drive, rows, source, reader = rig()
    def error(*args): raise TimeoutError("private original id and credential")
    if point == "download": reader.download = error
    else:
        def changed(current):
            if current.reads == 2: error()
        drive.on_read = changed
    with pytest.raises(ValueError, match="^bank_review_retirement_read_failed$"):
        reader(rows, source)
