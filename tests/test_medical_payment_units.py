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


def balance_transfer(kind='csv'):
    iid = ('aupaycsv:' if kind == 'csv' else 'aupaycard-mail:') + '1' * 24
    if kind != 'csv': iid += ':001'
    return [iid, '', 'au PAY' if kind == 'csv' else 'au PAYカード', iid, DAY,
            'オートチャージ　au PAY カード' if kind == 'csv' else 'au PAY 残高オートチャージ(不足額)',
            100, 'au PAY' if kind == 'csv' else '通常払い', 'transfer_aupay_charge', '', 'a' * 64,
            'CSV種別=オートチャージ; 利用日時=2026-09-01 12:00' if kind == 'csv' else 'メール明細No.001']


@pytest.mark.parametrize('kind', ['csv', 'card'])
@pytest.mark.parametrize('days', [7, 31])
def test_verified_topup_excluded_but_same_day_same_amount_purchase_retained(kind, days):
    tables = {'import_rows': [balance_transfer(kind)]}
    match, = comparison(tables, days=days)
    assert match.classification == 'different'
    assert match.reasons == ('verified_balance_transfer_not_purchase',)
    assert not possible_duplicate(query(), tables)
    # A top-up never removes or nets the real spending that follows it.
    tables['import_rows'].append(['purchase', '', 'au PAY', 'notice', DAY, 'Synthetic clinic',
                                  100, 'au PAY', 'auto_expense', '', 'b' * 64, ''])
    assert possible_duplicate(query(), tables)
    assert [m.classification for m in comparison(tables, days=days)] == ['different', 'candidate']


@pytest.mark.parametrize('change', ['unknown_source', 'invalid_id', 'source_mismatch', 'no_hash', 'date',
    'status_only', 'merchant_suffix', 'purchase_kind', 'refund', 'split_method', 'linked', 'expense', 'duplicate_id'])
def test_unverified_or_contradictory_transfer_remains_a_candidate(change):
    row = balance_transfer(); tables = {'import_rows': [row]}
    if change == 'unknown_source': row[2] = 'unknown'
    if change == 'invalid_id': row[0] = row[3] = 'unknown-id'
    if change == 'source_mismatch': row[3] = 'another-source'
    if change == 'no_hash': row[10] = ''
    if change == 'date': row[4] = ''
    if change == 'status_only': row[5] = 'Synthetic clinic'
    if change == 'merchant_suffix': row[5] += ' 医療費'
    if change == 'purchase_kind': row[11] = 'CSV種別=支払い'
    if change == 'refund': row[6] = -100; tables['expense_rows'] = [
        ['expense', DAY, 'Merchant', 'Unknown', 100, '', '', '', '', '', row[0], '', 'active']]
    if change == 'split_method': row[7] = '現金+au PAY'
    if change == 'linked': row[9] = 'missing'
    if change == 'expense': tables['expense_rows'] = [
        ['expense', DAY, 'Merchant', 'Unknown', 100, '', '', '', 'au PAY', '', row[0], '', 'active']]
    if change == 'duplicate_id': tables['import_rows'].append(deepcopy(row))
    assert possible_duplicate(query(), tables)
    assert all(m.classification != 'different' for m in comparison(tables))


def test_local_medical_can_post_with_only_verified_topup_and_replay_is_noop():
    from app.medical_local_reading import apply_local
    from test_medical_local_reading import proof, parsed
    service, store, db, _, source = medical()
    row = balance_transfer(); row[6] = parsed().total
    db.rows['取込データ'] = [deepcopy(row)]
    assert apply_local(service, source, 'synthetic-folder', parsed(), proof())
    assert len(db.rows['支出明細']) == 1 and db.rows['取込データ'][0] == row
    before = deepcopy(db.rows)
    assert not apply_local(service, source, 'synthetic-folder', parsed(), proof())
    assert db.rows == before


def school_collection():
    from app.auto_expense import expense_id
    iid = 'bankpdf:chiba:synthetic-account:' + '1'*24; eid = expense_id(iid)
    description = 'チバシキユウシヨクヒトウ'  # Published public descriptor, not household data.
    return {'receipt_rows': [], 'review_rows': [],
        'import_rows': [[iid, '', '千葉銀行PDF', iid, DAY, description, -100, '銀行口座',
                         'auto_expense', eid, '1'*64, 'page=1;row=1; 銀行最終判定=bank_expense_authority']],
        'expense_rows': [[eid, DAY, description, '自動計上', 100, 'その他', '未分類', '銀行口座',
                         '千葉銀行PDF', '', iid, 'bank_expense_authority', 'active']]}


@pytest.mark.parametrize('description', ['チバシキュウショクヒトウ', 'チバシガッコウキュウショクヒトウ',
                                       'ﾁﾊﾞｼｷﾕｳｼﾖｸﾋﾄｳ'])
@pytest.mark.parametrize('days', [7, 31])
def test_published_school_descriptor_with_consistent_bank_authority_is_distinct(description, days):
    tables = school_collection()
    tables['expense_rows'][0][2] = tables['import_rows'][0][5] = description
    before = deepcopy(tables)
    match, = comparison(tables, days=days)
    assert match.classification == 'different'
    assert match.reasons == ('verified_school_collection_not_medical_payment',)
    assert not possible_duplicate(query(), tables) and tables == before


@pytest.mark.parametrize('change', ['unknown_descriptor', 'substring', 'medical_counterparty', 'source',
    'id_namespace', 'hash', 'source_id', 'missing_expense', 'missing_import', 'amount', 'direction',
    'date', 'expense_link', 'import_link', 'authority', 'note', 'status', 'inactive', 'method',
    'merchant_conflict', 'medical_category', 'duplicate_import', 'receipt_link'])
def test_name_alone_or_any_broken_bank_contract_cannot_discard_medical_candidate(change):
    tables = school_collection(); row = tables['import_rows'][0]; expense = tables['expense_rows'][0]
    if change == 'unknown_descriptor': row[5] = expense[2] = '別の市給食費'
    if change == 'substring': row[5] = expense[2] = 'チバシキユウシヨクヒトウ医療費'
    if change == 'medical_counterparty': row[5] = expense[2] = 'Synthetic clinic'
    if change == 'source': row[2] = expense[8] = 'au PAYカード'
    if change == 'id_namespace': row[0] = row[3] = expense[10] = 'unknown-id'
    if change == 'hash': row[10] = '2'*64
    if change == 'source_id': row[3] = 'different-source'
    if change == 'missing_expense': tables['expense_rows'] = []; row[6] = 100
    if change == 'missing_import': tables['import_rows'] = []
    if change == 'amount': row[6] = -101
    if change == 'direction': row[6] = 100
    if change == 'date': row[4] = ''
    if change == 'expense_link': expense[10] = 'missing'
    if change == 'import_link': row[9] = 'missing'
    if change == 'authority': expense[11] = 'manual'
    if change == 'note': row[11] = '未確認'
    if change == 'status': row[8] = 'needs_review_bank_finalization'
    if change == 'inactive': expense[12] = 'inactive'; row[6] = 100
    if change == 'method': expense[7] = row[7] = '不明'
    if change == 'merchant_conflict': expense[2] = 'Synthetic clinic'
    if change == 'medical_category': expense[5:7] = ['医療・保険', '病院']
    if change == 'duplicate_import': tables['import_rows'].append(deepcopy(row))
    if change == 'receipt_link': expense[9] = 'missing-receipt'
    assert possible_duplicate(query(), tables)
    assert all(m.classification != 'different' for m in comparison(tables))


def test_school_rule_cannot_ignore_an_actual_medical_payment_or_nonmedical_query():
    tables = school_collection()
    nonmedical = query(); nonmedical.items[0].major_category = '教育'; nonmedical.items[0].minor_category = '学校'
    assert compare_payments(nonmedical, tables, days=31, unknown_dates=True)[0].classification == 'unresolved'
    actual = receipt((100,), identity='actual-medical')
    for key in tables: tables[key] += actual[key]
    assert possible_duplicate(query(), tables)
    assert [m.classification for m in comparison(tables)].count('candidate') == 1


def test_school_collection_is_preserved_when_local_medical_posts_and_replays():
    from app.medical_local_reading import apply_local
    from test_medical_local_reading import parsed, proof
    service, store, db, _, source = medical()
    tables = school_collection(); tables['expense_rows'][0][4] = parsed().total
    tables['import_rows'][0][6] = -parsed().total; load(db, tables)
    original = deepcopy(db.rows['支出明細'])
    assert apply_local(service, source, 'synthetic-folder', parsed(), proof())
    assert db.rows['支出明細'][:1] == original and len(db.rows['支出明細']) == 2
    before = deepcopy(db.rows)
    assert not apply_local(service, source, 'synthetic-folder', parsed(), proof())
    assert db.rows == before


@pytest.mark.parametrize('change', ['new_payment', 'changed_school_descriptor', 'changed_school_amount'])
def test_changed_evidence_after_intent_aborts_before_any_medical_accounting(change):
    from app.medical_local_reading import apply_local
    from test_medical_local_reading import parsed, proof
    service, store, db, verify, source = medical()
    tables = school_collection(); tables['expense_rows'][0][4] = parsed().total
    tables['import_rows'][0][6] = -parsed().total; load(db, tables)
    calls = 0
    def concurrent_change(*args):
        nonlocal calls
        calls += 1
        if calls != 2: return
        if change == 'new_payment':
            db.rows['取込データ'].append(['new-card', '', 'card', 'notice', DAY, 'Synthetic clinic',
                                          parsed().total, '', 'auto_expense', '', 'b'*64, ''])
        if change == 'changed_school_descriptor': db.rows['取込データ'][0][5] = 'Unresolved counterparty'
        if change == 'changed_school_amount': db.rows['支出明細'][0][4] = 999
    verify.side_effect = concurrent_change
    assert not apply_local(service, source, 'synthetic-folder', parsed(), proof())
    assert len(db.rows['支出明細']) == 1 and not db.rows['レシート']
    assert not any(r[0] == 'receipt:'+source['source_id'] for r in db.rows['取込データ'])
    item = next(iter(service.items.values()))
    assert item['status'] == 'waiting' and item['aborted_before_accounting']
