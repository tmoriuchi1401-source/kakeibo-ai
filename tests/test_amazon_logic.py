from app.amazon_pipeline import money, date_ymd

def test_money():
    assert money("13,136")==13136
    assert money("0")==0

def test_date():
    assert date_ymd("2023-12-03T11:30:15Z")=="2023-12-03"
