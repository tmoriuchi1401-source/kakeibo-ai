import sys

from app import cli
from app.amazon_review import AMAZON_REVIEW_HEADERS, AMAZON_REVIEW_SHEET
from app.amazon_review_schema_install import install_amazon_review_schema
from app.sheets import HEADERS


EXISTING_REVIEW_HEADERS = [
    "確認ID", "優先度", "日付", "データ元", "店舗", "金額", "状態", "推奨対応", "備考",
    "ユーザー判断", "統合先取込ID", "カテゴリ（大｜小）", "小カテゴリ（従来）", "ユーザー備考",
    "反映結果", "Amazon候補", "Amazon候補数", "Amazon注文候補選択", "Amazon候補ID", "Amazon選択状態",
]


class Request:
    def __init__(self, callback=None):
        self.callback = callback

    def execute(self):
        if self.callback:
            self.callback()
        return {}


class ValuesAPI:
    def __init__(self, service):
        self.service = service

    def update(self, **kwargs):
        def apply():
            assert kwargs["range"] == "Amazon要確認!A1:N1"
            self.service.headers[AMAZON_REVIEW_SHEET] = list(kwargs["body"]["values"][0])
            self.service.header_updates.append(kwargs)

        return Request(apply)

    def append(self, **kwargs):
        raise AssertionError("installer must not append rows")

    def clear(self, **kwargs):
        raise AssertionError("installer must not clear data")


class SpreadsheetsAPI:
    def __init__(self, service):
        self.service = service
        self.values_api = ValuesAPI(service)

    def batchUpdate(self, **kwargs):
        def apply():
            requests = kwargs["body"]["requests"]
            assert len(requests) == 1
            properties = requests[0]["addSheet"]["properties"]
            assert properties["title"] == AMAZON_REVIEW_SHEET
            self.service.headers[AMAZON_REVIEW_SHEET] = []
            self.service.batch_updates.append(kwargs)

        return Request(apply)

    def values(self):
        return self.values_api


class Service:
    def __init__(self, headers=None):
        self.headers = {name: list(values) for name, values in (headers or {}).items()}
        self.batch_updates = []
        self.header_updates = []
        self.api = SpreadsheetsAPI(self)

    def spreadsheets(self):
        return self.api


class DB:
    sid = "private-sheet-id"

    def __init__(self, service):
        self.svc = service
        self.reads = []

    def sheet_titles(self):
        return list(self.svc.headers)

    def get(self, rng):
        self.reads.append(rng)
        header = self.svc.headers[AMAZON_REVIEW_SHEET]
        return [list(header)] if header else []

    def __getattr__(self, name):
        if name.startswith(("append", "update", "clear", "ensure", "configure")):
            raise AssertionError(f"unexpected DB write: {name}")
        raise AttributeError(name)


def test_missing_sheet_creates_only_target_and_writes_exact_header():
    service = Service({"要確認": EXISTING_REVIEW_HEADERS, "Amazon注文": ["existing"]})

    result = install_amazon_review_schema(DB(service))

    assert result == {
        "sheet_created": 1, "schema_already_valid": 0,
        "header_written": 1, "schema_mismatch": 0,
    }
    assert service.headers[AMAZON_REVIEW_SHEET] == AMAZON_REVIEW_HEADERS
    assert service.headers["要確認"] == EXISTING_REVIEW_HEADERS
    assert service.headers["Amazon注文"] == ["existing"]
    assert len(service.batch_updates) == 1
    assert len(service.header_updates) == 1


def test_matching_schema_is_no_op():
    service = Service({AMAZON_REVIEW_SHEET: AMAZON_REVIEW_HEADERS})

    result = install_amazon_review_schema(DB(service))

    assert result["schema_already_valid"] == 1
    assert result["sheet_created"] == result["header_written"] == 0
    assert service.batch_updates == []
    assert service.header_updates == []


def test_mismatched_schema_is_not_overwritten_or_cleared():
    service = Service({AMAZON_REVIEW_SHEET: ["wrong", "header"]})

    result = install_amazon_review_schema(DB(service))

    assert result["schema_mismatch"] == 1
    assert service.headers[AMAZON_REVIEW_SHEET] == ["wrong", "header"]
    assert service.batch_updates == []
    assert service.header_updates == []


def test_second_install_is_idempotent_no_op():
    service = Service()
    db = DB(service)

    first = install_amazon_review_schema(db)
    second = install_amazon_review_schema(db)

    assert first["sheet_created"] == first["header_written"] == 1
    assert second["schema_already_valid"] == 1
    assert second["sheet_created"] == second["header_written"] == 0
    assert len(service.batch_updates) == len(service.header_updates) == 1


def test_existing_review_schema_remains_unchanged():
    assert HEADERS["要確認"] == EXISTING_REVIEW_HEADERS
    assert "Amazon要確認" not in HEADERS


def test_cli_runs_only_dedicated_installer(monkeypatch, capsys):
    db = object()
    monkeypatch.setattr(cli, "make", lambda require_gemini: (None, db, None))
    monkeypatch.setattr(
        cli, "install_amazon_review_schema", lambda value: {"ok": value is db},
    )
    monkeypatch.setattr(sys, "argv", ["kakeibo", "amazon-review-schema-install"])

    cli.main()

    assert capsys.readouterr().out.strip() == "{'ok': True}"
