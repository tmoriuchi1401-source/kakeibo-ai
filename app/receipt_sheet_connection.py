"""Compose #200's existing reader, decoder, journal and validation worker.

No installer, credential creation, workflow dispatch or financial writer. The
caller supplies the existing private adapter and authenticated read ports. A
live caller must separately approve the exact workbook/cache range and Drive
resources. This first connection deliberately accepts one general receipt.
"""
from copy import deepcopy

from .category_rules import parse_rules
from .drive_run_state import StateError
from .receipt_private_adapter import _expected
from .receipt_sheet_decoder import decode_receipt_row
from .receipt_sheet_validation import (
    FINAL, PURPOSE, IndependentReceiptReader, ValidationJournal, cached_result,
    execute_accepted_request, project_result, request_uuid, result_cache_entry,
)
from .reconciliation import parse_import_rows

CACHE_RANGE = "'_receipt_validation_results'!A2:M1000"
CACHE_HEADERS = ["key", "request_id", "case_id", "source_id", "source_version",
                 "source_sha256", "snapshot_digest", "state", "posting_authority",
                 "financial_write_count", "mime_type", "classification", "transaction_kind"]


def connect_existing_google_services(*, private, drive, sheets, spreadsheet_id, source_folder):
    """Bind existing authenticated services; never create credentials/resources.

    Drive v3 is used only for independent metadata and original-byte reads;
    the supplied #200 private adapter retains its existing conditional IO.
    Construction performs no network IO. A host/Actions entry is still needed.
    """
    fields = ("id,trashed,version,mimeType,parents,owners(emailAddress),"
              "permissions(type,role,emailAddress,deleted)")
    def metadata(fid):
        return drive.files().get(fileId=fid, fields=fields).execute(num_retries=0)
    def source_bytes(fid):
        return drive.files().get_media(fileId=fid).execute(num_retries=0)
    return ReceiptSheetConnection(private=private,
        projection=GoogleReceiptProjection(sheets, spreadsheet_id=spreadsheet_id, cache_range=CACHE_RANGE),
        metadata=metadata, source_bytes=source_bytes, spreadsheet_id=spreadsheet_id,
        private_id=private.fid, source_folder=source_folder,
        owner_email=private.owner, service_email=private.service)


def cache_row(entry):
    source = entry["source"]
    return [entry["key"], entry["request_id"], entry["case_id"], source["source_id"],
            source["version"], source["sha256"], entry["snapshot_digest"], entry["state"],
            entry["posting_authority"], entry["financial_write_count"], source["mime_type"],
            source["classification"], source["transaction_kind"]]


def result_formula(row):
    """Formula for the existing G result cell, installed only after approval.

    Local row references only render data; they never address a request. The
    event UUID, case and complete source identity must agree. Duplicate cache
    records with identical contents are harmless; conflicts display UNKNOWN.
    The message always distinguishes the submitted snapshot from later edits.
    """
    if type(row) is not int or row < 2:
        raise StateError("receipt_connection_formula_row_invalid")
    cache = "'_receipt_validation_results'!"
    rid = f'REGEXEXTRACT(TO_TEXT($I{row}),"^(?:ready|register):([a-f0-9-]{{36}})$")'
    conditions = [f'{cache}B2:B1000={rid}', f'{cache}C2:C1000=$A{row}',
                  f'{cache}D2:D1000=$AL{row}', f'{cache}E2:E1000=TO_TEXT($AM{row})',
                  f'{cache}F2:F1000=$AN{row}']
    filtered = f'UNIQUE(FILTER({cache}A2:M1000,{",".join(conditions)}))'
    return (f'=IF(OR($A{row}<>$AK{row},$A{row}=""),"結果不明・照合待ち",'
            f'IFERROR(LET(records,{filtered},state,INDEX(records,1,8),'
            'IF(ROWS(records)<>1,"結果不明・照合待ち",'
            'IF(state="検証済み・未記帳","登録検証済み・未記帳",state)&"（登録時の内容）")), '
            f'IF(OR($I{row}="",LEFT(TO_TEXT($I{row}),6)="ready:"),"編集中・未登録",'
            '"受付確認中・未記帳／チェックを繰り返さない")))')


class ReceiptSheetConnection:
    def __init__(self, *, private, projection, metadata, source_bytes,
                 spreadsheet_id, private_id, source_folder, owner_email, service_email):
        if (private.fid != private_id or private.owner != owner_email
                or private.service != service_email or projection.spreadsheet_id != spreadsheet_id):
            raise StateError("receipt_connection_resource_binding_mismatch")
        self.private, self.projection = private, projection
        self.journal = ValidationJournal(private, preflight=lambda: None)
        self.reader = IndependentReceiptReader(
            metadata=metadata, source_bytes=source_bytes, sheet_values=self.sheet_values,
            private_cases=private.private_cases, spreadsheet_id=spreadsheet_id,
            private_id=private_id, source_folder=source_folder,
            owner_email=owner_email, service_email=service_email,
            parse_imports=parse_import_rows, parse_rules=parse_rules)

    def sheet_values(self):
        """Independently decode raw native cells using the pinned private catalog.

        No import, policy, source classification or confirmation is learned from
        editable cells. Row coordinates are discarded after finding the case.
        """
        catalog, _ = self.private.read_catalog()
        if len(catalog["cases"]) != 1:
            raise StateError("receipt_connection_single_case_required")
        case_id, entry = next(iter(catalog["cases"].items()))
        expected = _expected(case_id, entry)
        rows = self.projection.read_rows()
        matches = [r for r in rows if r and r[0] == case_id]
        if len(matches) != 1:
            raise StateError("receipt_connection_case_missing_or_duplicate")
        candidate = decode_receipt_row(matches[0], expected=expected)
        return dict(sheet="確認", cases=[candidate],
            imports=[deepcopy(entry["import_row"])],
            candidates=list(enumerate(deepcopy(entry["candidate_rows"]), 2)),
            categories=deepcopy(entry["categories"]), rules=deepcopy(entry["rules"]))

    def prepare(self, case_id, request_id):
        """Freeze input before exposing #200's UUID checkbox; no dispatch.

        Used by an explicitly invoked preparer, never by result refresh. The
        Apps Script entry still consumes the same private prepared ticket.
        """
        request_uuid(request_id)
        _, tag = self.journal.load(writable=True)
        snapshot = self.reader(case_id)["request_snapshot"]
        self.journal.prepare(dict(request_id=request_id, purpose=PURPOSE,
                                  snapshot=snapshot), expected_tag=tag)
        return dict(request_id=request_id, snapshot=deepcopy(snapshot),
                    checkedValue="register:" + request_id,
                    uncheckedValue="ready:" + request_id)

    def run(self, request_id):
        """UUID-only worker; existing journal claims validation once.

        Cache failure does not re-execute validation or change its terminal
        record. An ambiguous worker outcome is never automatically retried.
        """
        record = execute_accepted_request(request_id, reader=self.reader, journal=self.journal)
        pending = True
        if record["state"] in FINAL:
            try:
                self.projection.append_result(result_cache_entry(record))
                cached_result(self.projection.read_results(), record)
                pending = False
            except Exception:
                pass
        return dict(record=record, display_pending=pending)

    def result(self, request_id, rows):
        """GET-only reconciliation/rendering; preserves draft, links and notes."""
        record = self.journal.query(request_id)
        if record["state"] in FINAL:
            cached_result(self.projection.read_results(), record)
        return project_result(rows, record)


class GoogleReceiptProjection:
    """Small Sheets API port using the caller's existing authenticated service.

    Only the preapproved cache range is appendable; no case-row updates, tab
    creation, sharing or writer construction. Writes have zero client retries.
    Its self-keyed cache is a projection, never an acceptance journal.
    """
    def __init__(self, service, *, spreadsheet_id, cache_range):
        if cache_range != CACHE_RANGE:
            raise StateError("receipt_connection_cache_range_invalid")
        self.service, self.spreadsheet_id, self.cache_range = service, spreadsheet_id, cache_range

    def _values(self):
        return self.service.spreadsheets().values()

    def read_rows(self):
        rows = self._values().get(spreadsheetId=self.spreadsheet_id, range="'確認'!A2:AT1000",
            valueRenderOption="UNFORMATTED_VALUE").execute(num_retries=0).get("values", [])
        return [r + [None] * (46 - len(r)) for r in rows]

    def append_result(self, entry):
        self._values().append(spreadsheetId=self.spreadsheet_id, range=self.cache_range,
            valueInputOption="RAW", insertDataOption="INSERT_ROWS",
            body={"values": [cache_row(entry)]}).execute(num_retries=0)

    def read_results(self):
        rows = self._values().get(spreadsheetId=self.spreadsheet_id, range=self.cache_range,
            valueRenderOption="UNFORMATTED_VALUE").execute(num_retries=0).get("values", [])
        entries = []
        for r in rows:
            if not r:
                continue
            if len(r) != len(CACHE_HEADERS):
                raise StateError("receipt_connection_cache_invalid")
            entries.append(dict(key=r[0], request_id=r[1], case_id=r[2],
                source=dict(source_id=r[3], version=r[4], sha256=r[5], mime_type=r[10],
                            classification=r[11], transaction_kind=r[12]),
                snapshot_digest=r[6], state=r[7], posting_authority=r[8], financial_write_count=r[9]))
        return entries
