"""Small, typed links to material needed for a human review decision.

The role describes why the material is shown.  Only stable Drive IDs and exact
rows in the current workbook are linkable; a guessed match is never evidence.
"""
from __future__ import annotations

from dataclasses import dataclass
import re

from googleapiclient.errors import HttpError

from .reconciliation import ImportTransaction


DRIVE_ID = re.compile(r"[A-Za-z0-9_-]{10,}\Z")
SHEET_RANGE = re.compile(r"[A-Z]+[1-9][0-9]*(?::[A-Z]+[1-9][0-9]*)?\Z")
ROLES = frozenset({"target", "comparison", "supporting"})


@dataclass(frozen=True)
class ReviewResource:
    label: str
    role: str
    resource_type: str
    file_id: str = ""
    spreadsheet_id: str = ""
    sheet_id: int | None = None
    cell_range: str = ""

    @classmethod
    def drive_file(cls, *, label: str, role: str, file_id: str) -> ReviewResource:
        return cls(label, role, "drive_file", file_id=file_id)

    @classmethod
    def sheet_row(cls, *, label: str, role: str, spreadsheet_id: str,
                  sheet_id: int, row: int, last_column: str = "A") -> ReviewResource:
        return cls(label, role, "sheet_range", spreadsheet_id=spreadsheet_id,
                   sheet_id=sheet_id, cell_range=f"A{row}:{last_column}{row}")

    def __post_init__(self) -> None:
        if self.role not in ROLES or not self.label or '"' in self.label or "\n" in self.label:
            raise ValueError("review_resource_invalid_label_or_role")
        if self.resource_type == "drive_file":
            if not DRIVE_ID.fullmatch(self.file_id) or self.spreadsheet_id or self.sheet_id is not None or self.cell_range:
                raise ValueError("review_resource_invalid_drive_file")
        elif self.resource_type == "sheet_range":
            if (not DRIVE_ID.fullmatch(self.spreadsheet_id) or type(self.sheet_id) is not int
                    or self.sheet_id < 0 or not SHEET_RANGE.fullmatch(self.cell_range) or self.file_id):
                raise ValueError("review_resource_invalid_sheet_range")
        else:
            raise ValueError("review_resource_invalid_type")

    @property
    def url(self) -> str:
        if self.resource_type == "drive_file":
            return f"https://drive.google.com/file/d/{self.file_id}/view"
        return (f"https://docs.google.com/spreadsheets/d/{self.spreadsheet_id}/edit"
                f"#gid={self.sheet_id}&range={self.cell_range}")

    def formula(self) -> str:
        return f'=HYPERLINK("{self.url}","{self.label}")'


def receipt_original_link(tx: ImportTransaction, drive=None) -> str:
    """Link the stored source file after checking its current Drive identity."""
    if tx.source != "receipt":
        return "対象外"
    source_id = str(tx.row[3]).strip()
    if not DRIVE_ID.fullmatch(source_id) or tx.import_id != "receipt:" + source_id:
        return "原本リンクなし"
    try:
        if drive is None:
            from .google_clients import drive_service
            drive = drive_service()
        file = drive.files().get(fileId=source_id,
            fields="id,mimeType,trashed",supportsAllDrives=True).execute()
    except HttpError as error:
        status = int(getattr(error.resp, "status", 0) or 0)
        return "原本リンクなし" if status in (403, 404) else "原本確認不可"
    except Exception:
        return "原本確認不可"
    mime = str(file.get("mimeType", ""))
    if (file.get("id") != source_id or file.get("trashed")
            or not (mime.startswith("image/") or mime == "application/pdf")):
        return "原本リンクなし"
    return ReviewResource.drive_file(label="原本を開く", role="target", file_id=source_id).formula()


def recorded_candidate_ids(tx: ImportTransaction) -> tuple[str, ...]:
    """Read reconciliation's explicit candidate identities, never fuzzy matches."""
    if tx.status != "needs_review_duplicate":
        return ()
    notes = re.findall(r"(?:^|;\s*)候補=([^;]+)", tx.note)
    if not notes:
        return ()
    return tuple(dict.fromkeys(value.strip() for value in notes[-1].split(",")
                               if value.strip()))


def recorded_detail_total_link(tx: ImportTransaction, *, spreadsheet_id: str,
                               sheet_id: int | None,
                               unique_import_id: bool = True) -> str:
    """Link the exact import note cell containing a recorded item/receipt total mismatch.

    A failed extraction has no posted expense items. Its calculated item total
    is retained in the import note, so that cell is the comparison authority.
    The import row identity and recorded receipt total must agree before linking.
    """
    if "明細合計" not in tx.note:
        return ""
    matches = re.findall(
        r"(?:^|;\s*)明細合計([0-9]{1,12})≠レシート合計([0-9]{1,12})(?=;|$)",
        tx.note)
    if (len(matches) != 1 or int(matches[0][1]) != tx.amount
            or not unique_import_id
            or tx.row_num < 2 or not tx.row or str(tx.row[0]) != tx.import_id
            or len(tx.row) <= 8 or str(tx.row[2]) != tx.source
            or str(tx.row[8]) != tx.status
            or len(tx.row) <= 11 or str(tx.row[11]) != tx.note
            or (tx.source == "receipt" and
                tx.import_id != "receipt:" + str(tx.row[3]).strip())
            or not DRIVE_ID.fullmatch(spreadsheet_id)
            or type(sheet_id) is not int or sheet_id < 0):
        return "明細合計リンクなし"
    return ReviewResource(
        label="明細合計を見る", role="comparison", resource_type="sheet_range",
        spreadsheet_id=spreadsheet_id, sheet_id=sheet_id,
        cell_range=f"L{tx.row_num}",
    ).formula()
