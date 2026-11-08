"""Offline private envelope adapter; injected IO only, no credentials/client.

IO has DriveRegistryIO's metadata(fid, fields), read_versioned()->(bytes, tag)
and replace_versioned(before_bytes, tag, after_bytes) contract. The last method
MUST perform one server-side Drive v2 If-Match PUT, no retry/redirect/merge.
This module neither constructs that live transport nor initializes a file.
"""
from copy import deepcopy
import json
import re
from threading import RLock

from .category_rules import parse_rules, valid_rule
from .drive_run_state import StateError
from .page_receipt_model import digest
from .receipt_item_correction_review import review
from .receipt_sheet_decoder import decode_receipt_row
from .receipt_sheet_validation import (JOURNAL_SCHEMA, PREPARED, PURPOSE,
    ValidationJournal, strong_etag, validate_journal)
from .reconciliation import parse_import_rows

SCHEMA = "receipt-private-envelope-v1"
MAX_BYTES = 1024 * 1024
FIELDS = "id,etag,version,parents(id),labels(trashed),mimeType,owners(emailAddress),permissions(type,role,emailAddress,deleted)"


def _fail(code="invalid"):
    raise StateError("receipt_private_" + code) from None


def _bytes(value):
    return json.dumps(value, ensure_ascii=True, allow_nan=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def _parse(raw):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                _fail()
            result[key] = value
        return result
    def invalid_constant(_):
        _fail()
    if type(raw) is not bytes or not 0 < len(raw) <= MAX_BYTES:
        _fail()
    value = json.loads(raw.decode("utf-8"), object_pairs_hook=unique,
                       parse_constant=invalid_constant)
    # Reject exponent overflow, non-JSON values and lone UTF-16 surrogates too.
    def inspect(node):
        if isinstance(node, dict):
            for key, child in node.items():
                key.encode("utf-8")
                inspect(child)
        elif isinstance(node, list):
            for child in node:
                inspect(child)
        elif isinstance(node, str):
            node.encode("utf-8")
    inspect(value)
    _bytes(value)
    return value


def _expected(case_id, entry):
    if set(entry) != {"anchor", "import_row", "candidate_rows", "categories", "rules", "decoder"}:
        _fail("catalog_invalid")
    anchor, row, candidates = entry["anchor"], entry["import_row"], entry["candidate_rows"]
    categories, rules, decoder = entry["categories"], entry["rules"], entry["decoder"]
    if (set(anchor) != {"source", "candidate_digest", "import_digest", "policy_digest", "total_confirmation"}
            or type(row) is not list or len(row) != 12 or row[0] != case_id
            or type(candidates) is not list or not candidates
            or any(type(r) is not list or len(r) != 11 or r[1] != case_id for r in candidates)
            or type(categories) is not list or not categories
            or any(type(c) is not list or len(c) != 2 or any(type(x) is not str or not x for x in c) for c in categories)
            or len(set(map(tuple, categories))) != len(categories)
            or type(rules) is not list or any(type(r) is not list or len(r) != 18 for r in rules)
            or set(decoder) != {"machine", "kind_label"}):
        _fail("catalog_invalid")
    if len(candidates) > 3:
        _fail("capacity_exceeded")
    if (digest(row) != anchor["import_digest"] or digest(candidates) != anchor["candidate_digest"]
            or digest([categories, rules]) != anchor["policy_digest"]):
        _fail("catalog_evidence_mismatch")
    txs, parsed_rules = parse_import_rows([row]), parse_rules(rules)
    if (len(txs) != 1 or len(parsed_rules) != len(rules)
            or any(not valid_rule(r, set(map(tuple, categories))) for r in parsed_rules)):
        _fail("catalog_invalid")
    confirmation = anchor["total_confirmation"]
    if (type(confirmation) is not dict or set(confirmation) != {"source", "receipt_total", "actor_id"}
            or confirmation["source"] != anchor["source"]
            or type(confirmation["receipt_total"]) is not int or confirmation["receipt_total"] != txs[0].amount
            or type(confirmation["actor_id"]) is not str
            or not re.fullmatch(r"[a-f0-9]{64}", confirmation["actor_id"])):
        _fail("catalog_confirmation_required")
    original = review(txs[0], list(enumerate(candidates, 2)), anchor["source"], [], categories,
                      total_confirmation=confirmation, approved_product_rules=parsed_rules)["preview"]["original_items"]
    expected = dict(case_id=case_id, source=anchor["source"], original_items=original,
                    categories=categories, context_digest=digest(anchor), receipt_total=txs[0].amount, **decoder)
    # Validate the derived decoder contract with ALL original slots, never trim.
    probe = [case_id, decoder["kind_label"], None, txs[0].amount,
             sum(i["amount"] for i in original), txs[0].amount-sum(i["amount"] for i in original), None, None, None]
    labels = {"product": "商品", "tax": "外税", "discount": "値引き"}
    slots = {i["item_id"]: i for i in original}
    for key in decoder["machine"][4:7]:
        item = slots.get(key)
        probe.extend([item["name"], item["quantity"], item["amount"], *item["category"],
                      labels[item["kind"]], "", "採用", False] if item else [None]*9)
    probe.extend(decoder["machine"])
    decode_receipt_row(probe, expected=expected)
    return deepcopy(expected)


class _Replay:
    """In-memory transition check delegated to the unchanged journal core."""
    def __init__(self, value):
        self.value, self.tag = deepcopy(value), '"before"'

    def read_versioned(self):
        return deepcopy(self.value), self.tag

    def replace_versioned(self, value, tag):
        if tag != self.tag:
            _fail("transition_invalid")
        self.value, self.tag = deepcopy(value), '"after"'


def _transition(before, after):
    replay = _Replay(before)
    journal = ValidationJournal(replay, preflight=lambda: None)
    added = set(after["requests"]) - set(before["requests"])
    changed = [rid for rid, r in before["requests"].items() if after["requests"].get(rid) != r]
    if len(added) == 1 and not changed:
        record = after["requests"][next(iter(added))]
        if record["state"] != PREPARED:
            _fail("transition_invalid")
        journal.prepare({k: record[k] for k in ("request_id", "purpose", "snapshot")}, expected_tag=replay.tag)
    elif not added and len(changed) == 1:
        rid = changed[0]
        record = after["requests"].get(rid)
        if record is None:
            _fail("transition_invalid")
        if before["requests"][rid]["state"] == PREPARED:
            journal.accept_prepared(rid, record["snapshot"]["digest"])
        else:
            journal.transition(rid, record["state"], record["result"])
    else:
        _fail("transition_invalid")
    if _bytes(replay.value) != _bytes(after):
        _fail("transition_invalid")


class ReceiptPrivateAdapter:
    """Journal dict port + read-only catalog projections, one outer ETag.

    catalog_digest is independently pinned, NEVER learned from this file/Sheet.
    After any ambiguous IO this instance is permanently GET-only. Recreating it
    to retry is forbidden; durable incident reconciliation is a future host gate.
    Metadata is the explicit Drive v2 projection above, for file AND parent.
    """
    def __init__(self, io, *, file_id, folder_id, owner_email, service_email, catalog_digest):
        if (any(type(v) is not str or not v for v in (file_id, folder_id, owner_email, service_email))
                or file_id == folder_id or owner_email == service_email
                or type(catalog_digest) is not str or not re.fullmatch(r"[a-f0-9]{64}", catalog_digest)):
            _fail("configuration_invalid")
        self.io, self.fid, self.folder = io, file_id, folder_id
        self.owner, self.service, self.catalog_digest = owner_email, service_email, catalog_digest
        self._lock, self._stopped, self._cached, self._ack = RLock(), False, None, None

    def _metadata(self):
        if (getattr(self.io, "config", {}).get("file_id") != self.fid
                or self.io.config.get("folder_id") != self.folder):
            _fail("transport_binding_mismatch")
        values = []
        grants = {("user", "owner", self.owner), ("user", "writer", self.service)}
        for fid, mime in ((self.folder, "application/vnd.google-apps.folder"), (self.fid, "application/json")):
            m = deepcopy(self.io.metadata(fid, FIELDS))
            if (m.get("id") != fid or m.get("mimeType") != mime or m.get("labels", {}).get("trashed") is not False
                    or [o.get("emailAddress") for o in m.get("owners", [])] != [self.owner]
                    or {(p.get("type"), p.get("role"), p.get("emailAddress")) for p in m.get("permissions", [])
                        if not p.get("deleted")} != grants
                    or type(m.get("version")) is not str or not m["version"]
                    or not strong_etag(m.get("etag"))
                    or fid == self.fid and m.get("parents") != [{"id": self.folder}]):
                _fail("metadata_changed")
            values.append(m)
        return values

    def _read(self):
        before = self._metadata()
        raw, tag = self.io.read_versioned()
        if not strong_etag(tag) or tag != before[1]["etag"] or self._metadata() != before:
            _fail("read_unknown")
        value = _parse(raw)
        if (type(value) is not dict or set(value) != {"schema", "version", "catalog", "journal"}
                or value["schema"] != SCHEMA or type(value["version"]) is not int or value["version"] != 1
                or type(value["catalog"]) is not dict or set(value["catalog"]) != {"cases"}
                or type(value["catalog"]["cases"]) is not dict or not value["catalog"]["cases"]
                or len(value["catalog"]["cases"]) > 1000
                or digest(value["catalog"]) != self.catalog_digest):
            _fail("envelope_or_catalog_invalid")
        sources = []
        for case_id, entry in value["catalog"]["cases"].items():
            sources.append(_expected(case_id, entry)["source"]["source_id"])
        if len(set(sources)) != len(sources):
            _fail("catalog_source_duplicate")
        journal = validate_journal(value["journal"])
        if journal["schema"] != JOURNAL_SCHEMA:
            _fail("migration_forbidden")
        for record in journal["requests"].values():
            snapshot = record["snapshot"]
            anchor = value["catalog"]["cases"][snapshot["case_id"]]["anchor"]
            if snapshot["source"] != anchor["source"] or snapshot["context_digest"] != digest(anchor):
                _fail("journal_catalog_mismatch")
        return raw, tag, value

    def _load(self):
        try:
            raw, tag, value = self._read()
            if self._ack is not None and (raw, tag) != self._ack:
                self._ack = None  # Later GETs can reconcile; writes stay stopped.
                _fail("readback_unknown")
            self._ack, self._cached = None, (raw, tag, deepcopy(value))
            return raw, tag, value
        except Exception:
            self._stopped = True
            _fail("read_unknown")

    def read_versioned(self):
        with self._lock:
            _, tag, value = self._load()
            return deepcopy(value["journal"]), tag

    def read_catalog(self):
        with self._lock:
            _, tag, value = self._load()
            return deepcopy(value["catalog"]), tag

    def private_cases(self):
        catalog, tag = self.read_catalog()
        return {"cases": {key: deepcopy(e["anchor"]) for key, e in catalog["cases"].items()}}, tag

    def decoder_expected(self, case_id):
        catalog, _ = self.read_catalog()
        try:
            return _expected(case_id, catalog["cases"][case_id])
        except Exception:
            _fail("catalog_case_unavailable")

    def replace_versioned(self, value, tag):
        with self._lock:
            if self._stopped or self._ack is not None or self._cached is None:
                _fail("write_stopped")
            raw, old_tag, envelope = self._cached
            if not strong_etag(tag) or tag != old_tag:
                _fail("stale")
            try:
                value = deepcopy(validate_journal(value))
                if value["schema"] != JOURNAL_SCHEMA:
                    _fail("migration_forbidden")
                _transition(envelope["journal"], value)
                for record in value["requests"].values():
                    snapshot = record["snapshot"]
                    anchor = envelope["catalog"]["cases"][snapshot["case_id"]]["anchor"]
                    if snapshot["source"] != anchor["source"] or snapshot["context_digest"] != digest(anchor):
                        _fail("journal_catalog_mismatch")
                proposed = _bytes(dict(envelope, journal=value))
                if len(proposed) > MAX_BYTES:
                    _fail("size_limit")
            except Exception:
                _fail("update_invalid")
            # Stop BEFORE the single conditional write; GETs never clear a fault.
            self._stopped = True
            try:
                # Independent pre-write read: never merge into a newer envelope.
                fresh_raw, fresh_tag, _ = self._read()
                if (fresh_raw, fresh_tag) != (raw, tag):
                    _fail("stale")
                self.io.replace_versioned(raw, tag, proposed)
                actual, new_tag, actual_value = self._read()
                if actual != proposed or new_tag == tag:
                    _fail("readback_unknown")
                self._cached = (actual, new_tag, actual_value)
                self._ack = (actual, new_tag)  # Also check the core's save/load.
                self._stopped = False
            except Exception:
                _fail("write_unknown")
