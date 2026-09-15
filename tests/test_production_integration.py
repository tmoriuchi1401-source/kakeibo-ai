"""Synthetic shared Google transports, real parent/CLIs/parsers/writers/postprocessing.

The bank stage deliberately exercises its existing empty-folder preview. OCR and
AI responses are fixtures; the privacy decision and receipt writer remain real.
No requests, credentials, original documents, or delete operations are supported.
"""
import base64
from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
from datetime import datetime
from email.message import EmailMessage
import io
import hashlib
import json
import re
import sys
from types import SimpleNamespace

import pytest

from app import cli, production_flow as flow, production_source
from app.aupay_card_recurring import SqliteRecurringRunState
from app.drive_run_state import StateBinding, DriveStateTransport, envelope, snapshot, validate
from app.models import ReceiptItem, ReceiptResult
from app.production_ledger import ProductionLedger, initial_ledger
from app.production_run import execute_serial
from app.settings import Settings
from app.sheets import HEADERS


NOW = datetime.fromisoformat("2026-09-14T12:00:00+09:00")
SID = "synthetic-sheet"
RECEIPTS, PAYPAY, BANK, ARCHIVE, STATE = (letter * 20 for letter in "RPBAS")


class Request:
    def __init__(self, call):
        self.call = call

    def execute(self, **kwargs):
        assert kwargs.get("num_retries", 0) == 0
        return deepcopy(self.call())


class Sheets:
    """Small A1 transport fixture; all production SheetsDB behavior is retained."""
    def __init__(self, rows, writes, *, readonly=False):
        self.rows, self.writes, self.readonly = rows, writes, readonly
        self.after_write = lambda method, kwargs: None

    def spreadsheets(self): return self
    def values(self): return self

    def area(self, value):
        title, cells = value.split("!")
        title = title.strip("'")
        start, _, end = cells.partition(":")
        def cell(text):
            match = re.fullmatch(r"([A-Z]*)([0-9]*)", text)
            assert match and any(match.groups()), text
            letters, digits = match.groups()
            col = 0
            for letter in letters:
                col = col * 26 + ord(letter) - 64
            return (int(digits) - 1 if digits else None, col - 1 if letters else None)
        r0, c0 = cell(start)
        r1, c1 = cell(end or start)
        return title, r0 or 0, c0 or 0, r1, c1

    def get(self, **kwargs):
        assert kwargs["spreadsheetId"] == SID
        if "range" not in kwargs:
            return Request(lambda: {"spreadsheetId": SID, "sheets": [
                {"properties": {"title": title, "sheetId": i,
                  "gridProperties": {"rowCount": 1000, "columnCount": 30}}}
                for i, title in enumerate(self.rows)]})
        def read():
            title, r0, c0, r1, c1 = self.area(kwargs["range"])
            rows = [list(row[c0:None if c1 is None else c1 + 1])
                    for row in self.rows[title][r0:None if r1 is None else r1 + 1]]
            for row in rows:
                while row and row[-1] == "": row.pop()
            while rows and not rows[-1]: rows.pop()
            return {"values": rows}
        return Request(read)

    def mutation(self, method, kwargs, action):
        def write():
            assert not self.readonly, "preview attempted Sheets write"
            assert kwargs["spreadsheetId"] == SID
            self.writes.append((method, deepcopy(kwargs)))
            result = action()
            self.after_write(method, kwargs)
            return result
        return Request(write)

    def put(self, rng, values):
        title, r0, c0, _, _ = self.area(rng)
        target = self.rows[title]
        for offset, row in enumerate(values):
            while len(target) <= r0 + offset: target.append([])
            dest = target[r0 + offset]
            while len(dest) < c0 + len(row): dest.append("")
            dest[c0:c0 + len(row)] = list(row)
        return {"updatedRows": len(values)}

    def update(self, **kwargs):
        return self.mutation("update", kwargs, lambda: self.put(kwargs["range"], kwargs["body"]["values"]))

    def append(self, **kwargs):
        def append():
            title = self.area(kwargs["range"])[0]
            values = kwargs["body"]["values"]
            self.rows[title].extend(deepcopy(values))
            return {"updates": {"updatedRows": len(values)}}
        return self.mutation("append", kwargs, append)

    def clear(self, **kwargs):
        def clear():
            title, r0, c0, r1, c1 = self.area(kwargs["range"])
            for row in self.rows[title][r0:None if r1 is None else r1 + 1]:
                for col in range(c0, len(row) if c1 is None else min(c1 + 1, len(row))): row[col] = ""
            return {}
        return self.mutation("clear", kwargs, clear)

    def batchUpdate(self, **kwargs):
        def batch():
            body = kwargs["body"]
            if "data" in body:
                for item in body["data"]: self.put(item["range"], item["values"])
            else:
                for item in body["requests"]:
                    if "addSheet" in item:
                        title = item["addSheet"]["properties"]["title"]
                        assert title not in self.rows
                        self.rows[title] = []
                    else:
                        assert set(item) <= {"repeatCell", "setDataValidation", "updateSheetProperties",
                                             "updateDimensionProperties", "setBasicFilter", "addBanding",
                                             "updateBanding", "deleteBanding", "clearBasicFilter"}, item
            return {}
        return self.mutation("batch", kwargs, batch)


class Drive:
    def __init__(self, metadata, payloads, writes, *, readonly=False):
        self.metadata, self.payloads, self.writes, self.readonly = metadata, payloads, writes, readonly

    def files(self): return self

    def get(self, **kwargs): return Request(lambda: self.metadata[kwargs["fileId"]])
    def get_media(self, **kwargs): return Request(lambda: self.payloads[kwargs["fileId"]])

    def list(self, **kwargs):
        folder = re.search(r"'([^']+)' in parents", kwargs["q"]).group(1)
        found = [file for file in self.metadata.values() if folder in file["parents"]]
        assert len(found) <= kwargs["pageSize"]
        return Request(lambda: {"files": found})

    def update(self, **kwargs):
        def update():
            assert not self.readonly, "preview attempted Drive write"
            file_id = kwargs["fileId"]
            self.writes.append(file_id)
            if "media_body" in kwargs:
                media = kwargs["media_body"]
                self.payloads[file_id] = media.getbytes(0, media.size())
            else:
                self.metadata[file_id].update(deepcopy(kwargs.get("body", {})))
                if "addParents" in kwargs:
                    self.metadata[file_id]["parents"] = [kwargs["addParents"]]
            return self.metadata[file_id]
        return Request(update)


def raw_mail(subject, body, identity):
    mail = EmailMessage()
    mail["Subject"], mail["Message-ID"] = subject, f"<{identity}@example.invalid>"
    mail["From"], mail["Date"] = "no-reply@amazon.co.jp", "Sun, 13 Sep 2026 10:00:00 +0900"
    mail.set_content(body)
    return base64.urlsafe_b64encode(mail.as_bytes()).decode().rstrip("=")


class Gmail:
    def __init__(self, source): self.source = source
    def users(self): return self
    def messages(self): return self
    def list(self, **kwargs): return Request(lambda: {"messages": [{"id": self.source}]})
    def get(self, **kwargs):
        if self.source == "amazon":
            value = {"raw": raw_mail("Amazon.co.jp ご注文の確認",
                "注文番号: 123-1234567-1234567\n注文日: 2026年9月13日\n注文合計: 1,500円\n支払い方法: Visa\n商品点数: 2", "amazon")}
        elif self.source == "aupay_card":
            value = {"raw": raw_mail("【ご利用詳細】au PAY カード",
                "▼カード情報\nau PAY カード\n本会員さま ご利用分\n\nNo.001--------\n▼ご利用日\n2026年9月13日\n▼ご利用金額\n1,200円\n▼ご利用先\n合成カード店舗\n", "card")}
        else:
            body = "au PAY ご利用のお知らせ\n■利用店舗\n合成残高店舗\n■種別\n支払\n■利用日時\n2026年9月13日(日) 12:34:56\n■支払金額\n1,234円\n■伝票番号\n123456789012\n"
            value = {"payload": {"mimeType": "text/plain", "body": {"data": base64.urlsafe_b64encode(body.encode()).decode()}}}
        return Request(lambda: value)


@pytest.fixture
def integrated(monkeypatch, tmp_path):
    import app.google_clients as google
    from app.receipt_text_extraction import _ReceiptTextExtraction
    from app.bank_pdf_pipeline import SOURCE, DOCOMO_SMTB_SOURCE, CHIBA_BANK_SOURCE
    rows = {title: [list(header)] for title, header in HEADERS.items()}
    rows["カテゴリ"] += [["食費", "食品"], ["その他", "未分類"]]
    swrites, dwrites, metadata, payloads, calls, failures, ai_calls = [], [], {}, {}, [], [], []
    sheets, ro_sheets = Sheets(rows, swrites), Sheets(rows, swrites, readonly=True)
    drive, ro_drive = Drive(metadata, payloads, dwrites), Drive(metadata, payloads, dwrites, readonly=True)

    def add_file(identity, folder, mime, payload, name="fixture"):
        metadata[identity] = {"id": identity, "name": name, "mimeType": mime,
                              "parents": [folder], "capabilities": {"canEdit": True}}
        payloads[identity] = payload
    add_file("receipt-fixture", RECEIPTS, "image/png", b"synthetic-normal-receipt")
    add_file("private-fixture", RECEIPTS, "image/png", b"synthetic-payroll")
    csv = ("取引日,出金金額（円）,入金金額（円）,海外出金金額,通貨,変換レート（円）,利用国,取引内容,取引先,取引方法,支払い区分,利用者,取引番号\n"
           "2026/09/13 12:34,900,,,,,,支払い,合成PayPay店舗,PayPay残高,一回払い,本人,TX-SYNTHETIC\n")
    add_file("paypay-fixture", PAYPAY, "text/csv", csv.encode("utf-8-sig"), "fixture.csv")
    env = {"SPREADSHEET_ID": SID, "KAKEIBO_STATE_FOLDER_ID": STATE}
    bindings = {}
    for source, native in flow.STATE_SOURCES.items():
        identity = f"{source}-state"
        env[flow.STATE_ID_ENV[source]] = identity
        binding = StateBinding(native, SID, STATE, identity)
        bindings[source] = binding
        original = tmp_path / f"initial-{source}"
        SqliteRecurringRunState(original / binding.checkpoint_name, repo_root=flow.REPO)
        add_file(identity, STATE, "application/json", envelope(binding, snapshot(original, binding)))
    ledger_binding = StateBinding("production_run", SID, STATE, "ledger")
    add_file("ledger", STATE, "application/json", initial_ledger(ledger_binding))
    env["AUPAY_CARD_RECURRING_AUTHORITY_JSON"] = json.dumps({
        "policy_id": "synthetic-card", "source": "au_pay_card_gmail",
        "expected_spreadsheet_id": SID, "expected_worksheet": "取込データ",
        "allowed_statuses": ["auto_expense", "matched_receipt", "transfer_aupay_charge", "matched_amazon"],
        "max_batch_size": 10, "max_messages": 10, "overlap_seconds": 7200,
        "max_window_seconds": 7 * 86400, "initial_start": "2026-09-13T00:00:00+09:00",
        "valid_from": "2026-09-01T00:00:00+09:00", "expires_at": "2026-09-20T00:00:00+09:00"})
    env["AUPAY_CARD_AUDIT_KEY_JSON"] = json.dumps({"key_id": "synthetic", "key_b64": base64.b64encode(b"k" * 32).decode()})
    env["BANK_PDF_RECURRING_AUTHORITY_JSON"] = json.dumps({
        "schema_version": 1, "policy_id": "synthetic-bank", "source": "bank_pdf_drive",
        "expected_spreadsheet_id": SID, "expected_drive_folder_id": BANK, "expected_worksheet": "取込データ",
        "target_binding_version": 1, "canonical_schema_version": 1,
        "supported_bank_sources": [SOURCE, DOCOMO_SMTB_SOURCE, CHIBA_BANK_SOURCE],
        "allowed_classifications": ["expense"], "max_files": 10, "max_rows": 100,
        "overlap_seconds": 3600, "max_window_seconds": 7 * 86400,
        "initial_start": "2026-09-13T00:00:00+09:00", "valid_from": "2026-09-13T00:00:00+09:00",
        "expires_at": "2026-09-20T00:00:00+09:00", "expected_branch": "main"})
    settings = Settings(spreadsheet_id=SID, gmail_token_json="synthetic", gemini_api_key="synthetic",
                        receipt_drive_folder_id=RECEIPTS, paypay_drive_folder_id=PAYPAY,
                        bank_pdf_drive_folder_id=BANK, processed_drive_folder_id=ARCHIVE,
                        medical_review_shadow_enabled=False)
    for module in (cli, production_source):
        monkeypatch.setattr(module, "Settings", lambda: settings)
    for name in ("app.sheets.sheets_service",): monkeypatch.setattr(name, lambda: sheets)
    for module in (google, cli, production_source):
        monkeypatch.setattr(module, "read_only_sheets_service", lambda: ro_sheets)
        if hasattr(module, "drive_service"): monkeypatch.setattr(module, "drive_service", lambda: drive)
        if hasattr(module, "read_only_drive_service"): monkeypatch.setattr(module, "read_only_drive_service", lambda: ro_drive)
    for module in ("app.drive_receipts", "app.drive_paypay"):
        monkeypatch.setattr(module + ".drive_service", lambda: drive)
    for module in ("app.drive_receipts", "app.drive_paypay", "app.production_source"):
        monkeypatch.setattr(module + ".download_drive_file", lambda file_id, **kw: payloads[file_id])
    monkeypatch.setattr("app.aupay_mail_pipeline.gmail_service", lambda _: Gmail("aupay_balance"))
    def extraction(data, mime):
        text = "給与明細\n支給額\n基本給\n控除額" if data == b"synthetic-payroll" else "レシート\n合成食品店\nパン 500円\n合計 500円\n消費税 45円\nお預り 500円\nお釣り 0円"
        return _ReceiptTextExtraction("extracted", "image_ocr", text)
    monkeypatch.setattr("app.receipt_privacy_gate._extract_receipt_text", extraction)
    class AI:
        def analyze_receipt(self, data, mime, categories, **kwargs):
            assert data == b"synthetic-normal-receipt"
            ai_calls.append(data)
            return ReceiptResult(merchant="合成食品店", date="2026-09-13", total=500, payment_method="現金",
                items=[ReceiptItem(name="パン", amount=500, major_category="食費", minor_category="食品")])
    monkeypatch.setattr(cli, "GeminiAI", lambda *args: AI())
    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None): return NOW.astimezone(tz)
    monkeypatch.setattr(cli, "datetime", FixedDateTime)
    monkeypatch.setattr("app.bank_pdf_recurring.subprocess.check_output", lambda *args, **kw: "a" * 40)

    def child(args, **kwargs):
        module, argv = args[2], args[3:]
        source = next(name for name in flow.DEPENDENCIES for mode in (True, False)
                      if args[:len(flow.command(name, apply=mode))] == flow.command(name, apply=mode))
        calls.append(source)
        output, error = io.StringIO(), io.StringIO()
        code = 0
        with monkeypatch.context() as child_patch, redirect_stdout(output), redirect_stderr(error):
            child_patch.setattr(sys, "argv", [module] + argv)
            child_patch.setattr(cli, "gmail_readonly_service", lambda _: Gmail(source))
            for key, value in kwargs["env"].items(): child_patch.setenv(key, value)
            try:
                (cli if module == "app.cli" else production_source).main()
            except SystemExit as exc:
                code = exc.code or 0
            except Exception as exc:
                failures.append((source, type(exc).__name__, str(exc)))
                code = 1
        if code: failures.append((source, output.getvalue()))
        return SimpleNamespace(returncode=code, stdout=output.getvalue(), stderr=error.getvalue())
    monkeypatch.setattr(flow.subprocess, "run", child)

    def run(apply, *, canary=False, target=""):
        directory = tmp_path / f"run-{len(calls)}"; directory.mkdir()
        ledger = ProductionLedger(DriveStateTransport(drive if apply else ro_drive, ledger_binding), ledger_binding)
        runners = flow.assemble(env, directory, apply=apply, bank_apply=False, ledger=ledger, canary_target=target)
        return execute_serial(runners, history=ledger.value["sources"], preview=not apply, amazon_canary=canary)
    return SimpleNamespace(run=run, rows=rows, payloads=payloads, metadata=metadata,
                           swrites=swrites, dwrites=dwrites, calls=calls, failures=failures,
                           ai_calls=ai_calls, bindings=bindings, sheets=sheets)


def test_shared_transport_cli_preview_apply_and_replay(integrated):
    f = integrated
    original = deepcopy(f.payloads)
    preview = f.run(False)
    assert preview["success"], (preview, f.failures)
    assert f.swrites == f.dwrites == f.ai_calls == []
    assert f.payloads == original
    assert preview["sources"]["receipts"]["counts"]["new_eligible"] == 1
    assert preview["sources"]["receipts"]["counts"]["needs_review"] == 1

    result = f.run(True)
    assert result["success"], (result, f.failures)
    assert f.calls == list(flow.DEPENDENCIES) * 2
    assert result["sources"]["amazon"]["counts"]["written_purchases"] == 1
    assert result["sources"]["receipts"]["counts"]["written"] == 1
    assert result["sources"]["aupay_card"]["counts"]["written"] == 1
    assert result["sources"]["aupay_balance"]["counts"]["new"] == 1
    assert result["sources"]["paypay"]["counts"]["imported_files"] == 1
    assert result["sources"]["bank"]["counts"]["files_seen"] == 0
    assert f.payloads["bank-state"] == original["bank-state"]
    assert len(f.rows["取込データ"]) == 6  # header + five distinct source imports
    assert len(f.rows["支出明細"]) == len(f.rows["支出一覧"]) == 5, f.rows["取込データ"]
    # Canonical card imports retain the legacy accounting boundary, even in the
    # new parent. Enabling orchestration does not authorize backlog repair.
    card = next(row for row in f.rows["取込データ"][1:] if row[2] == "au PAYカード")
    assert card[8:10] == ["auto_expense", ""]
    assert not any(row[10] == card[0] for row in f.rows["支出明細"][1:])
    assert f.metadata["receipt-fixture"]["appProperties"]["kakeiboReceiptClass"] == "normal"
    assert f.metadata["private-fixture"]["parents"] == [RECEIPTS]
    assert f.ai_calls == [b"synthetic-normal-receipt"]
    for source in ("amazon", "aupay_card"):
        assert validate(f.payloads[f"{source}-state"], f.bindings[source])["phase"] == "ready"
    identities = {title: [row[0] for row in f.rows[title][1:]]
                  for title in ("取込データ", "支出明細", "Amazonイベント", "Amazon注文ヘッダ", "レシート")}
    def accounting_appends():
        # Display/review rebuilds deliberately clear and refill their own views.
        return sum(method == "append" and kwargs["range"].split("!")[0] in identities
                   for method, kwargs in f.swrites)
    append_count = accounting_appends()
    replay = f.run(True)
    assert replay["success"], (replay, f.failures)
    assert append_count == accounting_appends()
    assert identities == {title: [row[0] for row in f.rows[title][1:]] for title in identities}
    assert len(f.ai_calls) == 1
    rendered = json.dumps(replay)
    assert "合成" not in rendered and "TX-SYNTHETIC" not in rendered and "123-1234567" not in rendered


def test_real_source_state_corruption_stops_dependents_and_keeps_independent_imports(integrated):
    f = integrated
    f.payloads["amazon-state"] = b"synthetic-corruption"
    result = f.run(True)
    assert not result["success"]
    assert f.payloads["amazon-state"] == b"synthetic-corruption"
    assert "amazon" not in f.calls
    for source in ("receipts", "aupay_balance", "paypay"):
        assert result["sources"][source]["status"] == "success"
    for source in ("aupay_card", "bank", "review_apply", "reconcile", "auto_expense", "review_refresh", "expenses_refresh"):
        assert result["sources"][source]["status"] == "skipped"
    assert len(f.rows["取込データ"]) == 4  # three independent sources + header
    assert json.loads(f.payloads["ledger"])["sources"]["amazon"]["phase"] == "pending"


def test_isolated_amazon_canary_writes_four_rows_then_readonly_replay(integrated):
    f = integrated
    original = deepcopy(f.payloads)
    target = "amazon-order:" + hashlib.sha256(b"123-1234567-1234567").hexdigest()[:16]
    result = f.run(True, canary=True, target=target)
    assert result["success"], (result, f.failures)
    assert f.calls == ["amazon"] and set(result["sources"]) == {"amazon"}
    counts = result["sources"]["amazon"]["counts"]
    for key in ("written_purchases", "event_rows_written", "header_rows_written", "import_rows_written", "expense_rows_written"):
        assert counts[key] == 1
    assert counts["write_requests"] == 4
    assert len(f.swrites) == 4 and f.ai_calls == []
    for identity in ("aupay_card-state", "bank-state", "receipt-fixture", "private-fixture", "paypay-fixture"):
        assert original[identity] == f.payloads[identity]
    ledger = json.loads(f.payloads["ledger"])
    assert all(item["last_success"] is None for source, item in ledger["sources"].items() if source != "amazon")
    remote, writes = deepcopy(f.payloads), (len(f.swrites), len(f.dwrites))
    preview = f.run(False, canary=True)
    assert preview["success"]
    assert preview["sources"]["amazon"]["counts"]["eligible_purchases"] == 0
    assert preview["sources"]["amazon"]["counts"]["new_event_rows"] == 0
    assert f.payloads == remote and writes == (len(f.swrites), len(f.dwrites))


def test_canary_target_drift_has_no_accounting_writes(integrated):
    f = integrated
    result = f.run(True, canary=True, target="amazon-order:" + "0" * 16)
    assert not result["success"]
    assert f.swrites == [] and f.calls == ["amazon"]
    assert validate(f.payloads["amazon-state"], f.bindings["amazon"])["phase"] == "pending"


def test_real_receipt_committed_response_loss_blocks_automatic_replay(integrated):
    f = integrated
    def lose_response(method, kwargs):
        if (method == "append" and kwargs["range"] == "取込データ!A:A"
                and kwargs["body"]["values"][0][0] == "receipt:receipt-fixture"):
            raise RuntimeError("synthetic response lost after commit")
    f.sheets.after_write = lose_response
    first = f.run(True)
    assert not first["success"]
    assert first["sources"]["receipts"]["status"] == "failed"
    assert first["sources"]["auto_expense"]["status"] == "skipped"
    assert len([row for row in f.rows["取込データ"] if row[0] == "receipt:receipt-fixture"]) == 1
    assert f.metadata["receipt-fixture"]["parents"] == [RECEIPTS]
    second = f.run(True)
    assert not second["success"]
    assert f.calls.count("receipts") == 1  # pending is checked before its CLI
    assert len(f.ai_calls) == 1
    assert len([row for row in f.rows["取込データ"] if row[0] == "receipt:receipt-fixture"]) == 1
