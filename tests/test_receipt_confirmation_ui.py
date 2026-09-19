from copy import deepcopy
from types import SimpleNamespace

from app.receipt_confirmation import TITLE
from app.receipt_confirmation_ui import dropdown_requests, duplicate_options
from tests.test_receipt_confirmation import medical
from tests.test_receipt_hold_inventory import normal_service


def rules(requests, col, n=2):
    return [r['setDataValidation'].get('rule') for r in requests if 'setDataValidation' in r
            and r['setDataValidation']['range']['startColumnIndex'] == col
            and r['setDataValidation']['range']['startRowIndex'] == n-1]


def options(requests, col, n=2):
    return [x['userEnteredValue'] for x in rules(requests, col, n)[0]['condition'].get('values', [])]


def hidden(requests, start):
    return [r['updateDimensionProperties']['properties']['hiddenByUser'] for r in requests
            if r.get('updateDimensionProperties', {}).get('range', {}).get('startIndex') == start
            and 'hiddenByUser' in r['updateDimensionProperties']['properties']][0]


def test_existing_normal_receipt_is_decision_only_and_owner_values_never_written():
    review, store, db, *_ = normal_service()
    db.rows[TITLE][0][14] = 'owner note'
    before = deepcopy((store.value, db.rows))
    requests = dropdown_requests(review, 7, review.ui_rows())
    assert options(requests, 12) == ['保留', '既存値を維持', '候補明細で確定']
    assert hidden(requests, 7) and hidden(requests, 13)
    assert (store.value, db.rows) == before
    assert all(set(r) <= {'setDataValidation', 'repeatCell', 'updateDimensionProperties'} for r in requests)
    assert all('userEnteredValue' not in r.get('repeatCell', {}).get('fields', '') for r in requests)


def test_live_owner_input_keeps_columns_visible_and_legacy_decision_available():
    review, _, db, *_ = normal_service()
    db.rows[TITLE][0][8] = 'owner typing'
    db.rows[TITLE][0][12] = '重複候補と別の支出として確定'
    db.rows[TITLE][0][13] = 'previous selection'
    requests = dropdown_requests(review, 7, review.ui_rows())
    assert not hidden(requests, 7) and not hidden(requests, 13)
    assert db.rows[TITLE][0][12] in options(requests, 12)


def test_medical_controls_allow_new_facilities_and_payment_methods_without_autofilling():
    review, store, db, _, source = medical()
    item = next(iter(review.items.values()))
    item['medical_candidates'] = dict(source=source, candidate_id='test', date='2026-09-01', issuer='Candidate clinic', amount_yen=100, category='医療・保険｜病院')
    db.rows['支出明細'] = [['id', '2026-08-01', 'Past clinic', 'care', 200, '医療・保険', '病院', '独自決済']]
    before = deepcopy((store.value, db.rows))
    requests = dropdown_requests(review, 7, review.ui_rows())
    assert options(requests, 8) == ['Candidate clinic', 'Past clinic']
    assert not rules(requests, 8)[0]['strict'] and not rules(requests, 11)[0]['strict']
    assert '独自決済' in options(requests, 11)
    assert rules(requests, 7)[0]['condition']['type'] == 'DATE_IS_VALID'
    assert '候補で医療費を確定' in options(requests, 12)
    assert not hidden(requests, 7)
    assert (store.value, db.rows) == before
    item['medical_candidates']['source'] = dict(source, version='old')
    assert '候補で医療費を確定' not in options(dropdown_requests(review, 7, review.ui_rows()), 12)


def test_duplicate_dropdown_contains_only_matching_active_external_expenses():
    review, _, db, _, source = medical()
    row = db.rows[TITLE][0]
    row[7:11] = ['2026-09-01', 'Clinic', 100, '医療・保険｜病院']
    def expense(eid, day='2026-09-01', amount=100, status='active', rid='', iid=''):
        return [eid, day, 'Clinic', 'care', amount, '医療・保険', '病院', '現金', 'manual', rid, iid, '', status]
    db.rows['支出明細'] = [expense('match'), expense('wrong-amount', amount=200), expense('old', day='2026-08-24'),
        expense('inactive', status='duplicate_excluded'), expense('self', rid='R-'+source['source_id']), expense('self-import', iid='receipt:'+source['source_id'])]
    requests = dropdown_requests(review, 7, review.ui_rows())
    assert options(requests, 13) == ['match']
    note = next(r['repeatCell']['cell']['note'] for r in requests if r.get('repeatCell', {}).get('range', {}).get('startRowIndex') == 1
                and r['repeatCell']['range']['startColumnIndex'] == 13)
    assert '2026-09-01／Clinic／100円' in note
    row[9] = 200
    assert options(dropdown_requests(review, 7, review.ui_rows()), 13) == ['wrong-amount']


def test_intake_kind_is_optional_dropdown_without_granting_posting_authority():
    review, _, db, _, source = medical()
    review.observe_intake_hold(dict(source, source_id='other'), 'folder', SimpleNamespace(classification='sensitive_unknown'))
    review.render()
    requests = dropdown_requests(review, 7, review.ui_rows())
    assert options(requests, 12, 3) == ['保留']
    assert options(requests, 14, 3) == ['一般の買物', '医療', '対象外', '再撮影が必要']
    assert not rules(requests, 14, 3)[0]['strict']
