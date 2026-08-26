from email.message import EmailMessage

from app.amazon_review import AMAZON_REVIEW_HEADERS
from app.amazon_review_preview import preview_amazon_reviews
from app.sheets import HEADERS
from app.amazon_email import parse_amazon_email
from app.amazon_gmail_storage import GmailRawMessage


EXISTING_REVIEW_HEADERS = [
    "確認ID", "優先度", "日付", "データ元", "店舗", "金額", "状態", "推奨対応", "備考",
    "ユーザー判断", "統合先取込ID", "カテゴリ（大｜小）", "小カテゴリ（従来）", "ユーザー備考",
    "反映結果", "Amazon候補", "Amazon候補数", "Amazon注文候補選択", "Amazon候補ID", "Amazon選択状態",
]


def _message(body="この商品をキャンセル\n数量: 1"):
    email = EmailMessage()
    email["Subject"] = "注文のキャンセル"
    email["From"] = "Amazon.co.jp <no-reply@amazon.co.jp>"
    email["Date"] = "Mon, 24 Aug 2026 12:00:00 +0900"
    email.set_content(body)
    return GmailRawMessage("private-gmail-id", "private-thread-id", email.as_bytes())


class ReadOnlyDB:
    def __init__(self, *, exists=False, header=None, review_rows=None, orders=None):
        self.exists = exists
        self.header = AMAZON_REVIEW_HEADERS if header is None else header
        self.review_rows = list(review_rows or [])
        self.orders = list(orders or [])
        self.reads = []

    def sheet_titles(self):
        return ["Amazon要確認"] if self.exists else []

    def get(self, rng):
        self.reads.append(rng)
        values = {
            "Amazon要確認!1:1": [self.header],
            "Amazon要確認!A2:N": self.review_rows,
            "Amazon注文!A2:O": self.orders,
            "Amazon注文ヘッダ!A2:O": [],
            "Amazonイベント!A2:X": [],
        }
        return values[rng]

    def __getattr__(self, name):
        if name.startswith(("append", "update", "clear", "ensure", "configure")):
            raise AssertionError(f"write method must not be used: {name}")
        raise AttributeError(name)


def _preview(db, messages=None):
    return preview_amazon_reviews(
        object(), db, created_at="2026-08-26 12:34:56",
        fetcher=lambda service: list(messages or [_message()]),
    )


def test_missing_sheet_is_normal_and_plans_new_cancellation_row():
    result = _preview(ReadOnlyDB())

    assert result["review_sheet_exists"] == 0
    assert result["review_schema_match"] == 0
    assert result["existing_review_rows"] == 0
    assert result["existing_review_ids"] == 0
    assert result["planned_review_rows"] == 1
    assert result["planned_new_rows"] == 1
    assert result["planned_cancellation"] == 1
    assert result["planned_status_unreviewed"] == 1


def test_matching_schema_reads_existing_id_and_counts_duplicate():
    message = _message()
    first = _preview(ReadOnlyDB(), [message])
    assert first["planned_new_rows"] == 1

    from app.amazon_cancellation_return_preview import _review_key
    review_id = _review_key(message.raw_mime, parse_amazon_email(message.raw_mime).source_hash)
    db = ReadOnlyDB(exists=True, review_rows=[[review_id] + [""] * 13])
    result = _preview(db, [message])

    assert result["review_schema_match"] == 1
    assert result["existing_review_rows"] == 1
    assert result["existing_review_ids"] == 1
    assert result["duplicate_review_ids"] == 1
    assert result["planned_new_rows"] == 0


def test_schema_mismatch_stops_without_reading_rows_or_gmail():
    fetched = []
    db = ReadOnlyDB(exists=True, header=["wrong"])
    result = preview_amazon_reviews(
        object(), db, fetcher=lambda service: fetched.append(True) or [],
    )

    assert result["review_schema_match"] == 0
    assert result["planned_review_rows"] == 0
    assert fetched == []
    assert "Amazon要確認!A2:N" not in db.reads


def test_multiple_candidates_reason_is_counted_without_private_output():
    orders = [
        ["key", order_id, "asin", "2026-08-20", "対象商品", 1, 1200,
         "payment", "major", "minor", "", "hash", "timestamp", "", 1]
        for order_id in ("PRIVATE-ORDER-1", "PRIVATE-ORDER-2")
    ]
    result = _preview(ReadOnlyDB(orders=orders), [_message("対象商品: 対象商品\n数量: 1\n金額: 1200円\nキャンセル")])
    rendered = str(result)

    assert result["planned_multiple_candidates"] == 1
    assert "PRIVATE-ORDER" not in rendered
    assert "private-gmail-id" not in rendered
    assert "private-thread-id" not in rendered


def test_existing_review_schema_remains_unchanged():
    assert HEADERS["要確認"] == EXISTING_REVIEW_HEADERS


def test_cli_uses_readonly_gmail_and_sheets(monkeypatch, capsys):
    import sys

    import app.cli as cli

    gmail_service = object()
    sheets_service = object()
    expected_db = object()

    class FakeSettings:
        gmail_token_json = "readonly-token"
        spreadsheet_id = "private-sheet"

        def validate(self, **kwargs):
            assert kwargs == {"need_gmail": True, "need_sheet": True}

    monkeypatch.setattr(cli, "Settings", FakeSettings)
    monkeypatch.setattr(
        cli, "gmail_readonly_service",
        lambda token: gmail_service if token == "readonly-token" else None,
    )
    monkeypatch.setattr(cli, "read_only_sheets_service", lambda: sheets_service)
    monkeypatch.setattr(
        cli, "SheetsDB",
        lambda spreadsheet_id, service: expected_db
        if spreadsheet_id == "private-sheet" and service is sheets_service else None,
    )
    monkeypatch.setattr(
        cli, "preview_amazon_reviews",
        lambda gmail, db: {"planned_review_rows": 1}
        if gmail is gmail_service and db is expected_db else {},
    )
    monkeypatch.setattr(sys, "argv", ["kakeibo-ai", "amazon-review-preview"])

    cli.main()

    assert capsys.readouterr().out.strip() == "{'planned_review_rows': 1}"
