"""Deterministic, write-free transaction planning primitives.

The module deliberately knows nothing about Gmail.  Adapters provide parsed
records with a stable source identity; this keeps provider-specific parsing
separate from KakeiboAI materialization.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import re
import unicodedata

from .aupay_card_contract import (
    aupay_card_source_hash,
    normalize_card_payment_method,
)


def _text(value: object) -> str:
    return unicodedata.normalize("NFKC", str(value or "")).strip()


def _merchant(value: object) -> str:
    return re.sub(r"\s+", " ", _text(value))


@dataclass(frozen=True)
class Transaction:
    schema_version: int
    source: str
    source_record_id: str
    transaction_date: str
    merchant: str
    amount_yen: int
    transaction_kind: str
    payment_method: str
    identity: str
    business_fingerprint: str
    memo: str = ""
    member: str = ""
    source_occurrence: int = 1
    source_hash: str = ""


@dataclass(frozen=True)
class PlanItem:
    transaction: Transaction
    action: str
    reason: str


@dataclass(frozen=True)
class CrossSourceEvidence:
    state: str
    candidate_identities: tuple[str, ...] = ()


@dataclass(frozen=True)
class ReconciledTransaction:
    canonical: Transaction
    state: str
    source_identities: tuple[str, ...]
    existing_source_identities: tuple[str, ...]
    cross_source: CrossSourceEvidence


def normalize_card_transaction(raw: dict) -> Transaction:
    """Normalize a parsed card row; missing core data is rejected."""
    date = _text(raw.get("date"))
    merchant = _merchant(raw.get("merchant"))
    source_id = _text(raw.get("import_id"))
    try:
        amount = int(raw.get("amount"))
    except (TypeError, ValueError) as exc:
        raise ValueError("amount_invalid") from exc
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
        raise ValueError("date_invalid")
    if not merchant or not source_id or amount == 0:
        raise ValueError("required_field_missing")
    kind = _text(raw.get("transaction_kind")) or "purchase"
    if kind not in {"purchase", "return"}:
        raise ValueError("transaction_kind_invalid")
    if (kind == "purchase" and amount < 0) or (kind == "return" and amount > 0):
        raise ValueError("transaction_kind_amount_mismatch")
    payment = normalize_card_payment_method(raw.get("payment_type"))
    memo = _text(raw.get("memo"))
    member = _text(raw.get("member"))
    try:
        source_occurrence = int(raw.get("source_occurrence", raw.get("occurrence", 1)))
    except (TypeError, ValueError) as exc:
        raise ValueError("source_occurrence_invalid") from exc
    source_hash = _text(raw.get("source_hash")) or aupay_card_source_hash(raw)
    if not re.fullmatch(r"[0-9a-f]{64}", source_hash):
        raise ValueError("source_hash_invalid")
    # The business fingerprint is evidence/audit metadata, not a duplicate
    # key: same-day same-amount purchases must remain separate transactions.
    business = "|".join((
        date, merchant, str(amount), kind, payment, member, memo,
    ))
    fingerprint = hashlib.sha256(business.encode("utf-8")).hexdigest()[:24]
    return Transaction(3, "au PAYカード", source_id, date, merchant, amount,
                       kind, payment, source_id, fingerprint, memo, member,
                       source_occurrence, source_hash)


def normalized_merchant(value: object) -> str:
    """Normalize harmless card/CSV merchant formatting differences."""
    return re.sub(r"[^0-9A-Z\u3040-\u30ff\u3400-\u9fff]+", "", _merchant(value).upper())


def _mail_source_parts(identity: str) -> tuple[str, int] | None:
    match = re.fullmatch(r"aupaycard-mail:([0-9a-f]{24}):(\d{3})", identity)
    return (match.group(1), int(match.group(2))) if match else None


def _cross_source_evidence(tx: Transaction, existing_rows: list[list]) -> CrossSourceEvidence:
    date_amount_candidates = []
    merchant_candidates = []
    for row in existing_rows:
        padded = list(row) + [""] * max(0, 8 - len(row))
        identity, source = _text(padded[0]), _text(padded[2])
        if source != "au PAYカード" or not identity.startswith("aupaycard:"):
            continue
        try:
            amount = int(float(str(padded[6]).replace(",", "")))
        except (TypeError, ValueError):
            continue
        if _text(padded[4]) == tx.transaction_date and amount == tx.amount_yen:
            date_amount_candidates.append(identity)
            if normalized_merchant(padded[5]) == normalized_merchant(tx.merchant):
                merchant_candidates.append(identity)
    unique = tuple(sorted(set(merchant_candidates)))
    if len(unique) == 1:
        return CrossSourceEvidence("cross_source_strong_match", unique)
    ambiguous = tuple(sorted(set(date_amount_candidates)))
    if ambiguous:
        return CrossSourceEvidence("cross_source_ambiguous", ambiguous)
    return CrossSourceEvidence("cross_source_no_match")


def reconcile_transactions(raw_rows: list[dict], existing_rows: list[list] | None = None) -> dict:
    """Project source records into deterministic, evidence-preserving candidates."""
    existing_rows = existing_rows or []
    existing_identities = {
        _text(row[0]) for row in existing_rows if row and _text(row[0])
    }
    rejected = 0
    by_identity: dict[str, list[Transaction]] = {}
    for raw in raw_rows:
        try:
            tx = normalize_card_transaction(raw)
        except ValueError:
            rejected += 1
            continue
        by_identity.setdefault(tx.identity, []).append(tx)

    collisions = 0
    same_source_duplicates = 0
    unique: list[Transaction] = []
    review: list[Transaction] = []
    for rows in by_identity.values():
        variants = {(x.transaction_date, x.merchant, x.amount_yen, x.transaction_kind,
                     x.payment_method, x.business_fingerprint, x.source_hash)
                    for x in rows}
        if len(variants) > 1:
            collisions += 1
            review.extend(rows)
            continue
        unique.append(rows[0])
        same_source_duplicates += len(rows) - 1

    by_fingerprint: dict[str, list[Transaction]] = {}
    for tx in unique:
        by_fingerprint.setdefault(tx.business_fingerprint, []).append(tx)

    reconciled: list[ReconciledTransaction] = []
    probable_resend_groups = 0
    probable_resend_records = 0
    for rows in by_fingerprint.values():
        # All fingerprint components, including the mail detail number, are
        # identical.  Different source identities therefore remain evidence
        # but project to one candidate.  Identity is only the final stable
        # tie-break because the business content is otherwise indistinguishable.
        identities = tuple(sorted(x.identity for x in rows))
        parts = [_mail_source_parts(identity) for identity in identities]
        probable = (
            len(identities) > 1
            and all(part is not None for part in parts)
            and len({part[0] for part in parts if part}) == len(parts)
            and len({part[1] for part in parts if part}) == 1
        )
        # A fingerprint collision without the expected Gmail source structure
        # is not granted resend authority; preserve every row for review.
        groups = [rows] if probable else [[row] for row in rows]
        if not probable and len(rows) > 1:
            review.extend(rows)
        for projected_rows in groups:
            projected_identities = tuple(sorted(x.identity for x in projected_rows))
            canonical = min(projected_rows, key=lambda x: (
                x.transaction_date, x.amount_yen, x.transaction_kind,
                normalized_merchant(x.merchant),
                x.payment_method, x.memo, x.identity,
            ))
            reconciled.append(ReconciledTransaction(
                canonical=canonical,
                state="probable_resend" if probable else ("needs_review" if len(rows) > 1 else "new"),
                source_identities=projected_identities,
                existing_source_identities=tuple(
                    identity for identity in projected_identities
                    if identity in existing_identities
                ),
                cross_source=_cross_source_evidence(canonical, existing_rows),
            ))
        if probable:
            probable_resend_groups += 1
            probable_resend_records += len(identities) - 1

    reconciled.sort(key=lambda x: (
        x.canonical.transaction_date, x.canonical.amount_yen,
        normalized_merchant(x.canonical.merchant), x.canonical.identity,
    ))
    cross_counts = {name: 0 for name in (
        "cross_source_strong_match", "cross_source_ambiguous", "cross_source_no_match",
    )}
    for item in reconciled:
        cross_counts[item.cross_source.state] += 1
    return {
        "schema_version": 1,
        "transactions": reconciled,
        "review_transactions": tuple(review),
        "summary": {
            "raw_transactions": len(raw_rows),
            "canonical_transactions": len(reconciled),
            "same_source_duplicates": same_source_duplicates,
            "probable_resend_groups": probable_resend_groups,
            "probable_resend_records": probable_resend_records,
            "needs_review": len(review),
            "rejected": rejected,
            "identity_collisions": collisions,
            "purchase_canonical_transactions": sum(
                item.canonical.transaction_kind == "purchase" for item in reconciled
            ),
            "return_canonical_transactions": sum(
                item.canonical.transaction_kind == "return" for item in reconciled
            ),
            "existing_identity_duplicates": sum(
                bool(item.existing_source_identities) for item in reconciled
            ),
            **cross_counts,
        },
    }


def build_write_plan(raw_rows: list[dict], existing_rows: list[list] | None = None) -> dict:
    """Build a deterministic plan without mutating any storage."""
    existing_rows = existing_rows or []
    existing = {}
    for row in existing_rows:
        if row and str(row[0]).strip():
            existing.setdefault(str(row[0]).strip(), row)
    items: list[PlanItem] = []
    seen: dict[str, Transaction] = {}
    collisions = 0
    rejected = 0
    reconciliation = reconcile_transactions(raw_rows, existing_rows)
    canonical_ids = {x.canonical.identity for x in reconciliation["transactions"]}
    noncanonical_ids = {
        identity for x in reconciliation["transactions"]
        for identity in x.source_identities if identity != x.canonical.identity
    }
    for raw in raw_rows:
        try:
            tx = normalize_card_transaction(raw)
        except ValueError:
            rejected += 1
            continue
        prior = seen.get(tx.identity)
        if prior and prior != tx:
            collisions += 1
            items.append(PlanItem(tx, "reject", "identity_collision"))
        elif tx.identity in noncanonical_ids:
            items.append(PlanItem(tx, "probable_resend", "business_fingerprint_match"))
        elif tx.identity in existing or prior:
            items.append(PlanItem(tx, "duplicate", "existing_identity" if tx.identity in existing else "input_identity"))
        elif tx.transaction_kind == "return":
            items.append(PlanItem(tx, "reject", "return_requires_dedicated_apply_semantics"))
        elif tx.identity in canonical_ids:
            items.append(PlanItem(tx, "insert", "new_identity"))
        else:
            items.append(PlanItem(tx, "reject", "reconciliation_review"))
        seen[tx.identity] = tx
    counts = {"insert": 0, "duplicate": 0, "probable_resend": 0,
              "needs_review": 0, "rejected": rejected}
    for item in items:
        counts["needs_review" if item.action == "reject" else item.action] += 1
    return {
        "schema_version": 1,
        "items": items,
        "summary": counts,
        "unique_transactions": sum(item.action == "insert" for item in items),
        "identity_collisions": collisions,
        "reconciliation": reconciliation,
        "same_day_same_amount_groups": same_day_same_amount_groups([i.transaction for i in items]),
    }


def same_day_same_amount_groups(transactions: list[Transaction]) -> list[dict]:
    groups: dict[tuple[str, int], list[Transaction]] = {}
    for tx in transactions:
        groups.setdefault((tx.transaction_date, tx.amount_yen), []).append(tx)
    return [{"date": date, "amount_yen": amount,
             "count": len(rows), "identities": sorted(tx.identity for tx in rows),
             "distinct": len({tx.identity for tx in rows}) == len(rows)}
            for (date, amount), rows in sorted(groups.items()) if len(rows) > 1]
