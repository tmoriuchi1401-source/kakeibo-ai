"""Exact overlap is one transaction with every original still bound to proof."""
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
import hashlib

import pytest

from app.bank_review_groups import encoded, make_groups, validate_snapshot
from app.bank_review_refresh import build_view
from app.bank_review_requests import BankReviewRequestProcessor
from app.bank_review_ui import build_rows, validate_rows
from test_bank_review_source import rig
from test_bank_review_requests import MemoryBankDB, REQUEST, NOW


def overlap(monkeypatch, *, interest=False):
    db, drive, reader, parsed = rig(monkeypatch, interest=interest)
    template = parsed.transactions[0]
    def tx(index):
        sha = hashlib.sha256(f"synthetic-bank-event-{index}".encode()).hexdigest()
        return replace(template, source_row_hash=sha,
            source_row_identity="bankpdf:au-jibun:jibun-primary:" + sha[:24],
            description=template.description if interest else f"{template.description} {index % 5}",
            signed_amount=100 + index, source_page=1, source_row=index + 1)
    shared = tuple(tx(i) for i in range(50))
    left = replace(parsed, transactions=shared + tuple(tx(i) for i in range(50, 53)))
    right = replace(parsed, transactions=tuple(replace(t, source_page=2, source_row=i + 1)
        for i, t in enumerate(shared)) + tuple(tx(i) for i in range(53, 57)))
    drive.items = [{**drive.items[0], "id": name} for name in ("synthetic_pdf_C", "synthetic_pdf_E")]
    documents = {"synthetic_pdf_C": left, "synthetic_pdf_E": right}
    blobs = {key: key.encode() for key in documents}
    reader.download = lambda file_id: blobs[file_id]
    monkeypatch.setattr("app.bank_review_source.BankPdfPipeline.parse",
        lambda self, path, **kwargs: documents[path.read_bytes().decode()])
    return db, drive, reader, documents, blobs


def test_fifty_exact_overlaps_count_once_with_both_positions_and_unique_events(monkeypatch):
    db, drive, reader, documents, blobs = overlap(monkeypatch)
    source = reader()
    assert len(source.transactions) == 57
    assert sum(group["count"] for group in source.groups) == 57
    assert sum(group["total_amount"] for group in source.groups) == sum(range(100, 157))
    members = {row["identity"]: row for group in source.groups for row in group["members"]}
    for tx in documents["synthetic_pdf_C"].transactions[:50]:
        row = members[tx.source_row_identity]
        assert row["file_id"] == "synthetic_pdf_C"
        assert row["originals"] == [
            dict(file_id="synthetic_pdf_C", pdf_sha256=hashlib.sha256(blobs["synthetic_pdf_C"]).hexdigest(),
                 page=1, row=tx.source_row),
            dict(file_id="synthetic_pdf_E", pdf_sha256=hashlib.sha256(blobs["synthetic_pdf_E"]).hexdigest(),
                 page=2, row=tx.source_row)]
    assert sum("originals" in member for member in members.values()) == 50
    assert [len(file["parsed"].transactions) for file in source.files] == [53, 54]
    validate_rows(build_rows(source.groups))
    for group in source.groups:
        assert validate_snapshot(encoded(group["snapshot"]), group["key"], group["membership_digest"]) == group["snapshot"]
    # Listing order cannot change the selected original or a captured proof.
    drive.items.reverse()
    assert reader() == source
    assert all(not value for value in db.ledgers.values())


@pytest.mark.parametrize("field,value", [
    ("source", "千葉銀行PDF"), ("account_alias", "other-account"),
    ("transaction_date", "2026-09-15"), ("description", "different"),
    ("signed_amount", 101), ("transaction_kind", "withdrawal"),
    ("source_row_hash", "d" * 64), ("review_status", "unknown"),
])
def test_same_id_different_content_account_balance_hash_or_unknown_still_rejected(monkeypatch, field, value):
    _, _, reader, documents, _ = overlap(monkeypatch)
    right = documents["synthetic_pdf_E"]
    documents["synthetic_pdf_E"] = replace(right,
        transactions=(replace(right.transactions[0], **{field: value}), *right.transactions[1:]))
    with pytest.raises(ValueError, match="collision"):
        reader()


def test_document_local_occurrence_number_is_not_cross_pdf_evidence(monkeypatch):
    _, _, reader, documents, _ = overlap(monkeypatch)
    for key, parsed in documents.items():
        documents[key] = replace(parsed, transactions=tuple(
            replace(t, source_row_identity=t.source_row_identity + ":001") for t in parsed.transactions))
    with pytest.raises(ValueError, match="collision"):
        reader()


@pytest.mark.parametrize("field,value", [("transaction_date", ""), ("source_row_hash", None),
    ("description", ""), ("review_status", "unknown")])
def test_matching_but_incomplete_content_is_not_exact_evidence(monkeypatch, field, value):
    from app.bank_review_source import same_transaction_content
    _, _, _, documents, _ = overlap(monkeypatch)
    first = replace(documents["synthetic_pdf_C"].transactions[0], **{field:value})
    assert not same_transaction_content(first, replace(first, source_page=2))


def test_outgoing_overlap_also_counts_one_expense_candidate_without_writes(monkeypatch):
    db, _, reader, documents, _ = overlap(monkeypatch)
    for key, parsed in documents.items():
        documents[key] = replace(parsed, transactions=tuple(replace(tx,
            signed_amount=-tx.signed_amount, transaction_kind="withdrawal") for tx in parsed.transactions))
    before = deepcopy(db.ledgers)
    source = reader()
    assert len(source.transactions) == sum(g["count"] for g in source.groups) == 57
    assert sum(g["total_amount"] for g in source.groups) == sum(range(100,157))
    assert not source.confirmed_income and db.ledgers == before


@pytest.mark.parametrize("change", ["add", "remove", "bytes", "position", "metadata"])
def test_original_scope_change_invalidates_fingerprint_or_membership(monkeypatch, change):
    _, drive, reader, documents, blobs = overlap(monkeypatch)
    before = reader()
    if change == "add":
        drive.items.append({**drive.items[0], "id": "synthetic_pdf_Z"})
        documents["synthetic_pdf_Z"] = documents["synthetic_pdf_C"]
        blobs["synthetic_pdf_Z"] = b"synthetic_pdf_Z"
    elif change == "remove":
        drive.items.pop()
    elif change == "bytes":
        blobs["synthetic_pdf_E"] = b"synthetic_pdf_E revised"
        documents["synthetic_pdf_E revised"] = documents["synthetic_pdf_E"]
    elif change == "position":
        parsed = documents["synthetic_pdf_E"]
        documents["synthetic_pdf_E"] = replace(parsed, transactions=tuple(
            replace(t, source_page=3) for t in parsed.transactions))
    else:
        drive.items[1]["modifiedTime"] = "2026-10-02T00:00:00Z"
    after = reader()
    assert before.fingerprint != after.fingerprint
    if change != "metadata":
        assert [g["membership_digest"] for g in before.groups] != [g["membership_digest"] for g in after.groups]


def test_confirmed_income_and_rule_usage_are_not_doubled(monkeypatch):
    _, _, reader, documents, _ = overlap(monkeypatch, interest=True)
    source = reader()
    assert not source.groups and len(source.confirmed_income) == 57
    assert sum(d.transaction.signed_amount for d in source.confirmed_income) == sum(range(100, 157))
    # The metadata-only application planner also revalidates overlapping PDFs.
    from test_bank_rule_usage import fixture, plan
    db, one, _, store = fixture()
    first = next(iter(one.transactions.values()))
    one.files = (one.files[0], {"pdf_sha256": "c" * 64,
        "parsed": SimpleNamespace(transactions=(replace(first, source_page=3, source_row=4),))})
    before_calls = len(db.calls)
    edits, counts = plan(db, one, store)
    assert counts["bank_rule_applications_new"] == 1
    assert len(db.calls) == before_calls
    one.files[1]["parsed"].transactions = (replace(first, signed_amount=first.signed_amount + 1),)
    with pytest.raises(ValueError, match="original_collision"):
        plan(db, one, store)


@pytest.mark.parametrize("moment", ["captured", "before_commit"])
def test_old_group_approval_cannot_transfer_to_changed_originals(monkeypatch, moment):
    _, drive, reader, _, _ = overlap(monkeypatch)
    source = reader()
    rows = build_rows(source.groups)
    for i in range(0, len(rows), 2):
        rows[i][2:6] = ["収入", "利息", "今回のみ", True]
    db = MemoryBankDB()
    calls = []
    def read():
        calls.append(1)
        if moment == "captured" or len(calls) == 2:
            drive.items = drive.items[:1]
        return reader()
    counts, output = BankReviewRequestProcessor(db, read, clock=lambda: NOW).process(rows, REQUEST)
    assert counts["bank_groups_confirmed"] == counts["bank_metadata_writes"] == counts["bank_ledger_writes"] == 0
    assert counts["bank_held"] and not db.calls
    assert all(not row[5] for row in output)


def test_unsent_controls_retained_but_checkboxes_revoked_when_overlap_changes(monkeypatch):
    _, drive, reader, _, _ = overlap(monkeypatch)
    before = reader()
    rows = build_rows(before.groups)
    for i in range(0, len(rows), 2):
        rows[i][2:6] = ["自己口座間振替", "", "今回のみ", True]
        rows[i+1][2:5] = ["千葉銀行", "chiba-primary", True]
    drive.items.pop()
    current = reader()
    updated = build_rows(current.groups, rows)
    old = {row[6]: row for row in rows}
    for row in updated:
        if row[6] in old:
            assert row[2:4] == old[row[6]][2:4]
            assert row[4 if row[8] == "sender" else 5] is False
    validate_rows(updated)


def test_retirement_checks_both_originals_and_does_not_resolve_after_only_one(monkeypatch):
    from test_bank_review_refresh import rig as view_rig
    from app.bank_review_retirement import BankReviewRetirementReader
    from app.bank_review_groups import member_originals
    # Use a nonfinancial saved answer for the retirement path.
    db, source, ui, submitted = view_rig()
    from test_bank_review_requests import processor
    member = source.groups[0]["members"][0]
    member["pdf_sha256"] = "b" * 64
    member["originals"] = [{key: member[key] for key in ("file_id", "pdf_sha256", "page", "row")},
        dict(file_id="synthetic_other_pdf", pdf_sha256="c" * 64, page=2, row=4)]
    member["originals"].sort(key=lambda x: x["file_id"])
    # Representative is the first lexicographic original.
    member.update(member["originals"][0])
    source.groups = make_groups([member])
    submitted = build_rows(source.groups)
    submitted[0][2:6] = ["対象外", "", "今回のみ", True]
    processor(db, source).process(submitted, REQUEST)
    submitted[0][5] = False
    source.groups = []
    from app.bank_meaning_resolver import load
    resolver = load(db, legacy_rules={})
    retired = {original["file_id"]: dict(pdf_sha256=original["pdf_sha256"])
        for original in member_originals(member)}
    result, _, rows = build_view(source, resolver, submitted, retired_originals=retired)
    assert result["bank_groups_resolved"] == 1 and not rows
    retired.pop(member["originals"][1]["file_id"])
    result, _, rows = build_view(source, resolver, submitted, retired_originals=retired)
    assert result["bank_stale_groups"] == 1 and rows
    # A retirement read examines both PDF IDs, not only the representative.
    seen = []
    retirement = BankReviewRetirementReader(None, "A" * 20, "B" * 20)
    retirement._metadata = lambda file_id: seen.append(file_id)
    assert retirement(submitted, source) == {}
    assert set(seen) == {item["file_id"] for item in member["originals"]}


@pytest.mark.parametrize("fault", ["duplicate_file", "missing_sha", "position", "representative", "order"])
def test_incomplete_or_ambiguous_provenance_cannot_be_approved(monkeypatch, fault):
    _, _, reader, _, _ = overlap(monkeypatch)
    row = deepcopy(reader().groups[0]["members"][0])
    if fault == "duplicate_file": row["originals"][1]["file_id"] = row["originals"][0]["file_id"]
    if fault == "missing_sha": row["originals"][1].pop("pdf_sha256")
    if fault == "position": row["originals"][1]["page"] = 0
    if fault == "representative": row["file_id"] = "different"
    if fault == "order": row["originals"].reverse()
    with pytest.raises(ValueError, match="originals_invalid"):
        make_groups([row])
