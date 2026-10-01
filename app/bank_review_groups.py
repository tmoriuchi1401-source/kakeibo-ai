"""Exact bank meaning-review groups, independent of category rules.

Group keys describe conditions, never replace a transaction's import identity.
The membership proof additionally binds original values and current settlement
evidence, so refreshed groups cannot inherit an earlier approval silently.
"""
from collections import Counter, defaultdict
from copy import deepcopy
from datetime import date
import hashlib
import json
import re

from .bank_reconciliation import normalize_bank_description

CONDITION_FIELDS = (
    "bank", "account_alias", "direction", "normalized_description",
    "description", "transaction_kind", "counterparty", "amount_partition",
)
REVIEW_FIELDS = (
    "classification", "reason", "income_outcome", "income_reason", "rule_match", "bucket",
)
MEMBER_FIELDS = (
    "identity", "source_hash", "bank", "source", "account_alias", "direction",
    "date", "description", "signed_amount", "transaction_kind", "counterparty",
    "file_id", "pdf_sha256", "page", "row", "import_exists", "import_exact",
    "import_status", "income_exists", "saved_income_exact",
) + REVIEW_FIELDS


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value):
    return hashlib.sha256(encoded(value).encode("utf-8")).hexdigest()


def condition(group):
    return {key: group.get(key, "" if key != "amount_partition" else None)
            for key in CONDITION_FIELDS}


def group_key(group):
    return "bank-group:" + digest(condition(group))


def member_proof(row):
    return {key: row.get(key, "") for key in MEMBER_FIELDS}


def snapshot(group):
    return {"v": 1, "condition": condition(group),
            "review": {key: group[key] for key in REVIEW_FIELDS},
            "members": [member_proof(row) for row in group["members"]]}


def _validate(row):
    if row.get("direction") not in {"incoming", "outgoing"}:
        raise ValueError("bank_review_direction_invalid")
    amount = row.get("signed_amount")
    if type(amount) is not int or not amount or (amount > 0) != (row["direction"] == "incoming"):
        raise ValueError("bank_review_amount_invalid")
    if row.get("transaction_kind") != ("deposit" if amount > 0 else "withdrawal"):
        raise ValueError("bank_review_kind_invalid")
    try:
        if date.fromisoformat(row["date"]).isoformat() != row["date"]:
            raise ValueError
    except (TypeError, ValueError, KeyError):
        raise ValueError("bank_review_date_invalid") from None
    if not all(isinstance(row.get(k), str) and row[k].strip()
               for k in ("identity", "description", "bank", "account_alias")):
        raise ValueError("bank_review_identity_missing")
    if row.get("bucket") not in {"reward", "automatic", "review"}:
        raise ValueError("bank_review_bucket_invalid")
    if not all(k in row for k in REVIEW_FIELDS):
        raise ValueError("bank_review_evidence_missing")


def make_groups(rows):
    """Preserve raw text and counterpart; split automatic/wide funding amounts.

    Similar labels, different aliases, opposing directions and conflicting
    evidence never combine. An amount partition does not establish meaning.
    """
    bases = defaultdict(list)
    seen = set()
    for original in rows:
        row = deepcopy(original)
        _validate(row)
        if row["identity"] in seen:
            raise ValueError("duplicate_review_identity")
        seen.add(row["identity"])
        row["normalized_description"] = normalize_bank_description(row["description"])
        row.setdefault("counterparty", "")
        key = tuple(row[k] for k in CONDITION_FIELDS[:-1] + REVIEW_FIELDS)
        bases[key].append(row)
    groups = []
    for members in bases.values():
        amounts = [abs(row["signed_amount"]) for row in members]
        automatic = members[0]["bucket"] == "automatic"
        wide = (members[0]["reason"] == "ambiguous_incoming_transfer"
                and len(members) > 1 and max(amounts) >= 5 * min(amounts))
        partitions = defaultdict(list)
        for row in members:
            partitions[abs(row["signed_amount"]) if automatic or wide else None].append(row)
        for partition, items in partitions.items():
            group = {key: items[0][key] for key in CONDITION_FIELDS[:-1] + REVIEW_FIELDS}
            amounts = [abs(row["signed_amount"]) for row in items]
            group.update(amount_partition=partition, count=len(items),
                min_amount=min(amounts), max_amount=max(amounts), total_amount=sum(amounts),
                start_date=min(row["date"] for row in items),
                end_date=max(row["date"] for row in items),
                amount_pattern=dict(sorted(Counter(amounts).items())),
                members=sorted(items, key=lambda row: (row["date"], row["identity"])))
            group["key"] = group_key(group)
            group["snapshot"] = snapshot(group)
            group["membership_digest"] = digest(group["snapshot"])
            groups.append(group)
    rank = {"reward": 0, "automatic": 1, "review": 2}
    groups.sort(key=lambda group: (rank[group["bucket"]], group["bank"], group["account_alias"],
        group["normalized_description"], group["direction"], group["min_amount"],
        group["description"], group["reason"], group["counterparty"]))
    counters = Counter()
    for group in groups:
        prefix = {"reward": "R", "automatic": "A", "review": "Q"}[group["bucket"]]
        counters[prefix] += 1
        group["id"] = prefix + str(counters[prefix]).zfill(2)
    if len({group["key"] for group in groups}) != len(groups):
        # One condition carrying different classifications is a conflict, not
        # two independent approvals that could overwrite each other.
        raise ValueError("bank_review_condition_conflict")
    return groups


def validate_snapshot(value, key, membership_digest):
    """Validate hidden UI proof before trusting any selected answer."""
    try:
        proof = json.loads(value)
        if not isinstance(proof, dict) or proof.get("v") != 1:
            raise ValueError
        members = proof["members"]
        if not isinstance(members, list) or not members:
            raise ValueError
        regenerated = make_groups(members)
        # A partition produced from the full candidate set remains an exact
        # amount condition when validating just its captured members.
        partition = proof["condition"]["amount_partition"]
        if partition is not None:
            if type(partition) is not int or partition <= 0 or any(
                    abs(row["signed_amount"]) != partition for row in members):
                raise ValueError
            if len(regenerated) == 1:
                regenerated[0]["amount_partition"] = partition
                regenerated[0]["key"] = group_key(regenerated[0])
                regenerated[0]["snapshot"] = snapshot(regenerated[0])
        if (len(regenerated) != 1 or regenerated[0]["snapshot"] != proof
                or regenerated[0]["key"] != key or digest(proof) != membership_digest):
            raise ValueError
        return proof
    except (KeyError, TypeError, ValueError):
        raise ValueError("bank_review_snapshot_invalid") from None


def safe_alias(value):
    return isinstance(value, str) and bool(re.fullmatch(r"[a-z][a-z0-9_-]{1,63}", value))


def candidates_from_pdf(parsed, classifications, incomes, *, file_id, pdf_sha256,
                        import_rows, completed_postings, income_rows):
    """Use existing classifier and strict ledger proofs; never infer meaning.

    Callers supply decisions from the ordinary bank pipeline under the current
    production rules. Saved postings remove only eligible reclassification
    reviews. A bank_income import label alone never settles a deposit.
    """
    from .bank_archive_evidence import RECLASSIFICATION_REASONS, import_matches
    from .bank_income import income_id, validate_income_rows
    from .reconciliation import parse_import_rows
    if parsed.issues or parsed.balance_consistency_failures or not parsed.transactions:
        raise ValueError("bank_review_pdf_parse_unresolved")
    if not re.fullmatch(r"[0-9a-f]{64}", pdf_sha256):
        raise ValueError("bank_review_pdf_digest_invalid")
    classified = {item.transaction.source_row_identity: item for item in classifications}
    deposits = {item.transaction.source_row_identity: item for item in incomes}
    if len(classified) != len(classifications) or len(deposits) != len(incomes):
        raise ValueError("bank_review_classification_collision")
    if len({tx.source_row_identity for tx in parsed.transactions}) != len(parsed.transactions):
        raise ValueError("bank_review_identity_collision")
    if set(classified) != {tx.source_row_identity for tx in parsed.transactions}:
        raise ValueError("bank_review_classification_incomplete")
    existing_incomes = validate_income_rows(income_rows)
    by_id = defaultdict(list)
    for existing in parse_import_rows(import_rows):
        by_id[existing.import_id].append(existing)
    output = []
    for tx in parsed.transactions:
        identity = tx.source_row_identity
        classified_tx = classified[identity]
        income = deposits.get(identity)
        if tx.signed_amount > 0 and income is None:
            raise ValueError("bank_review_income_assessment_incomplete")
        if classified_tx.transaction != tx or income is not None and income.transaction != tx:
            raise ValueError("bank_review_original_decision_mismatch")
        completed = completed_postings.get(identity)
        review = classified_tx.classification == "needs_review" and not (
            classified_tx.reason in RECLASSIFICATION_REASONS and completed in {"income", "expense"})
        unknown_income = income is not None and income.outcome == "needs_review" and not (
            income.reason == "deposit_purpose_unconfirmed" and completed == "income")
        if not review and not unknown_income:
            continue
        imports = by_id.get(identity, [])
        exact = import_matches(tx, imports)
        if imports and not exact:
            raise ValueError("bank_review_existing_import_mismatch")
        bucket = "reward" if classified_tx.reason == "bank_reward" else (
            "automatic" if classified_tx.reason == "automatic_own_account_deposit" else "review")
        explicit = classified_tx.reason in {"operator_confirmed_non_own_review", "operator_confirmed_docomo_smtb_review"}
        output.append(dict(identity=identity, source_hash=tx.source_row_hash,
            bank=identity.split(":")[1], source=tx.source, account_alias=tx.account_alias,
            direction="incoming" if tx.signed_amount > 0 else "outgoing",
            date=tx.transaction_date, description=tx.description, signed_amount=tx.signed_amount,
            transaction_kind=tx.transaction_kind, classification=classified_tx.classification,
            reason=classified_tx.reason, income_outcome=income.outcome if income else "",
            income_reason=income.reason if income else "", rule_match="explicit_review_rule" if explicit else "no_exact_settling_rule",
            bucket=bucket, file_id=file_id, pdf_sha256=pdf_sha256, page=tx.source_page,
            row=tx.source_row, import_exists=bool(imports), import_exact=bool(exact),
            import_status=imports[0].status if exact else "",
            income_exists=income_id(identity) in existing_incomes,
            saved_income_exact=completed == "income"))
    return output
