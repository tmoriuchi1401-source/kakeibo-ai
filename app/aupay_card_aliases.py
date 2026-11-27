"""Card alias evidence carried by existing append-only recurring summaries.

No fingerprint grants duplicate authority. Only a saved source hash and an
independent exact owner-row read do; indistinguishable new sources stop coverage.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import re

from .transaction_plan import normalize_card_transaction


class LiteralRows:
    """Use existing literal reads without changing the underlying API client."""
    def __init__(self,db): object.__setattr__(self,"db",db)
    def __getattr__(self,name): return getattr(self.db,name)
    def __setattr__(self,name,value): setattr(self.db,name,value)
    def get(self,rng): return self.db.get_raw(rng)


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=True, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


def import_row(row):
    if len(row) > 12:
        raise RuntimeError("card_alias_owner_shape")
    return list(row) + [""] * (12 - len(row))


def _signature(row):
    row = import_row(row)
    # Sheets' existing reader returns formatted strings; compare the amount
    # under the same integer-yen contract used by the canonical identity reader.
    row[6] = int(str(row[6]).replace(",", ""))
    return digest([row[i] for i in (2, 4, 5, 6, 7, 11)])


def _tx_signature(tx):
    return digest([tx.source, tx.transaction_date, tx.merchant, tx.amount_yen,
                   tx.payment_method, tx.memo])


def _owner_changes(entry):
    """Only an exact stable-expense J link may succeed an immutable owner."""
    from .auto_expense import expense_id
    changes = entry.get("owner_changes", [])
    if not isinstance(changes,list): raise RuntimeError("card_alias_owner_change_invalid")
    previous = None
    for change in changes:
        if set(change) != {"before", "after", "expense", "plan_sha256"}:
            raise RuntimeError("card_alias_owner_change_invalid")
        before, after, expense = change["before"], change["after"], change["expense"]
        if (len(before)!=12 or len(after)!=12 or len(expense)!=13 or before[0]!=entry["owner"]
                or before[2]!="au PAYカード" or before[8]!="auto_expense" or before[9]
                or after[:9]!=before[:9] or after[10:]!=before[10:]
                or after[9]!=expense_id(entry["owner"]) or expense[0]!=after[9]
                or expense[1:3]!=[before[4],before[5]] or expense[4]!=before[6]
                or expense[7:9]!=[before[7],before[2]] or expense[10]!=entry["owner"] or expense[12]!="active"
                or not re.fullmatch(r"[0-9a-f]{64}",change["plan_sha256"])
                or previous is not None and previous!=before):
            raise RuntimeError("card_alias_owner_change_invalid")
        previous=after
    if changes and digest(previous)!=entry["owner_row_sha256"]:
        raise RuntimeError("card_alias_owner_change_invalid")
    return changes


def validate_owner_expenses(registry, expense_rows):
    by_id={}
    for row in expense_rows:
        if row: by_id.setdefault(row[0],[]).append(list(row)+[""]*max(0,13-len(row)))
    for entry in merge_registry(registry):
        for change in _owner_changes(entry):
            actual=by_id.get(change["expense"][0],[])
            # Categories and user notes remain editable in the existing UI.
            # Alias authority depends on economic ownership, not those labels.
            fields=(0,1,2,4,7,8,10,12)
            if len(actual)!=1 or any(actual[0][i]!=change["expense"][i] for i in fields):
                raise RuntimeError("card_alias_expense_readback_mismatch")


def merge_registry(entries):
    """Union append-only deltas; reject reassignment or damaged saved evidence."""
    owners, sources = {}, {}
    for entry in entries:
        if (not isinstance(entry, dict) or set(entry)-{"owner_changes"} != {
                "owner", "owner_row_sha256", "business_fingerprint", "aliases"}
                or not re.fullmatch(r"aupaycard-mail:[0-9a-f]{24}:\d{3}", entry["owner"])
                or not re.fullmatch(r"[0-9a-f]{64}", entry["owner_row_sha256"])
                or not re.fullmatch(r"[0-9a-f]{24}", entry["business_fingerprint"])
                or not isinstance(entry["aliases"], dict) or entry["owner"] not in entry["aliases"]):
            raise RuntimeError("card_alias_registry_invalid")
        changes = _owner_changes(entry)
        old = owners.setdefault(entry["owner"], deepcopy(entry))
        if old["business_fingerprint"] != entry["business_fingerprint"]:
            raise RuntimeError("card_alias_owner_changed")
        if old["owner_row_sha256"] != entry["owner_row_sha256"]:
            prior=old.get("owner_changes",[])
            if (len(changes)<=len(prior) or changes[:len(prior)]!=prior
                    or digest(changes[len(prior)]["before"])!=old["owner_row_sha256"]):
                raise RuntimeError("card_alias_owner_changed")
            old["owner_row_sha256"]=entry["owner_row_sha256"]
            old["owner_changes"]=deepcopy(changes)
        for identity, source_hash in entry["aliases"].items():
            if (not re.fullmatch(r"aupaycard-mail:[0-9a-f]{24}:\d{3}", identity)
                    or not re.fullmatch(r"[0-9a-f]{64}", source_hash)):
                raise RuntimeError("card_alias_registry_invalid")
            if sources.setdefault(identity, (entry["owner"], source_hash)) != (entry["owner"], source_hash):
                raise RuntimeError("card_alias_reassigned")
            old["aliases"][identity] = source_hash
    return tuple(owners[k] for k in sorted(owners))


def resolve_aliases(raw_rows, existing_rows, registry):
    registry = merge_registry(registry)
    physical = {}
    for row in existing_rows:
        if row:
            physical.setdefault(str(row[0]).strip(), []).append(import_row(row))
    known, fingerprints = {}, set()
    for entry in registry:
        owner = entry["owner"]
        rows = physical.get(owner, [])
        if len(rows) != 1 or digest(rows[0]) != entry["owner_row_sha256"]:
            raise RuntimeError("card_alias_owner_readback_mismatch")
        if rows[0][3] != owner or rows[0][10] != entry["aliases"][owner]:
            raise RuntimeError("card_alias_owner_content_mismatch")
        for identity, source_hash in entry["aliases"].items():
            if identity != owner and physical.get(identity):
                raise RuntimeError("card_alias_multiple_physical_owners")
            known[identity] = (owner, source_hash, entry["business_fingerprint"])
        fingerprints.add(entry["business_fingerprint"])
    legacy_signatures = {_signature(row) for rows in physical.values() for row in rows
                         if row[2] == "au PAYカード" and str(row[0]).startswith("aupaycard-mail:")}
    result = {}
    normalized = [normalize_card_transaction(raw) for raw in raw_rows]
    present_sources = {tx.identity: tx for tx in normalized if tx.identity in physical}
    for tx in normalized:
        if tx.identity in known:
            owner, source_hash, fingerprint = known[tx.identity]
            if tx.source_hash != source_hash or tx.business_fingerprint != fingerprint:
                raise RuntimeError("card_alias_source_changed")
            result[tx.identity] = owner
        elif tx.identity not in physical and (tx.business_fingerprint in fingerprints
                                              or _tx_signature(tx) in legacy_signatures):
            # Existing legacy owners can acquire the planner's within-window
            # aliases only while that exact original is also being read.
            legacy_pair = any(
                old.business_fingerprint == tx.business_fingerprint
                and old.identity not in known
                and old.identity.rsplit(":", 1)[-1] == tx.identity.rsplit(":", 1)[-1]
                and old.identity != tx.identity for old in present_sources.values())
            if legacy_pair:
                continue
            # Could be a late resend OR another legitimate indistinguishable
            # purchase. Neither dropping it nor appending it is justified.
            raise RuntimeError("card_alias_unresolved_no_checkpoint")
    return result


def confirmed_alias_updates(raw_rows, reconciliation, existing_rows, registry, owner_ids, *, expected_rows=()):
    """Call only after acknowledged writes and independent exact readback."""
    registry = merge_registry(registry)
    resolve_aliases([], existing_rows, registry)
    known = {identity: e["owner"] for e in registry for identity in e["aliases"]}
    raw_by_id = {tx.identity: tx for tx in map(normalize_card_transaction, raw_rows)}
    rows = {}
    for row in existing_rows:
        if row:
            rows.setdefault(str(row[0]).strip(), []).append(import_row(row))
    for expected in expected_rows:
        actual = rows.get(expected[0], [])
        if len(actual) != 1 or tuple(map(str, actual[0])) != tuple(map(str, import_row(expected))):
            raise RuntimeError("card_alias_exact_append_readback_mismatch")
    updates = []
    for item in reconciliation["transactions"]:
        owners = {known[i] for i in item.source_identities if i in known}
        owners.update(i for i in item.source_identities if i in rows)
        if not owners.intersection(owner_ids):
            continue
        if len(owners) != 1:
            raise RuntimeError("card_alias_multiple_physical_owners")
        owner = owners.pop()
        if len(rows[owner]) != 1:
            raise RuntimeError("card_alias_owner_readback_mismatch")
        row = rows[owner][0]
        tx = raw_by_id.get(owner)
        if tx is not None and (row[3] != owner or row[10] != tx.source_hash
                               or _signature(row) != _tx_signature(tx)):
            raise RuntimeError("card_alias_owner_content_mismatch")
        if owner not in raw_by_id and not any(e["owner"] == owner for e in registry):
            raise RuntimeError("card_alias_owner_source_missing")
        updates.append(dict(owner=owner, owner_row_sha256=digest(row),
            business_fingerprint=item.canonical.business_fingerprint,
            aliases={i: raw_by_id[i].source_hash for i in item.source_identities}))
        if owner not in updates[-1]["aliases"]:
            prior = next(e for e in registry if e["owner"] == owner)
            updates[-1]["aliases"][owner] = prior["aliases"][owner]
    merge_registry((*registry, *updates))
    if {e["owner"] for e in updates} != set(owner_ids):
        raise RuntimeError("card_alias_owner_readback_mismatch")
    return updates
