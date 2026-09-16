"""Synthetic local date roles and adjacency; no source dates or OCR fixture."""
import pytest
from app.medical_local_date import receipt_date


def token(text,x=10,y=10,line=1,confidence=90):
    return dict(text=text,box=(x,y,x+len(text)*12,y+20),confidence=confidence,line=(1,1,line))


@pytest.mark.parametrize('text,expected',[
    ('支払日 2025 / 2 / 3','2025-02-03'),('領収日 ２０２５年２月３日','2025-02-03'),
    ('会計日 2025.2.3','2025-02-03'),('発行日 令和 7 年 2 月 3 日','2025-02-03'),
    ('領収日 平成31年4月30日','2019-04-30'),('入金日 令和元年5月1日','2019-05-01')])
def test_supported_formats_retain_the_explicit_role(text,expected):
    day,p=receipt_date([token(text)])
    assert day==expected and p['date_evidence_verified'] and p['date_candidates']==1


@pytest.mark.parametrize('text',['生年月日 平成10年2月3日','診療日 2025/2/3','受診日 2025/2/3',
    '処方日 2025/2/3','支払期限 2025/2/3','期間 2025/2/3','2025/2/3',
    '発行日 平成31年5月1日','支払日 令和元年4月30日','支払日 2025/2/30',
    '会計日 2025/2/3～2025/2/4'])
def test_other_date_roles_invalid_dates_and_periods_are_not_payment(text):
    assert not receipt_date([token(text)])[0]


def test_split_neighbor_cells_and_low_confidence_unrelated_text():
    obs=[token('発行日',x=10,line=1),token('令和7年2月3日',x=80,line=2),
         token('別の記載',x=300,line=1,confidence=2)]
    day,p=receipt_date(obs)
    assert day=='2025-02-03' and p['date_basis']=='issue'
    obs[1]['confidence']=20
    assert not receipt_date(obs)[0]


def test_birth_and_issue_in_one_ocr_group_are_not_conflated():
    obs=[token('生年月日 平成10年2月3日',x=10),token('発行日 令和7年2月3日',x=360)]
    assert receipt_date(obs)[0]=='2025-02-03'
    obs.append(token('支払日 2025/2/4',x=10,y=60,line=2))
    assert receipt_date(obs)[1]['date_candidates']==2 and not receipt_date(obs)[0]


def test_spatially_unrelated_date_cannot_borrow_a_role():
    assert not receipt_date([token('発行日'),token('令和7年2月3日',x=900,line=2)])[0]
