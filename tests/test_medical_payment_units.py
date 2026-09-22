"""Synthetic payment-level matching; no live IDs, documents or credentials."""
from copy import deepcopy
from datetime import date, timedelta
import pytest

from app.medical_payment_units import compare_payments
from app.medical_auto_posting import possible_duplicate
from app.models import ReceiptResult, ReceiptItem
from test_receipt_confirmation import medical

DAY = '2026-09-01'


def receipt(parts=(100, 200), *, identity='synthetic-retail', day=DAY):
    rid, iid = 'R-' + identity, 'receipt:' + identity
    total = sum(parts)
    return {'receipt_rows': [[rid, day, 'Synthetic retailer', total, 'cash', '', '解析済', '', '']],
        'import_rows': [[iid, 'synthetic-import-time', 'receipt', identity, day, 'Synthetic retailer', total,
                         'cash', '解析済', '', 'a' * 64, '']],
        'expense_rows': [[f'{rid}-{n:02d}', day, 'Synthetic retailer', f'Unspecified product {n}', amount,
                          'Owner major', 'Owner minor', 'cash', 'receipt', rid, iid, 'Owner note', 'active']
                         for n, amount in enumerate(parts, 1)], 'review_rows': []}


def query(amount=100, day=DAY):
    return ReceiptResult(date=day, merchant='Synthetic clinic', total=amount, payment_method='',
        items=[ReceiptItem(name='Synthetic treatment', amount=amount, major_category='医療・保険', minor_category='病院')])


def comparison(tables, amount=100, *, days=31):
    return compare_payments(query(amount), tables, days=days, unknown_dates=days == 31)


def load(db, tables):
    for key, title in [('receipt_rows', 'レシート'), ('import_rows', '取込データ'), ('expense_rows', '支出明細')]:
        db.rows[title] = deepcopy(tables[key])


@pytest.mark.parametrize('days', [7, 31])
def test_verified_component_equal_to_medical_is_not_a_payment_match(days):
    tables = receipt(); before = deepcopy(tables)
    matches = comparison(tables, days=days)
    assert len(matches) == 1 and matches[0].classification == 'different'
    assert matches[0].reasons == ('verified_item_component_only',)
    assert not possible_duplicate(query(), tables) and tables == before


@pytest.mark.parametrize('parts', [(40, 60), (100,)])
@pytest.mark.parametrize('days', [7, 31])
def test_total_match_is_detected_with_or_without_an_equal_item(parts, days):
    matches = comparison(receipt(parts), days=days)
    assert len(matches) == 1 and matches[0].classification == 'candidate' and matches[0].posted


def test_explicit_receipt_payment_and_expenses_are_one_candidate():
    tables = receipt((40, 60)); iid = tables['import_rows'][0][0]
    tables['import_rows'].append(['payment-1', '', 'au PAYカード', 'synthetic-notice', DAY, 'Synthetic processor',
                                  100, 'card', 'matched_receipt', iid, 'b' * 64, ''])
    tables['expense_rows'].append(['former-payment', DAY, 'Synthetic processor', 'Aggregate', 100,
                                   '', '', 'card', 'au PAYカード', '', 'payment-1', '', 'duplicate_excluded'])
    matches = comparison(tables)
    assert len(matches) == 1 and matches[0].classification == 'candidate'
    assert len(matches[0].import_ids) == 2 and matches[0].posted


def test_import_only_payment_is_retained_and_distinguished_from_posted():
    tables = {'import_rows': [['payment-1', '', 'au PAYカード', 'notice', DAY, 'Synthetic merchant', 100, '', 'unclassified_card']]}
    matches = comparison(tables)
    assert len(matches) == 1 and matches[0].classification == 'candidate' and not matches[0].posted
    tables['expense_rows'] = [['aggregate', DAY, 'Synthetic merchant', 'Aggregate', 100, '', '', '',
                               'au PAYカード', '', 'payment-1', '', 'active']]
    matches = comparison(tables)
    assert len(matches) == 1 and matches[0].posted


@pytest.mark.parametrize('problem', ['missing_header', 'missing_import', 'missing_item', 'header_total',
    'import_total', 'wrong_parent', 'inactive_item', 'missing_commit', 'duplicate_id', 'nonsequential_items', 'split_payment'])
def test_incomplete_or_contradictory_parent_never_excludes_matching_item(problem):
    tables = receipt()
    if problem == 'missing_header': tables['receipt_rows'] = []
    if problem == 'missing_import': tables['import_rows'] = []
    if problem == 'missing_item': tables['expense_rows'].pop()
    if problem == 'header_total': tables['receipt_rows'][0][3] = 400
    if problem == 'import_total': tables['import_rows'][0][6] = 400
    if problem == 'wrong_parent': tables['expense_rows'][0][10] = 'missing-other-import'
    if problem == 'inactive_item': tables['expense_rows'][1][12] = 'inactive'
    if problem == 'missing_commit': tables['import_rows'][0][10] = ''
    if problem == 'duplicate_id': tables['expense_rows'].append(deepcopy(tables['expense_rows'][0]))
    if problem == 'nonsequential_items': tables['expense_rows'][1][0] = 'R-synthetic-retail-03'
    if problem == 'split_payment': tables['receipt_rows'][0][4] = '現金とカード併用'
    for days in (7, 31):
        matches = comparison(tables, days=days)
        assert matches and any(x.classification == 'unresolved' for x in matches)
    assert possible_duplicate(query(), tables)


def test_known_partial_payment_is_not_discarded_as_different_receipt_total():
    tables = receipt((80, 220))
    tables['import_rows'].append(['partial-card', '', 'au PAYカード', 'notice', DAY, 'Synthetic shop', 100,
        'card', 'matched_receipt', tables['import_rows'][0][0], 'b' * 64, '一部入金'])
    matches = comparison(tables)
    assert len(matches) == 1 and matches[0].classification == 'unresolved'
    assert 'linked_payment_totals_disagree' in matches[0].reasons


@pytest.mark.parametrize('method', ['現金+カード', 'cash/card', '現金とPayPay'])
def test_unresolved_split_method_cannot_exclude_a_component(method):
    tables = receipt(); tables['receipt_rows'][0][4] = method
    assert comparison(tables)[0].classification == 'unresolved'


def test_multiple_payment_claims_and_active_materializations_are_uncertain():
    tables = receipt();iid = tables['import_rows'][0][0]
    for n in (1, 2):
        tables['import_rows'].append([f'card-{n}', '', 'card', f'notice-{n}', DAY, 'Merchant', 300, '',
                                     'matched_receipt', iid, 'b' * 64, ''])
    assert 'multiple_settlement_records' in comparison(tables)[0].reasons
    tables['import_rows'].pop()
    tables['expense_rows'].append(['aggregate', DAY, 'Merchant', 'Payment', 300, '', '', '', 'card', '', 'card-1', '', 'active'])
    assert 'multiple_active_materializations' in comparison(tables)[0].reasons


def test_self_referencing_import_cannot_authorize_component_exclusion():
    tables = receipt();tables['import_rows'][0][9] = tables['import_rows'][0][0]
    assert comparison(tables)[0].classification == 'unresolved'


def test_different_headers_in_one_file_are_not_joined_by_source_or_merchant():
    first = receipt((40, 60), identity='document-page-1')
    second = receipt((30, 70), identity='document-page-2')
    first['import_rows'][0][3] = second['import_rows'][0][3] = 'same-multipage-source'
    tables = {k: first[k] + second[k] for k in first}
    matches = comparison(tables)
    assert len(matches) == 2 and all(x.classification == 'candidate' for x in matches)


def test_unrelated_broken_unit_does_not_hold_medical():
    tables = receipt(); broken = receipt((700, 200), identity='unrelated')
    broken['receipt_rows'] = []; broken['expense_rows'].pop()
    for kind in tables: tables[kind] += broken[kind]
    assert not possible_duplicate(query(), tables)
    assert [x.classification for x in comparison(tables)] == ['different']


@pytest.mark.parametrize('days', [7, 31])
@pytest.mark.parametrize('direction', [-1, 1])
def test_each_window_includes_its_boundary_and_excludes_next_day(days, direction):
    def shifted(distance): return (date.fromisoformat(DAY) + timedelta(days=direction * distance)).isoformat()
    assert len(comparison(receipt((100,), day=shifted(days)), days=days)) == 1
    assert comparison(receipt((100,), day=shifted(days + 1)), days=days) == ()


def test_unknown_dates_keep_the_existing_admission_hold():
    tables = {'import_rows': [['card', '', 'card', 'source', 'unusable', 'Merchant', 100, '', 'unclassified_card']]}
    assert possible_duplicate(query(), tables)
    assert comparison(tables, days=7) == ()


def test_orphan_active_row_is_uncertain_but_inactive_row_is_not_summed():
    row = ['standalone', DAY, 'Merchant', 'Unknown role', 100, '', '', '', '', '', '', '', 'active']
    assert comparison({'expense_rows': [row]})[0].classification == 'unresolved'
    row[12] = 'inactive'
    assert comparison({'expense_rows': [row]}) == ()


@pytest.mark.parametrize('status,note', [('transfer_aupay_charge', ''), ('refunded', ''),
    ('card_statement_total', ''), ('unclassified_card', '返金'), ('unclassified_card', '分割決済')])
def test_nonpurchase_or_unclear_payment_is_not_a_normal_purchase(status, note):
    tables = {'import_rows': [['payment', '', 'au PAYカード', 'notice', DAY, 'Merchant', 100, '', status, '', '', note]]}
    match, = comparison(tables)
    assert match.classification == 'unresolved' and 'payment_role_requires_review' in match.reasons


def test_refund_is_not_netted_against_a_purchase():
    tables = {'import_rows': [['sale', '', 'card', 'one', DAY, 'Merchant', 300, '', 'unclassified_card'],
                              ['refund', '', 'card', 'two', DAY, 'Merchant', -200, '', 'refunded']]}
    assert comparison(tables) == ()  # Never fabricates 300 - 200 = 100.
    assert len(comparison(tables, amount=300)) == 1


def test_medical_automatic_writer_and_admission_share_components_contract():
    service, store, db, _, _ = medical(); load(db, receipt())
    item = next(iter(service.items.values()));before = deepcopy(db.rows)
    assert not possible_duplicate(query(), service.tables())
    assert len(service._plan(item, query(), '', automatic=True)) == 3
    assert db.rows == before and item['inputs'] == [''] * 8
    # The explicitly chosen human/general paths keep their existing UI contract.
    with pytest.raises(ValueError, match='同日付近・同額の既存支出'):
        service._plan(item, query(), '', automatic=False)
    normal = dict(item, kind='normal', before={k: [] for k in service.tables()})
    with pytest.raises(ValueError, match='同日付近・同額の既存支出'):
        service._plan(normal, query(), '', automatic=True)


def test_writer_detects_matching_total_even_when_no_individual_item_matches():
    service, store, db, _, _ = medical(); load(db, receipt((40, 60)))
    item = next(iter(service.items.values()))
    with pytest.raises(ValueError, match='既存支払い'):
        service._plan(item, query(), '', automatic=True)
    with pytest.raises(ValueError, match='強制指定'):
        service._plan(item, query(), '', distinct=True, automatic=True)


def test_automatic_post_preserves_existing_categories_and_replay_sends_or_appends_zero():
    from test_medical_auto_posting import automatic, post
    from app.medical_candidate_runtime import process_plans
    store, plans, args, send, db, service = automatic()
    load(db, receipt((432, 568)));before = deepcopy(db.rows['支出明細'])
    assert process_plans(plans, **args)['medical_ai_requests'] == 1
    assert post(service, args) == 1
    assert db.rows['支出明細'][:len(before)] == before
    rows = deepcopy(db.rows); ai = deepcopy(store.value['medical_image_analyses'])
    assert process_plans(plans, **args)['medical_ai_requests'] == 0
    assert post(service, args) == 0 and db.rows == rows and store.value['medical_image_analyses'] == ai
    assert send.call_count == 1


def with_alias():
    tables=receipt((100,))
    tables['receipt_rows'].append(['R-copy',DAY,'Synthetic retailer',100,'','', '解析済','',''])
    tables['import_rows'].append(['receipt:copy','','receipt','copy',DAY,'Synthetic retailer',100,'',
                                  'matched_receipt',tables['expense_rows'][0][0],'b'*64,''])
    return tables


def test_committed_receipt_alias_is_one_payment_without_double_counting():
    tables=with_alias();before=deepcopy(tables)
    match,=comparison(tables)
    assert match.classification=='candidate' and len(match.receipt_ids)==2 and len(match.expense_ids)==1
    assert tables==before


@pytest.mark.parametrize('change',['amount','date','uncommitted','missing_target','inactive_target','own_expense','two_targets'])
def test_broken_alias_never_makes_a_payment_verified(change):
    tables=with_alias()
    if change=='amount':tables['receipt_rows'][-1][3]=101
    if change=='date':tables['receipt_rows'][-1][1]='2026-09-02'
    if change=='uncommitted':tables['import_rows'][-1][10]=''
    if change=='missing_target':tables['import_rows'][-1][9]='missing'
    if change=='inactive_target':tables['expense_rows'][0][12]='inactive'
    if change=='own_expense':tables['expense_rows'].append(['R-copy-01',DAY,'Synthetic retailer','Product',100,'','',
        '', 'receipt','R-copy','receipt:copy','','active'])
    if change=='two_targets':tables['expense_rows'].append(deepcopy(tables['expense_rows'][0]))
    assert any(x.classification=='unresolved' for x in comparison(tables))
