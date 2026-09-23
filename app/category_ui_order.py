"""Consolidate equivalent proposals without merging independent approvals."""
from copy import deepcopy
import json

from .category_rule_choices import DECLINE, checked


def proof(value):
    try:
        result = json.loads(value)
        return result if isinstance(result, dict) else {}
    except (TypeError, ValueError):
        return {}


def source_snapshot(value):
    data = proof(value)
    if not data:
        return value
    data.pop("ui_members", None)
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def member_keys(row):
    members = proof(row[11]).get("ui_members", [])
    return {str(row[6]), *(x for x in members if isinstance(x, str))}


def completed(row):
    state = str(row[1]).split("\n")[-1]
    return row[4] == DECLINE or state in {"登録済み", "既存ルールあり・追加登録しない"}


def consolidate_blocks(blocks):
    result = deepcopy(blocks)
    rules = consolidate_rules(result["rule"][1], result["backfill"][1],
                              [r[6] for r in result["rule"][1]])
    aliases = {alias: str(row[6]) for row in rules for alias in member_keys(row)}
    past = []
    seen = set()
    for row in result["backfill"][1]:
        if str(row[6]).startswith("displayed:"):
            original = str(row[6]).removeprefix("displayed:")
            row[6] = "displayed:" + aliases.get(original, original)
            signature = (row[6], *(str(v) for v in row[2:5]), str(row[8]), str(row[9]))
            if signature in seen:
                continue
            seen.add(signature)
        past.append(row)
    result["rule"] = (result["rule"][0], rules)
    result["backfill"] = (result["backfill"][0], past)
    return result


def consolidate_rules(rows, backfill=(), preferred=()):
    """Keep the chosen representative's source proof and all compatible checks.

    Different future decisions, held approvals or historical periods are not
    duplicates. Member IDs carry category edits across subsequent refreshes;
    they are display metadata, never additional authorization to register.
    """
    historical = {str(r[6]).removeprefix("displayed:"): tuple(str(v) for v in r[2:5]) + (str(r[8]), str(r[9]))
                  for r in backfill if len(r) >= 10 and str(r[6]).startswith("displayed:")}
    groups = {}
    for original in rows:
        row = deepcopy(original)
        data = proof(row[11])
        state = str(row[1]).split("\n")[-1]
        identity = tuple(str(data.get(k, "")) for k in
                         ("kind", "source", "account_alias", "merchant", "product_id", "amount"))
        # Malformed or held proposals remain individually reviewable.
        guard = row[6] if not data or state.startswith(("held:", "再承認", "競合")) else ""
        key = identity + tuple(row[2:4]) + (data.get("proposal") == "fallback_group", guard)
        groups.setdefault(key, []).append(row)
    output = []
    preferred = set(preferred)
    explicit = {str(row[6]) for row in rows}
    for members in groups.values():
        partitions = []
        # Decisions first: neutral repeated transactions join one compatible
        # decision, never bridge two different decisions or historical periods.
        members.sort(key=lambda r: (not (checked(r[4]) or r[4] == DECLINE or checked(r[5])),
                                    str(r[6]) not in preferred, str(r[6])))
        for row in members:
            decision = "register" if checked(row[4]) else "decline" if row[4] == DECLINE else ""
            period = historical.get(str(row[6])) if checked(row[5]) else None
            target = next((p for p in partitions if
                           (not decision or not p[1] or decision == p[1]) and
                           (period is None or p[2] is None or period == p[2])), None)
            if target is None:
                partitions.append([[row], decision, period])
            else:
                target[0].append(row)
                target[1] = target[1] or decision
                target[2] = target[2] if target[2] is not None else period
        for partition, decision, _ in partitions:
            row = partition[0]
            row[4] = DECLINE if decision == "decline" else decision == "register"
            row[5] = any(checked(r[5]) for r in partition)
            current = {str(r[6]) for r in partition}
            aliases = set().union(*(member_keys(r) for r in partition)) - (explicit - current)
            data = proof(row[11])
            if data and len(aliases) > 1:
                data["ui_members"] = sorted(aliases)
                row[11] = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            elif data:
                row[11] = source_snapshot(row[11])
            lines = [line for line in str(row[1]).split("\n") if not line.startswith("同条件・同カテゴリ ")]
            if len(aliases) > 1:
                lines.insert(max(0, len(lines)-1), f"同条件・同カテゴリ {len(aliases)}件を統合")
            row[1] = "\n".join(lines)
            output.append(row)
    # Stable within each group; registered/declined rows stay visible at end.
    return sorted(output, key=lambda r: (completed(r), str(r[9]), str(r[0]), str(r[6])))
