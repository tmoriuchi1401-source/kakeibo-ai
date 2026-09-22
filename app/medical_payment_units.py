"""Read-only payment units for Medical automation, using existing ledger links.

Item totals are never independently treated as payment totals. An item match may
be dismissed only with a complete, consistent receipt materialization. Unknown
units are relevant only when their own date/amount observations match the query.
"""
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
import re
import unicodedata

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


def _verified_balance_transfer(unit):
    """An unlinked, explicitly imported balance top-up is not a purchase.

    Status alone is insufficient: require the source-specific contract, an
    unambiguous charge description, and no receipt/expense materialization.
    The subsequent purchase remains a separate unit and is still checked.
    """
    if (unit.receipts or unit.expenses or len(unit.imports) != 1
            or set(unit.issues) != {'payment_role_requires_review'}):
        return False
    row = unit.imports[0]
    if (row[8] != 'transfer_aupay_charge' or row[9] or not _date(row[4])
            or not re.fullmatch(r'[a-f0-9]{64}', str(row[10]))):
        return False
    merchant = unicodedata.normalize('NFKC', str(row[5]))
    merchant = re.sub(r'\s+', '', merchant).upper()
    if row[2] == 'au PAY':
        return (re.fullmatch(r'aupaycsv:[a-f0-9]{24}', str(row[0])) is not None
                and row[3] == row[0] and row[7] == 'au PAY'
                and merchant == 'オートチャージAUPAYカード'
                and str(row[11]).split(';', 1)[0] == 'CSV種別=オートチャージ')
    if row[2] == 'au PAYカード':
        return (re.fullmatch(r'aupaycard(?:-mail)?:[a-f0-9]{24}(?::[0-9]{3})?', str(row[0])) is not None
                and row[3] == row[0] and row[7] == '通常払い'
                and re.fullmatch(r'AUPAY残高(?:オート)?チャージ(?:\(不足額\))?', merchant) is not None)
    return False


def _verified_school_collection(unit, parsed):
    """A published municipal collection descriptor is not a medical payment.

    This is an exact public descriptor contract, not a merchant-name/category
    guess. See docs/receipt-local-payment-evidence.md for the primary sources.
    No transaction ID, amount, private provider or household date is allowed
    in the descriptor set. Existing bank authority and reciprocal ledger links
    must agree; a similarly named orphan/card/receipt stays unresolved.
    """
    if (not parsed.items or sum(i.amount for i in parsed.items) != parsed.total
            or any((i.major_category, i.minor_category) not in
                   {('医療・保険', '病院'), ('医療・保険', '薬')} for i in parsed.items)):
        return False
    if (unit.receipts or len(unit.imports) != 1 or len(unit.expenses) != 1
            or set(unit.issues) != {'invalid_payment_total', 'payment_expense_role_unknown'}):
        return False
    row, expense = unit.imports[0], unit.expenses[0]
    from .bank_pdf_pipeline import JIBUN_BANK_ADAPTER, DOCOMO_SMTB_BANK_ADAPTER, CHIBA_BANK_ADAPTER
    from .auto_expense import expense_id
    namespaces = {a.source: a.identity_namespace for a in
                  (JIBUN_BANK_ADAPTER, DOCOMO_SMTB_BANK_ADAPTER, CHIBA_BANK_ADAPTER)}
    namespace = namespaces.get(row[2])
    if not namespace: return False
    if (not re.fullmatch('bankpdf:' + re.escape(namespace) + r':[a-z0-9-]+:[a-f0-9]{24}', str(row[0]))
            or row[3] != row[0] or not re.fullmatch(r'[a-f0-9]{64}', str(row[10]))
            or row[0].rsplit(':', 1)[1] != row[10][:24]):
        return False
    if (row[8] != 'auto_expense' or row[9] != expense[0] or expense[0] != expense_id(row[0])
            or expense[10] != row[0] or expense[9] or expense[12] != 'active'
            or expense[8] != row[2] or expense[2] != row[5]
            or expense[5] == '医療・保険'
            or not _date(row[4]) or _date(row[4]) != _date(expense[1])
            or row[7] != '銀行口座' or expense[7] != row[7]
            or _money(row[6]) is None or _money(row[6]) >= 0
            or _money(expense[4]) != -_money(row[6])
            or expense[11] != 'bank_expense_authority'
            or '銀行最終判定=bank_expense_authority' not in [s.strip() for s in str(row[11]).split(';')]):
        return False
    description = ''.join(unicodedata.normalize('NFKC', str(row[5])).split())
    description = description.translate(str.maketrans({'ュ': 'ユ', 'ョ': 'ヨ', 'ッ': 'ツ'}))
    return description in {'チバシキユウシヨクヒトウ', 'チバシガツコウキユウシヨクヒトウ'}


def _verified_nonmedical_purchase(unit, parsed):
    """Published counterparties plus an intact source contract, never fuzzy names.

    These narrowly documented descriptors denote food or road use, rather than
    clinical treatment. Unknown merchants, edited links, medical categories,
    partial payments and pending review retain the ordinary duplicate hold.
    Public references are in docs/receipt-local-payment-evidence.md.
    """
    if (not parsed.items or sum(i.amount for i in parsed.items) != parsed.total
            or any((i.major_category, i.minor_category) not in
                   {('医療・保険', '病院'), ('医療・保険', '薬')} for i in parsed.items)):
        return ''
    if unit.issues or unit.receipts or len(unit.imports) != 1 or len(unit.expenses) > 1:
        return ''
    row = unit.imports[0]
    if (row[8] != 'auto_expense' or row[3] != row[0] or not _date(row[4])
            or not re.fullmatch(r'[a-f0-9]{64}', str(row[10]))):
        return ''
    if unit.expenses:
        from .auto_expense import expense_id
        expense = unit.expenses[0]
        if (row[9] != expense[0] or expense[0] != expense_id(row[0])
                or expense[10] != row[0] or expense[9] or expense[12] != 'active'
                or expense[8] != row[2] or expense[2] != row[5]
                or _date(expense[1]) != _date(row[4]) or _money(expense[4]) != _money(row[6])
                or expense[7] != row[7] or expense[5] == '医療・保険'
                or expense[11] != '明確な決済取引'):
            return ''
    elif row[9]:
        return ''
    merchant = ''.join(unicodedata.normalize('NFKC', str(row[5])).split()).upper().replace('−', '-')
    note_parts = [part.strip() for part in str(row[11]).split(';')]
    if row[2] == 'au PAY':
        if (not re.fullmatch(r'aupaycsv:[a-f0-9]{24}', str(row[0])) or row[7] != 'au PAY'
                or not note_parts or note_parts[0] != 'CSV種別=支払い'
                or len(note_parts) not in (2, 3)
                or not re.fullmatch('利用日時=' + re.escape(_date(row[4])) + r' \d{2}:\d{2}', note_parts[1])
                or (len(note_parts) == 3 and note_parts[2] != '自動判定=明確な決済取引')):
            return ''
        if merchant == 'LINK-CAFE新木場2':
            return 'verified_public_cafe_not_medical_payment'
    if row[2] == 'au PAYカード':
        match = re.fullmatch(r'aupaycard-mail:[a-f0-9]{24}:([0-9]{3})', str(row[0]))
        if (not match or row[7] != '通常払い' or not note_parts
                or note_parts[0] != 'メール明細No.' + match[1]
                or note_parts[1:] not in ([], ['自動判定=明確な決済取引'])):
            return ''
        # This public operator example is deliberately exact. Do not infer
        # road use for arbitrary names containing 入/出, ETC, or a hyphen.
        if merchant == '守口入-本町出':
            return 'verified_public_toll_route_not_medical_payment'
    return ''


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


def _verified_aliases(receipts,imports,expenses):
    """Existing matched_receipt headers describe a payment, not new items.

    Admit only committed, exact-date/amount aliases to one active expense,
    with no expense of their own. Broken or partial links stay in the ordinary
    validation path and therefore remain unresolved.
    """
    aliases=[]
    for imported in imports:
        if imported[2]!='receipt' or imported[8]!='matched_receipt' or not imported[10]:continue
        sid=imported[3]
        if not sid or imported[0]!='receipt:'+sid:continue
        headers=[r for r in receipts if r[0]=='R-'+sid]
        targets=[r for r in expenses if r[0]==imported[9] and r[12]=='active']
        if len(headers)!=1 or len(targets)!=1:continue
        header,target=headers[0],targets[0]
        if any(r[9]==header[0] or r[10]==imported[0] for r in expenses):continue
        if (header[6]!='解析済' or not _date(header[1])
                or not _date(header[1])==_date(imported[4])==_date(target[1])
                or _money(header[3]) is None or _money(header[3])<=0
                or not _money(header[3])==_money(imported[6])==_money(target[4])):continue
        aliases.append((header[0],imported[0]))
    return aliases


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
        aliases=_verified_aliases(receipts,imports,expenses)
        alias_headers={r for r,_ in aliases};alias_imports={i for _,i in aliases}
        core_receipts=tuple(r for r in receipts if r[0] not in alias_headers)
        core_imports=tuple(r for r in imports if r[0] not in alias_imports)
        receipt_imports = [r for r in core_imports if r[2] == 'receipt']
        if core_receipts or receipt_imports or any(r[8] == 'receipt' or r[9] for r in expenses):
            if len(core_receipts) != 1 or len(receipt_imports) != 1:
                issues.add('receipt_parent_not_unique')
            else:
                header, imported = core_receipts[0], receipt_imports[0]
                children = [r for r in expenses if r[8] == 'receipt' or r[9] == header[0]]
                if len(core_imports) - len(receipt_imports) > 1:
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
        elif core_imports:
            if len(core_imports) != 1:
                issues.add('multiple_payment_records_without_receipt')
            else:
                active = [r for r in expenses if r[12] == 'active']
                if active and (len(active) != 1 or active[0][10] != core_imports[0][0]
                               or _money(active[0][4]) != _money(core_imports[0][6])):
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
        if _verified_balance_transfer(unit):
            classification, reasons = 'different', ('verified_balance_transfer_not_purchase',)
        elif _verified_school_collection(unit, parsed):
            classification, reasons = 'different', ('verified_school_collection_not_medical_payment',)
        elif purpose := _verified_nonmedical_purchase(unit, parsed):
            classification, reasons = 'different', (purpose,)
        elif unit.issues:
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
