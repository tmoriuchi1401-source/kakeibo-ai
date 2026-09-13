"""Private, repository-external authority manifest for four loan repayments."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
import os
from pathlib import Path
import re

from .bank_canary import BANK_LOAN_ROWS, CANARY_TARGET_SHEET, LOAN_EXPENSE_CATEGORY


LOAN_MANIFEST_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class BankLoanExactManifest:
    created_at: datetime
    source_identities: tuple[str, ...]
    target_spreadsheet_id: str
    target_worksheet: str = CANARY_TARGET_SHEET
    classification: str = "loan_repayment"
    projected_classification: str = "expense"
    category_major: str = LOAN_EXPENSE_CATEGORY[0]
    category_minor: str = LOAN_EXPENSE_CATEGORY[1]
    min_rows: int = BANK_LOAN_ROWS
    max_rows: int = BANK_LOAN_ROWS
    schema_version: int = LOAN_MANIFEST_SCHEMA_VERSION

    def validate(self) -> None:
        if self.schema_version != LOAN_MANIFEST_SCHEMA_VERSION:
            raise RuntimeError("bank_loan_manifest_schema_invalid")
        if self.created_at.tzinfo is None or self.created_at.utcoffset() is None:
            raise RuntimeError("bank_loan_manifest_timezone_required")
        if self.min_rows != BANK_LOAN_ROWS or self.max_rows != BANK_LOAN_ROWS:
            raise RuntimeError("bank_loan_manifest_requires_exactly_four_rows")
        if len(self.source_identities) != BANK_LOAN_ROWS:
            raise RuntimeError("bank_loan_manifest_requires_exactly_four_identities")
        if len(set(self.source_identities)) != BANK_LOAN_ROWS:
            raise RuntimeError("bank_loan_manifest_duplicate_identity")
        if any(
            not identity
            or len(identity) > 256
            or not re.fullmatch(r"[^\s\x00-\x1f]+", identity)
            for identity in self.source_identities
        ):
            raise RuntimeError("bank_loan_manifest_identity_invalid")
        if not self.target_spreadsheet_id:
            raise RuntimeError("bank_loan_manifest_target_required")
        if self.target_worksheet != CANARY_TARGET_SHEET:
            raise RuntimeError("bank_loan_manifest_sheet_invalid")
        if (
            self.classification != "loan_repayment"
            or self.projected_classification != "expense"
            or (self.category_major, self.category_minor) != LOAN_EXPENSE_CATEGORY
        ):
            raise RuntimeError("bank_loan_manifest_semantics_invalid")

    def to_private_dict(self) -> dict:
        self.validate()
        return {
            "schema_version": self.schema_version,
            "created_at": self.created_at.isoformat(),
            "source_identities": list(self.source_identities),
            "target_spreadsheet_id": self.target_spreadsheet_id,
            "target_worksheet": self.target_worksheet,
            "classification": self.classification,
            "projected_classification": self.projected_classification,
            "category_major": self.category_major,
            "category_minor": self.category_minor,
            "min_rows": self.min_rows,
            "max_rows": self.max_rows,
        }


def _outside_repository(path: Path, repository_root: Path) -> Path:
    resolved = path.resolve()
    root = repository_root.resolve()
    try:
        resolved.relative_to(root)
    except ValueError:
        return resolved
    raise RuntimeError("bank_loan_manifest_must_be_outside_repository")


def write_bank_loan_manifest(
    path: str | Path,
    manifest: BankLoanExactManifest,
    *,
    repository_root: str | Path,
) -> Path:
    """Create a private manifest once; existing authority is never overwritten."""
    destination = _outside_repository(Path(path), Path(repository_root))
    manifest.validate()
    destination.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        manifest.to_private_dict(), ensure_ascii=False, sort_keys=True, indent=2,
    )
    with destination.open("x", encoding="utf-8", newline="\n") as handle:
        handle.write(payload + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    return destination


def load_bank_loan_manifest(path: str | Path) -> BankLoanExactManifest:
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        manifest = BankLoanExactManifest(
            schema_version=int(raw["schema_version"]),
            created_at=datetime.fromisoformat(raw["created_at"]),
            source_identities=tuple(raw["source_identities"]),
            target_spreadsheet_id=str(raw["target_spreadsheet_id"]),
            target_worksheet=str(raw["target_worksheet"]),
            classification=str(raw["classification"]),
            projected_classification=str(raw["projected_classification"]),
            category_major=str(raw["category_major"]),
            category_minor=str(raw["category_minor"]),
            min_rows=int(raw["min_rows"]),
            max_rows=int(raw["max_rows"]),
        )
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError("bank_loan_manifest_invalid") from exc
    manifest.validate()
    return manifest
