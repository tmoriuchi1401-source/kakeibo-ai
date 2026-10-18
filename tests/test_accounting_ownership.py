from copy import deepcopy
import pytest

from app.drive_run_state import StateError
from app.iphone_accounting_ownership import relation, native_identity, parse_ownership
from accounting_ownership_fixtures import OwnershipFixture


@pytest.fixture
def f(): return OwnershipFixture()


@pytest.mark.parametrize('left,right', [('normal_auto','pdf_manual'), ('legacy_confirmation','pdf_auto'),
    ('pdf_auto','pdf_manual'), ('pdf_manual','grouped_unit'), ('explicit_unit','future_one_tab'),
    ('buyback_income','explicit_unit')])
def test_whole_source_and_same_unit_conflict_across_native_namespaces(f, left, right):
    assert relation(f.observe(left).value(), f.observe(right).value()) == 'conflict'


@pytest.mark.parametrize('entry', ['pdf_manual', 'grouped_unit', 'explicit_unit'])
def test_only_confirmed_nonoverlapping_units_are_independent(f, entry):
    assert relation(f.observe(entry,'u1').value(), f.observe(entry,'u2').value()) == 'independent'


def test_whole_is_not_an_invented_unit(f):
    result = f.observe('normal_auto')
    assert result.scope == 'whole' and result.unit is None
    assert result.source['drive_revision_id'] != result.source['native_source_version']


@pytest.mark.parametrize('change', ['revision','numeric','native_version','hash','partition','range'])
def test_version_or_partition_changes_do_not_release_a_prior_fence(f, change):
    before = f.observe().value(); after = deepcopy(before)
    if change == 'revision': after['source']['drive_revision_id'] = 'new-drive-revision'
    elif change == 'numeric': after['source']['drive_numeric_version'] = '20'
    elif change == 'native_version': after['source']['native_source_version'] = 'native-new'
    elif change == 'hash': after['source']['sha256'] = 'b'*64
    elif change == 'partition': after['partition']['revision'] += 1
    else:
        after['partition']['units'][0]['members'][0]['box'][3] = .4
        after['unit'] = deepcopy(after['partition']['units'][0])
    if change in {'revision','numeric','native_version'}:
        after['partition']['source']=deepcopy(after['source'])
    with pytest.raises(StateError, match='UNKNOWN'): relation(before, after)


@pytest.mark.parametrize('which', ['ledger','pending'])
@pytest.mark.parametrize('scope', ['whole','unit'])
def test_existing_ledger_or_old_pending_blocks_accounting(f, which, scope):
    prior = f.observe('normal_auto' if scope == 'whole' else 'pdf_manual').value()
    port = getattr(f, which)
    port.frame['records'] = ([dict(transaction_id=prior['ledger_targets'][0], ownership=prior)]
                            if which == 'ledger' else [prior])
    with pytest.raises(StateError): f.observe()


def test_old_pending_even_on_disjoint_unit_is_unknown(f):
    f.pending.frame['records'] = [f.observe('pdf_manual','u2').value()]
    with pytest.raises(StateError, match='UNKNOWN'): f.observe('pdf_manual','u1')


def test_disjoint_unit_ledger_does_not_authorize_a_whole_source(f):
    prior = f.observe('pdf_manual','u2').value()
    f.ledger.frame['records'] = [dict(transaction_id=prior['ledger_targets'][0], ownership=prior)]
    assert f.observe('pdf_manual','u1').scope == 'unit'
    with pytest.raises(StateError): f.observe('normal_auto')


@pytest.mark.parametrize('field', ['drive_revision_id','drive_numeric_version','native_source_version'])
def test_missing_independent_version_information_is_unknown(f, field):
    f.native.frame['pdf_manual','u1']['source'][field] = None
    with pytest.raises(StateError, match='UNKNOWN'): f.observe()


@pytest.mark.parametrize('bad', ['unknown_partition','unconfirmed','incomplete','overlap','duplicate_unit',
    'wrong_page','wrong_index','native_identity','purpose','held','ledger_incomplete','pending_corrupt'])
def test_ownership_never_uses_sheet_rows_links_or_similarity(f, bad):
    p = f.partition.frame; n = f.native.frame['pdf_manual','u1']
    if bad == 'unknown_partition': p['state'] = 'unknown'
    elif bad == 'unconfirmed': p['state'] = 'proposed'
    elif bad == 'incomplete': p['complete'] = False
    elif bad == 'overlap': p['units'][1]['members'][0]['box'] = deepcopy(p['units'][0]['members'][0]['box'])
    elif bad == 'duplicate_unit': p['units'].append(deepcopy(p['units'][0]))
    elif bad == 'wrong_page': p['units'][0]['members'][0]['identity'] = 'a'*64
    elif bad == 'wrong_index': n['receipt_index'] = 19
    elif bad == 'native_identity': n['operation_identity'] = 'a'*64
    elif bad == 'purpose': n['purpose'] = 'STRUCTURE_REVIEW'
    elif bad == 'held': n['case_id'] = 'G103'
    elif bad == 'ledger_incomplete': f.ledger.frame['complete'] = False
    else: f.pending.frame['records'] = [{'guessed_from_original_url': True}]
    with pytest.raises(StateError): f.observe()


@pytest.mark.parametrize('port', ['source','partition','native','ledger','pending'])
def test_changes_mid_observation_fail_closed(f, port):
    def change(current, *args):
        if current.reads == 2:
            if port == 'native': current.frame['pdf_manual','u1']['generation'] += 1
            else: current.frame['generation'] += 1
    getattr(f,port).on_read = change
    with pytest.raises(StateError, match='UNKNOWN'): f.observe()


def test_different_originals_require_independent_ledger_and_operation_identities(f):
    left = f.observe().value(); right = deepcopy(left)
    right['source']['source_id'] = 'other-source'
    right['partition']['source'] = deepcopy(right['source'])
    with pytest.raises(StateError): relation(left, right)
    right['operation_identity'] = 'e'*64; right['ledger_targets'] = ['other-native-ledger']
    assert relation(left, right) == 'independent'


def test_observation_is_read_only_and_round_trips(f):
    before = deepcopy((f.source.frame, f.partition.frame, f.native.frame, f.ledger.frame, f.pending.frame))
    result = f.observe()
    assert parse_ownership(result.value()) == result
    assert before == (f.source.frame, f.partition.frame, f.native.frame, f.ledger.frame, f.pending.frame)
