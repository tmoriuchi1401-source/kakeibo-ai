import json
from uuid import UUID

import pytest

from app.manual_entry import HEADER, execute, expense_id
from app.drive_run_state import StateError


FIRST = '12345678-1234-1234-1234-123456789abc'
CANCEL = '12345678-1234-1234-1234-123456789abd'


class FakeDB:
    def __init__(self):
        self.queue = [HEADER[:]]
        self.ledger = []
        self.writes = []

    def add(self, request_id=FIRST, payload=None):
        payload = payload or {'date': '2026-09-24', 'amount': 500, 'merchant': 'お祭り',
                              'major': 'その他', 'minor': '未分類', 'note': '', 'payment': '現金'}
        self.queue.append([request_id, 'dispatching', json.dumps(payload, ensure_ascii=False), '', '', ''])

    def get_raw(self, range_):
        assert range_ == "'_手入力受付'!A1:F"
        return [r[:] for r in self.queue]

    def set_raw_range(self, range_, rows):
        self.writes.append((range_, rows))
        if range_.startswith("'_手入力受付'!"):
            self.queue[int(range_.split('A')[-1]) - 1] = rows[0][:]
        else:
            number = int(range_.split('M')[-1])
            self.ledger[number-2][12] = rows[0][0]

    def categories(self):
        return [('その他', '未分類'), ('食費', '外食')]

    def expense_records(self):
        return {row[0]: (i, row[:]) for i, row in enumerate(self.ledger, 2)}

    def expense_index(self):
        return {row[0]: i for i, row in enumerate(self.ledger, 2)}

    def ensure_expense_status_column(self):
        pass

    def append_raw(self, sheet, rows):
        assert sheet == '支出明細'
        self.ledger.extend([row[:] for row in rows])


def test_post_and_replay_are_idempotent_and_do_not_expose_receipt():
    db = FakeDB()
    db.add()
    refreshed = []
    assert execute(db, FIRST, refresh_projection=lambda: refreshed.append(True)) == {'manual_posted': 1}
    row = db.ledger[0]
    assert row == [expense_id(FIRST), '2026-09-24', 'お祭り', '手入力', 500,
                   'その他', '未分類', '現金', 'manual', '', 'manual:' + FIRST, '', 'active']
    assert db.queue[1][1] == 'complete'
    assert execute(db, FIRST) == {'manual_ignored': 1}
    assert len(db.ledger) == len(refreshed) == 1


def test_cancel_before_post_skips_queued_request():
    db = FakeDB()
    db.add()
    db.add(CANCEL, {'target': FIRST})
    assert execute(db, CANCEL) == {'manual_cancelled': 1}
    assert execute(db, FIRST) == {'manual_ignored': 1}
    assert db.ledger == []
    assert db.queue[1][1] == 'cancelled'


def test_cancel_after_post_voids_without_deleting_ledger():
    db = FakeDB()
    db.add()
    execute(db, FIRST)
    db.add(CANCEL, {'target': FIRST})
    assert execute(db, CANCEL) == {'manual_cancelled': 1}
    assert db.ledger[0][12] == 'void'
    assert len(db.ledger) == 1
    assert db.queue[1][1] == 'cancelled'
    assert execute(db, CANCEL) == {'manual_ignored': 1}


def test_invalid_category_is_rejected_before_claim_or_ledger_write():
    db = FakeDB()
    db.add(payload={'date': '2026-09-24', 'amount': 500, 'merchant': 'お祭り',
                    'major': '食費', 'minor': '未分類', 'note': '', 'payment': '現金'})
    with pytest.raises(StateError, match='manual_payload_invalid'):
        execute(db, FIRST)
    assert db.queue[1][1] == 'error'
    assert db.ledger == []


@pytest.mark.parametrize('payment',['','PayPay','現金'])
def test_optional_payment_and_merchant_are_not_inferred(payment):
    db=FakeDB();db.add(payload={'date':'2026-09-24','amount':500,'merchant':'',
        'major':'食費','minor':'外食','note':'','payment':payment})
    assert execute(db,FIRST)=={'manual_posted':1}
    assert db.ledger[0][2]=='' and db.ledger[0][7]==payment


def test_pdf_source_barrier_runs_before_append_and_failure_never_posts():
    db=FakeDB();db.add()
    def changed(request_id,row):
        assert request_id==FIRST and not db.ledger
        raise StateError('manual_pdf_source_changed')
    with pytest.raises(StateError,match='source_changed'):
        execute(db,FIRST,before_append=changed)
    assert db.ledger==[] and db.queue[1][1]=='error'
    assert execute(db,FIRST)=={'manual_ignored':1}


def test_matching_id_with_different_content_is_never_overwritten():
    db=FakeDB();db.add();execute(db,FIRST)
    db.queue[1][1]='dispatching';db.ledger[0][4]=501
    with pytest.raises(StateError,match='content_conflict'):execute(db,FIRST)
    assert db.ledger[0][4]==501 and len(db.ledger)==1


def test_incorrect_readback_stays_error_no_automatic_second_append():
    db=FakeDB();db.add();append=db.append_raw
    def wrong(title,rows):
        append(title,rows);db.ledger[0][4]+=1
    db.append_raw=wrong
    with pytest.raises(StateError,match='readback_failed'):execute(db,FIRST)
    assert db.queue[1][1]=='error'
    assert execute(db,FIRST)=={'manual_ignored':1} and len(db.ledger)==1
