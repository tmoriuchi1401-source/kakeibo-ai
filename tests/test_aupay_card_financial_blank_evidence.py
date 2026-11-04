"""Synthetic range/values API representations of the same audited rows."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from app import aupay_card_incremental_recovery as recovery
from test_aupay_card_resume_recovery import no_write_proof


TITLES = ('支出明細', '取込データ', '収入明細', 'レシート')


def blank_proof(monkeypatch):
    native, parent, tables, documents = no_write_proof(monkeypatch)
    for title in TITLES:
        # Internal null followed by a populated cell; audited padding is empty.
        tables[(title, 'FORMULA')][1].extend([None, 'synthetic-memo'])
    monkeypatch.setattr(recovery, 'NO_WRITE_FINANCIAL_SHA256', recovery.financial_evidence(tables))
    return native, parent, tables, documents


def verify(native, parent, tables, documents, checkpoint=None):
    recovery.verify_no_write_state(native, parent, checkpoint or recovery.NO_WRITE_CHECKPOINT,
        financial=lambda: tables, money=SimpleNamespace(read=lambda k: deepcopy(documents[k])))


@pytest.mark.parametrize('title', TITLES)
@pytest.mark.parametrize('trailing', [[], [''], [None], ['', None, '']])
def test_range_null_and_values_empty_string_match_the_same_fixed_proof(monkeypatch, title, trailing):
    native, parent, tables, documents = blank_proof(monkeypatch)
    pin = recovery.NO_WRITE_FINANCIAL_SHA256
    tables[(title, 'FORMULA')][1][2] = ''
    tables[(title, 'FORMULA')][1].extend(trailing)
    tables[(title, 'FORMULA')].extend([[], ['', None]])
    before = deepcopy((native, parent, tables, documents))
    verify(native, parent, tables, documents)
    assert recovery.financial_evidence(tables) == pin
    assert (native, parent, tables, documents) == before


@pytest.mark.parametrize('replacement', [0, False, ' ', '0', 'None', '=IF(TRUE,"",0)'])
def test_only_empty_strings_and_null_are_equivalent(monkeypatch, replacement):
    native, parent, tables, documents = blank_proof(monkeypatch)
    tables[(TITLES[0], 'FORMULA')][1][2] = replacement
    with pytest.raises(RuntimeError, match='no_write_financial_changed'):
        verify(native, parent, tables, documents)


@pytest.mark.parametrize('bad', ['card_value', 'duplicate_id', 'second_card_booking',
                               'other_source_addition', 'category_edit', 'delete_row', 'header'])
def test_real_or_unreconciled_changes_still_stop_after_blank_normalization(monkeypatch, bad):
    native, parent, tables, documents = blank_proof(monkeypatch)
    rows = tables[(TITLES[1], 'FORMULA')]
    rows[1][2] = ''
    if bad == 'card_value': rows[1][1] += 1
    if bad == 'duplicate_id': rows.append(deepcopy(rows[1]))
    if bad == 'second_card_booking': rows.append(['synthetic-new-card-id', rows[1][1], None, 'synthetic-memo'])
    if bad == 'other_source_addition': rows.append(['synthetic-other-source', 123, None, 'synthetic-memo'])
    if bad == 'category_edit': rows[1][3] = 'synthetic-new-category'
    if bad == 'delete_row': rows.pop()
    if bad == 'header': rows[0][1] = 'synthetic-other-header'
    with pytest.raises(RuntimeError):
        verify(native, parent, tables, documents)


def test_blank_representation_does_not_allow_unknown_money_or_checkpoint_mismatch(monkeypatch):
    native, parent, tables, documents = blank_proof(monkeypatch)
    tables[(TITLES[0], 'FORMULA')][1][2] = ''
    with pytest.raises(RuntimeError):
        verify(native, parent, tables, documents, '2026-11-03T14:45:44+09:00')
    documents['money'] = {'state': 'UNKNOWN'}
    with pytest.raises(RuntimeError, match='no_write_money_changed'):
        verify(native, parent, tables, documents)


def test_historical_production_pins_are_preserved():
    # These commitments remain evidence of the original failure, not today's data.
    assert recovery.NO_WRITE_FINANCIAL_SHA256 == '4388e4c1abb0de1c14ef14adb7900d9c121c368db95676a249f893ca88c36d7a'
    assert recovery.NO_WRITE_MONEY_SHA256 == '58d87b362afc69e041de2c31e34e753c0959fb91146dc4518c1e58969c8224b2'
    assert recovery.NO_WRITE_NATIVE_SHA256 == '2ee8d898168d5db2ad59ed006944b2a8306316990c1c3df7ab5a2a62d5a44892'
    assert recovery.NO_WRITE_PARENT_SHA256 == 'cdee51239e3fadf3a34dec4c4a76e4db64c65dd827d1127bfe3557f1c09c9c14'
    assert recovery.NO_WRITE_RESULT_SHA256 == '258c1adcfe85f2e799f4b63eca53534fc5787fec7ae67c91d455011db5894c4c'


def test_tampered_fixed_financial_pin_is_not_bypassed_by_empty_strings(monkeypatch):
    native, parent, tables, documents = blank_proof(monkeypatch)
    tables[(TITLES[0], 'FORMULA')][1][2] = ''
    monkeypatch.setattr(recovery, 'NO_WRITE_FINANCIAL_SHA256', '0' * 64)
    with pytest.raises(RuntimeError, match='no_write_financial_changed'):
        verify(native, parent, tables, documents)
