"""Read-only payment units for Medical automation, using existing ledger links.

Item totals are never independently treated as payment totals. An item match may
be dismissed only with a complete, consistent receipt materialization. Unknown
units are relevant only when their own date/amount observations match the query.
"""
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
import re

from .receipt_reimport import _date, _money

POLICY = 'medical-payment-units-v1'
RECEIPT_COMMITTED = {'解析済', 'canonical_receipt', 'matched_receipt'}


@dataclass(frozen=True)
class PaymentMatch:
    classification: str  # candidate / unresolved / different
    reasons: tuple[str, ...]
    receipt_ids: tuple[str, ...]
    import_ids: tuple[str, ...]
    expense_ids: tuple[str, ...]
    posted: bool


@dataclass(frozen=True)
class PaymentUnit:
    receipts: tuple[tuple, ...]
    imports: tuple[tuple, ...]
    expenses: tuple[tuple, ...]
    issues: tuple[str, ...]
    verified_components: bool

    @property
    def posted(self):
        return any(r[12] == 'active' for r in self.expenses)


def _special_payment(row, kind):
    # Do not net refunds/transfers or invent installment/split-payment mappings.
    status = str(row[8] if kind == 'import_rows' else row[6]).lower()
    fields = (row[7], row[11]) if kind == 'import_rows' else (row[4], row[8])
    method = str(fields[0]).lower()
    mixed_method = (bool(re.search(r'現金|cash', method))
                    and bool(re.search(r'カード|card|pay|電子マネー|ポイント|商品券', method)))
    return (any(word in status for word in ('transfer', 'refund', 'cancel', 'statement', 'duplicate', 'review'))
            or mixed_method
            or bool(re.search(r'返金|返品|取消|払戻|振替|引落|分割|一部入金|部分払|併用|預り|内金', ' '.join(map(str, fields)))))


def payment_units(tables):
    """Join identifiers only; never join on a filename, date, merchant or category."""
    sizes = {'receipt_rows': 9, 'import_rows': 12, 'expense_rows': 13}
    nodes = []; index = defaultdict(list); source_imports = defaultdict(list)
    for kind, size in sizes.items():
        for raw in tables.get(kind, []):
            if not raw:
                continue
            row = tuple(list(raw) + [''] * max(0, size - len(raw)))
            n = len(nodes); nodes.append((kind, row)); index[kind, row[0]].append(n)
            if kind == 'import_rows' and row[2] == 'receipt':
                source_imports[row[3]].append(n)
    parents = list(range(len(nodes))); problems = defaultdict(set)

    def root(n):
        while parents[n] != n:
            parents[n] = parents[parents[n]]; n = parents[n]
        return n

    def join(a, b):
        parents[root(b)] = root(a)

    def link(n, targets):
        if not targets:
            problems[n].add('missing_link_target')
        for other in targets:
            join(n, other)

    for (kind, identity), duplicates in index.items():
        if not identity or len(duplicates) > 1:
            for n in duplicates:
                problems[n].add('missing_or_duplicate_identity')
                if identity:
                    join(duplicates[0], n)
    for n, (kind, row) in enumerate(nodes):
        if kind == 'expense_rows':
            for target_kind, identity in (('receipt_rows', row[9]), ('import_rows', row[10])):
                if identity:
                    link(n, index.get((target_kind, identity), []))
        elif kind == 'import_rows':
            if row[9]:
                targets = [other for k in sizes for other in index.get((k, row[9]), [])]
                link(n, targets)
                if len(targets) > 1 or n in targets:
                    problems[n].add('ambiguous_target_identity')
            # Existing receipt commit IDs may bind an import-only receipt. A
            # shared source file alone is never enough (it can contain pages).
            if row[2] == 'receipt' and row[3] and row[0] == 'receipt:' + str(row[3]):
                header = index.get(('receipt_rows', 'R-' + str(row[3])), [])
                if len(source_imports[row[3]]) == 1 and len(header) == 1:
                    join(n, header[0])
    groups = defaultdict(list)
    for n in range(len(nodes)):
        groups[root(n)].append(n)
    result = []
    for members in groups.values():
        rows = {kind: tuple(nodes[n][1] for n in members if nodes[n][0] == kind) for kind in sizes}
        receipts, imports, expenses = (rows[k] for k in sizes)
        issues = set().union(*(problems[n] for n in members))
        verified_components = False
        whole = [(r[1], _money(r[3])) for r in receipts] + [(r[4], _money(r[6])) for r in imports]
        if any(m is None or m <= 0 for _, m in whole):
            issues.add('invalid_payment_total')
        if len({m for _, m in whole}) > 1:
            issues.add('linked_payment_totals_disagree')
        if any(_special_payment(r, 'receipt_rows') for r in receipts) or any(_special_payment(r, 'import_rows') for r in imports):
            issues.add('payment_role_requires_review')
        receipt_imports = [r for r in imports if r[2] == 'receipt']
        if receipts or receipt_imports or any(r[8] == 'receipt' or r[9] for r in expenses):
            if len(receipts) != 1 or len(receipt_imports) != 1:
                issues.add('receipt_parent_not_unique')
            else:
                header, imported = receipts[0], receipt_imports[0]
                children = [r for r in expenses if r[8] == 'receipt' or r[9] == header[0]]
                if len(imports) - len(receipt_imports) > 1:
                    issues.add('multiple_settlement_records')
                if header[6] != '解析済' or imported[8] not in RECEIPT_COMMITTED or not imported[10]:
                    issues.add('receipt_commit_unverified')
                if (not _date(header[1]) or _date(header[1]) != _date(imported[4])
                        or any(_date(r[1]) != _date(header[1]) for r in children)):
                    issues.add('receipt_dates_disagree_or_unknown')
                if any(r[9] != header[0] or r[10] != imported[0] or r[8] != 'receipt' for r in children):
                    issues.add('receipt_item_links_disagree')
                active = [r for r in children if r[12] == 'active']
                amounts = [_money(r[4]) for r in active]
                # Import is the existing commit marker, written after every
                # stable sequential item. Sum alone does not prove completeness.
                expected = {f'{header[0]}-{i:02d}' for i in range(1, len(children) + 1)}
                if (not children or len(active) != len(children) or {r[0] for r in children} != expected
                        or any(m is None or m <= 0 for m in amounts)
                        or sum(m for m in amounts if m is not None) != _money(header[3])):
                    issues.add('receipt_items_incomplete_or_inconsistent')
                for r in expenses:
                    if r not in children and r[12] == 'active':
                        # A linked aggregate expense is only safe when it is
                        # exactly its own payment, never a second partial sum.
                        parent = [x for x in imports if x[0] == r[10]]
                        if len(parent) != 1 or _money(r[4]) != _money(parent[0][6]):
                            issues.add('linked_expense_role_unknown')
                        if active:
                            issues.add('multiple_active_materializations')
                verified_components = not issues
        elif imports:
            if len(imports) != 1:
                issues.add('multiple_payment_records_without_receipt')
            else:
                active = [r for r in expenses if r[12] == 'active']
                if active and (len(active) != 1 or active[0][10] != imports[0][0]
                               or _money(active[0][4]) != _money(imports[0][6])):
                    issues.add('payment_expense_role_unknown')
        else:
            # No existing schema evidence proves an orphan row is a whole
            # payment. Retain a matching row as unresolved, not as absent.
            issues.add('standalone_payment_role_unverified')
        result.append(PaymentUnit(receipts, imports, expenses, tuple(sorted(issues)), verified_components))
    return tuple(result)


def compare_payments(parsed, tables, *, days, unknown_dates):
    """Return relevant whole-payment matches and explained component exclusions.

    The 31-day admission check holds unknown dates; the existing 7-day writer
    check compares known dates. Neither window is widened or shortened here.
    """
    def matches(day, amount):
        if _money(amount) != parsed.total:
            return False
        day = _date(day)
        return unknown_dates if not day else abs((date.fromisoformat(day) - date.fromisoformat(parsed.date)).days) <= days

    results = []
    for unit in payment_units(tables):
        totals = [(r[1], r[3]) for r in unit.receipts] + [(r[4], r[6]) for r in unit.imports]
        components = [(r[1], r[4]) for r in unit.expenses if r[12] == 'active']
        whole_match = any(matches(d, a) for d, a in totals)
        item_match = any(matches(d, a) for d, a in components)
        if not whole_match and not item_match:
            continue
        if unit.issues:
            classification, reasons = 'unresolved', unit.issues
        elif whole_match:
            classification, reasons = 'candidate', ('payment_total_matches',)
        elif unit.verified_components:
            classification, reasons = 'different', ('verified_item_component_only',)
        else:
            classification, reasons = 'unresolved', ('payment_unit_unknown',)
        results.append(PaymentMatch(classification, reasons,
            tuple(r[0] for r in unit.receipts), tuple(r[0] for r in unit.imports),
            tuple(r[0] for r in unit.expenses), unit.posted))
    return tuple(results)
