import ast
import sys

import pytest

from app.cli import main
from app.paypay_pipeline import PayPayPipeline, parse_paypay_csv


HEADER = (
    "取引日,出金金額（円）,入金金額（円）,海外出金金額,通貨,変換レート（円）,利用国,"
    "取引内容,取引先,取引方法,支払い区分,利用者,取引番号\n"
)


def row(kind, transaction_id, amount="", merchant="", method="PayPay残高",
        date="2026/08/01 12:34"):
    return (
        f'{date},"{amount}",,,,,,{kind},{merchant},{method},一回払い,本人,'
        f"{transaction_id}\n"
    )


@pytest.fixture
def paypay_csv(tmp_path):
    path = tmp_path / "anonymous-paypay.csv"
    content = HEADER + "".join((
        row("支払い", "TEST-PAY-001", "1,200", "テスト食堂"),
        row("支払い", "TEST-PAY-002", "350", "匿名ストア", "PayPayカード"),
        row("チャージ", "TEST-CHARGE-001", merchant="PayPay残高"),
        row("送金", "TEST-SEND-001", merchant="匿名ユーザーA"),
        row("受取", "TEST-RECEIVE-001", merchant="匿名ユーザーB"),
        row("ポイント獲得", "TEST-POINT-001", merchant="PayPayポイント"),
    ))
    path.write_text(content, encoding="utf-8-sig")
    return path


def test_only_payment_rows_are_extracted(paypay_csv):
    transactions = parse_paypay_csv(paypay_csv)
    assert [item["transaction_id"] for item in transactions] == [
        "TEST-PAY-001", "TEST-PAY-002"
    ]


@pytest.mark.parametrize("excluded", ["チャージ", "送金", "受取", "ポイント獲得"])
def test_non_payment_transaction_is_excluded(tmp_path, excluded):
    path = tmp_path / "excluded.csv"
    path.write_text(
        HEADER + row("支払い", "TEST-PAY-001", "100", "テスト店舗")
        + row(excluded, "TEST-OTHER-001", "500", "除外対象"),
        encoding="utf-8-sig",
    )
    assert [item["transaction_id"] for item in parse_paypay_csv(path)] == ["TEST-PAY-001"]


def test_payment_fields_are_normalized(paypay_csv):
    transaction = parse_paypay_csv(paypay_csv)[0]
    assert transaction == {
        "date": "2026-08-01",
        "merchant": "テスト食堂",
        "amount": 1200,
        "payment_type": "PayPay残高",
        "transaction_id": "TEST-PAY-001",
        "import_id": "paypay:TEST-PAY-001",
        "payment_category": "一回払い",
        "user": "本人",
    }
    assert isinstance(transaction["amount"], int)


def test_import_id_is_stable(paypay_csv):
    first = parse_paypay_csv(paypay_csv)
    second = parse_paypay_csv(paypay_csv)
    assert [item["import_id"] for item in first] == [item["import_id"] for item in second]


def test_timestamp_with_seconds_is_converted_to_date(tmp_path):
    path = tmp_path / "paypay-with-seconds.csv"
    path.write_text(
        HEADER + row(
            "支払い", "TEST-PAY-SECONDS", "100", "秒あり店舗",
            date="2026/08/01 12:34:56",
        ),
        encoding="utf-8-sig",
    )
    assert parse_paypay_csv(path)[0]["date"] == "2026-08-01"


def test_preview_counts_total_and_samples(paypay_csv):
    result = PayPayPipeline().preview(paypay_csv, sample_limit=1)
    assert result["summary"] == {
        "rows": 6,
        "payments": 2,
        "non_payments": 4,
        "payment_total": 1550,
    }
    assert len(result["payment_samples"]) == 1
    assert result["payment_samples"][0]["merchant"] == "テスト食堂"


def test_cp932_csv_is_supported(tmp_path):
    path = tmp_path / "paypay-cp932.csv"
    path.write_bytes((HEADER + row("支払い", "TEST-PAY-003", "980", "CP932店舗")).encode("cp932"))
    assert parse_paypay_csv(path)[0]["amount"] == 980


def test_cli_paypay_preview_does_not_require_sheets(paypay_csv, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["app.cli", "paypay-preview", str(paypay_csv)])
    main()
    output = ast.literal_eval(capsys.readouterr().out.strip())
    assert output["summary"]["payments"] == 2


class FakeDB:
    def __init__(self):
        self.rows = []

    def import_ids(self):
        return {row[0] for row in self.rows}

    def append(self, sheet, rows):
        assert sheet == "取込データ"
        self.rows.extend(rows)


def test_import_writes_payments_in_import_data_format(paypay_csv):
    db = FakeDB()
    result = PayPayPipeline(db).import_csv(paypay_csv)

    assert result == {
        "source_rows": 2,
        "new": 2,
        "unchanged": 0,
        "unclassified_paypay": 2,
    }
    first = db.rows[0]
    assert first[0] == "paypay:TEST-PAY-001"
    assert first[2:10] == [
        "PayPay", "TEST-PAY-001", "2026-08-01", "テスト食堂", 1200,
        "PayPay残高", "unclassified_paypay", "",
    ]
    assert first[10]
    assert first[11] == "支払い区分=一回払い; 利用者=本人"


def test_reimport_is_unchanged_and_does_not_append_duplicates(paypay_csv):
    db = FakeDB()
    pipeline = PayPayPipeline(db)
    pipeline.import_csv(paypay_csv)
    result = pipeline.import_csv(paypay_csv)

    assert result == {
        "source_rows": 2,
        "new": 0,
        "unchanged": 2,
        "unclassified_paypay": 0,
    }
    assert len(db.rows) == 2
