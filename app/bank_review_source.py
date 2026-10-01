"""Bounded, read-only bank inbox evaluation for native Sheets review.

Source refresh and immediately-before-save evaluation both use the same PDF
parser, classifiers and exact ledger proofs. No snapshot from the visible UI
is used as the authoritative current source.
"""
from dataclasses import dataclass
from collections import defaultdict
import hashlib
from pathlib import Path
import tempfile

from .bank_archive_evidence import completed_bank_postings, import_matches
from .bank_income import deposit_decisions, BANK_NAMESPACES
from .bank_pdf_pipeline import BankPdfPipeline
from .bank_reconciliation import classify_bank_transaction
from .bank_review_groups import candidates_from_pdf, digest, make_groups
from .drive_receipts import normalize_folder_id
from .google_clients import download_drive_file
from .reconciliation import parse_import_rows

MAX_FILES = 20
MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_TRANSACTIONS = 2000


@dataclass(frozen=True)
class BankReviewSource:
    groups: list
    transactions: dict
    accounts: frozenset
    legacy_rules: dict
    legacy_rules_serialized: dict
    files: tuple
    confirmed_income: tuple
    imports: list
    incomes: list
    expenses: list
    fingerprint: str


class BankReviewSourceReader:
    def __init__(self, db, drive, folder_id, *, legacy_rules, account_alias=None,
                 extra_accounts=frozenset(), download=None):
        self.db, self.drive = db, drive
        self.folder = normalize_folder_id(folder_id)
        self.legacy_rules = legacy_rules
        self.account_alias = account_alias
        self.extra_accounts = frozenset(extra_accounts)
        self.download = download or (lambda file_id: download_drive_file(file_id, service=drive))

    def _files(self):
        response = self.drive.files().list(q=f"'{self.folder}' in parents and trashed=false and mimeType='application/pdf'",
            pageSize=MAX_FILES + 1, fields="files(id,name,mimeType,size,modifiedTime,parents,appProperties),nextPageToken",
            orderBy="name", supportsAllDrives=True, includeItemsFromAllDrives=True).execute()
        files = response.get("files", [])
        if response.get("nextPageToken") or len(files) > MAX_FILES:
            raise ValueError("bank_review_inbox_bound_exceeded")
        if len({item["id"] for item in files}) != len(files):
            raise ValueError("bank_review_duplicate_file_id")
        return sorted(files, key=lambda item: item["id"])

    def _ledgers(self):
        return {name: self.db.get(rng) for name, rng in (
            ("imports", "取込データ!A2:L"), ("incomes", "収入明細!A2:J"), ("expenses", "支出明細!A2:M"))}

    def __call__(self):
        files = self._files()
        ledgers = self._ledgers()
        by_id = defaultdict(list)
        for imported in parse_import_rows(ledgers["imports"]):
            by_id[imported.import_id].append(imported)
        class SnapshotDB:
            def get(self, rng):
                return ledgers["incomes"] if rng == "収入明細!A2:J" else ledgers["expenses"] if rng == "支出明細!A2:M" else ledgers["imports"]
        candidates, transactions, incomes, sources = [], {}, [], []
        accounts = set(self.extra_accounts)
        with tempfile.TemporaryDirectory(prefix="bank-review-") as directory:
            for number, item in enumerate(files):
                if (self.folder not in item.get("parents", []) or item.get("mimeType") != "application/pdf"
                        or (item.get("appProperties") or {}).get("kakeiboBankPdfProcessedAt")):
                    raise ValueError("bank_review_file_scope_invalid")
                size = item.get("size")
                if size is not None and (not str(size).isdigit() or int(size) > MAX_FILE_BYTES):
                    raise ValueError("bank_review_pdf_size_invalid")
                data = self.download(item["id"])
                if not isinstance(data, bytes) or not 0 < len(data) <= MAX_FILE_BYTES:
                    raise ValueError("bank_review_pdf_size_invalid")
                sha = hashlib.sha256(data).hexdigest()
                path = Path(directory) / f"source-{number}.pdf"
                path.write_bytes(data)
                parsed = BankPdfPipeline().parse(path, account_alias=self.account_alias,
                    existing_identities=set(by_id))
                if parsed.issues or parsed.balance_consistency_failures or not parsed.transactions:
                    raise ValueError("bank_review_pdf_parse_unresolved")
                completed = completed_bank_postings(SnapshotDB(), parsed.transactions, ledgers["imports"])
                classifications = [classify_bank_transaction(tx, **self.legacy_rules) for tx in parsed.transactions]
                deposits, collisions = deposit_decisions(parsed.transactions, **self.legacy_rules)
                if collisions:
                    raise ValueError("bank_review_identity_collision")
                rows = candidates_from_pdf(parsed, classifications, deposits,
                    file_id=item["id"], pdf_sha256=sha, import_rows=ledgers["imports"],
                    completed_postings=completed, income_rows=ledgers["incomes"])
                for tx in parsed.transactions:
                    existing = by_id.get(tx.source_row_identity, [])
                    if existing and not import_matches(tx, existing):
                        raise ValueError("bank_review_existing_import_mismatch")
                    if tx.source_row_identity in transactions:
                        # Overlapping documents need separate provenance
                        # handling; never choose a representative arbitrarily.
                        raise ValueError("bank_review_cross_pdf_identity_collision")
                    transactions[tx.source_row_identity] = tx
                    accounts.add((BANK_NAMESPACES[tx.source], tx.account_alias))
                if len(transactions) > MAX_TRANSACTIONS:
                    raise ValueError("bank_review_transaction_bound_exceeded")
                candidates.extend(rows)
                incomes.extend(decision for decision in deposits if decision.outcome == "confirmed_income")
                sources.append({"file_id": item["id"], "pdf_sha256": sha, "parsed": parsed,
                    "modifiedTime": item.get("modifiedTime", ""), "completed_postings": completed})
        if self._files() != files or self._ledgers() != ledgers:
            raise ValueError("bank_review_source_changed_during_read")
        serialized = {key: sorted(list(value)) for key, value in self.legacy_rules.items()}
        fingerprint = digest({"files": [{key: value for key, value in source.items()
            if key not in {"parsed", "completed_postings"}} for source in sources],
            "ledgers": ledgers, "legacy": serialized})
        return BankReviewSource(make_groups(candidates), transactions, frozenset(accounts),
            self.legacy_rules, serialized, tuple(sources), tuple(incomes),
            ledgers["imports"], ledgers["incomes"], ledgers["expenses"], fingerprint)


def from_environment(db, env):
    from .google_clients import read_only_drive_service
    from .settings import Settings
    settings = Settings(bank_internal_transfers_json=env.get("BANK_CONFIRMED_INTERNAL_TRANSFERS_JSON", "[]"),
        bank_non_own_classifications_json=env.get("BANK_CONFIRMED_NON_OWN_CLASSIFICATIONS_JSON", "[]"),
        bank_docomo_operator_rules_json=env.get("BANK_CONFIRMED_DOCOMO_SMTB_RULES_JSON", "[]"))
    return BankReviewSourceReader(db, read_only_drive_service(), env.get("BANK_PDF_DRIVE_FOLDER_ID", ""),
        legacy_rules={"confirmed_internal_transfers": settings.bank_confirmed_internal_transfers(),
                      "confirmed_non_own_classifications": settings.bank_confirmed_non_own_classifications()})
