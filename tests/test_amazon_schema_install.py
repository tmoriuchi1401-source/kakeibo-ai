import sys

from app import cli
from app.amazon_schema_install import AMAZON_SCHEMA_SHEETS, install_amazon_schema
from app.sheets import HEADERS


class Request:
    def __init__(self, callback=None, result=None):
        self.callback = callback
        self.result = result or {}

    def execute(self):
        if self.callback:
            self.callback()
        return self.result


class ValuesAPI:
    def __init__(self, service):
        self.service = service

    def get(self, spreadsheetId, range):
        self.service.reads.append(range)
        title = range.split("!", 1)[0]
        header = self.service.sheets.get(title, {}).get("header", [])
        return Request(result={"values": [header] if header else []})

    def update(self, **kwargs):
        def apply():
            title = kwargs["range"].split("!", 1)[0]
            self.service.sheets[title]["header"] = list(kwargs["body"]["values"][0])
            self.service.header_updates.append(kwargs)

        return Request(callback=apply)

    def clear(self, **kwargs):
        raise AssertionError("schema install must not clear data")

    def append(self, **kwargs):
        raise AssertionError("schema install must not append data")


class SpreadsheetsAPI:
    def __init__(self, service):
        self.service = service
        self.values_api = ValuesAPI(service)

    def get(self, spreadsheetId):
        return Request(result={"sheets": [
            {"properties": {"title": title}}
            for title in self.service.sheets
        ]})

    def batchUpdate(self, **kwargs):
        def apply():
            self.service.batch_updates.append(kwargs)
            for request in kwargs["body"]["requests"]:
                properties = request["addSheet"]["properties"]
                self.service.sheets[properties["title"]] = {
                    "header": [], "rows": [], "properties": properties,
                }

        return Request(callback=apply)

    def values(self):
        return self.values_api


class Service:
    def __init__(self, sheets=None):
        self.sheets = {
            title: {"header": list(header), "rows": list(rows), "properties": {}}
            for title, (header, rows) in (sheets or {}).items()
        }
        self.reads = []
        self.batch_updates = []
        self.header_updates = []
        self.api = SpreadsheetsAPI(self)

    def spreadsheets(self):
        return self.api


class DB:
    sid = "sheet-id"

    def __init__(self, service):
        self.svc = service

    def sheet_titles(self):
        return list(self.svc.sheets)

    def get(self, range):
        return self.svc.spreadsheets().values().get(
            spreadsheetId=self.sid, range=range,
        ).execute().get("values", [])


def _entry(summary, key):
    return summary[key]["count"], summary[key]["sheets"]


def test_installs_both_missing_sheets_with_existing_headers_and_only_target_writes():
    service = Service({"Amazon注文": (["existing"], [["keep-me"]])})

    summary = install_amazon_schema(DB(service))

    assert _entry(summary, "created_sheets") == (2, list(AMAZON_SCHEMA_SHEETS))
    assert _entry(summary, "initialized_headers") == (2, list(AMAZON_SCHEMA_SHEETS))
    assert service.sheets["Amazon注文"]["rows"] == [["keep-me"]]
    assert {update["range"] for update in service.header_updates} == {
        "Amazonイベント!A1", "Amazon注文ヘッダ!A1",
    }
    for title in AMAZON_SCHEMA_SHEETS:
        assert service.sheets[title]["header"] is HEADERS[title] or \
            service.sheets[title]["header"] == HEADERS[title]
        assert service.sheets[title]["properties"]["gridProperties"] == {
            "frozenRowCount": 1,
        }


def test_installs_only_missing_sheet_and_preserves_ready_sheet():
    service = Service({"Amazonイベント": (HEADERS["Amazonイベント"], [])})

    summary = install_amazon_schema(DB(service))

    assert _entry(summary, "created_sheets") == (1, ["Amazon注文ヘッダ"])
    assert _entry(summary, "initialized_headers") == (1, ["Amazon注文ヘッダ"])
    assert _entry(summary, "already_ready") == (1, ["Amazonイベント"])


def test_ready_sheets_and_second_run_make_no_writes():
    service = Service({title: (HEADERS[title], []) for title in AMAZON_SCHEMA_SHEETS})
    db = DB(service)

    first = install_amazon_schema(db)
    second = install_amazon_schema(db)

    assert _entry(first, "already_ready") == (2, list(AMAZON_SCHEMA_SHEETS))
    assert second == first
    assert service.batch_updates == []
    assert service.header_updates == []


def test_conflicting_header_is_reported_without_overwrite_or_data_change():
    rows = [["existing-data"]]
    service = Service({
        "Amazonイベント": (["wrong"], rows),
        "Amazon注文ヘッダ": (HEADERS["Amazon注文ヘッダ"], []),
        "要確認": (["existing"], [["untouched"]]),
    })

    summary = install_amazon_schema(DB(service))

    assert _entry(summary, "conflicts") == (1, ["Amazonイベント"])
    assert service.sheets["Amazonイベント"]["header"] == ["wrong"]
    assert service.sheets["Amazonイベント"]["rows"] == rows
    assert service.sheets["要確認"]["rows"] == [["untouched"]]
    assert service.batch_updates == []
    assert service.header_updates == []
    assert set(service.reads) == {
        "Amazonイベント!1:1", "Amazon注文ヘッダ!1:1",
    }


def test_empty_existing_header_is_initialized_without_touching_data_rows():
    rows = [["existing-data"]]
    service = Service({
        "Amazonイベント": ([], rows),
        "Amazon注文ヘッダ": (HEADERS["Amazon注文ヘッダ"], []),
    })

    summary = install_amazon_schema(DB(service))

    assert _entry(summary, "initialized_headers") == (1, ["Amazonイベント"])
    assert service.sheets["Amazonイベント"]["rows"] == rows


def test_cli_runs_dedicated_installer(monkeypatch, capsys):
    db = object()
    monkeypatch.setattr(cli, "make", lambda require_gemini: (None, db, None))
    monkeypatch.setattr(cli, "install_amazon_schema", lambda value: {"ok": value is db})
    monkeypatch.setattr(sys, "argv", ["kakeibo", "amazon-schema-install"])

    cli.main()

    assert capsys.readouterr().out.strip() == "{'ok': True}"
