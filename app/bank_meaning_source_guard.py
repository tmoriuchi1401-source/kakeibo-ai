"""Verify the same inbox originals immediately before bank writes and archive."""
from copy import deepcopy
import hashlib


class BankMeaningSourceGuard:
    def __init__(self, resolver, drive, inbox_id, download):
        self.resolver, self.drive = resolver, drive
        self.inbox_id, self.download = inbox_id, download
        self.originals = {}
        self.identities = set()
        self.settlement_identities = set()

    def __call__(self, tx):
        return self.resolver(tx)

    def observe(self, file, data):
        if (file.get("mimeType") != "application/pdf" or self.inbox_id not in file.get("parents", [])
                or not file.get("modifiedTime") or file.get("trashed")):
            raise ValueError("bank_meaning_original_scope_invalid")
        self.originals[file["id"]] = (deepcopy(file), hashlib.sha256(data).hexdigest())

    def parsed(self, transactions):
        self.identities.update(tx.source_row_identity for tx in transactions)

    def permit_settlement(self, identities):
        identities = set(identities)
        if not identities <= self.identities:
            raise ValueError("bank_meaning_settlement_original_unavailable")
        # Called only after the recurring runner's original, review and write
        # window gates. Merely observing an original grants no write scope.
        self.settlement_identities.update(identities)

    def require_settlement_scope(self, identities):
        identities = set(identities)
        if not identities <= self.identities:
            raise ValueError("bank_meaning_settlement_original_unavailable")
        if not identities <= self.settlement_identities:
            raise ValueError("bank_meaning_settlement_original_not_eligible")

    def require_unchanged(self):
        self.resolver.require_unchanged()
        for file_id, (before, sha) in self.originals.items():
            current = self.drive.files().get(fileId=file_id,
                fields="id,mimeType,modifiedTime,parents,trashed,appProperties",
                supportsAllDrives=True).execute()
            if (current.get("id") != file_id or current.get("trashed")
                    or current.get("mimeType") != "application/pdf"
                    or self.inbox_id not in current.get("parents", [])
                    or current.get("modifiedTime") != before["modifiedTime"]
                    or set(current.get("parents", [])) != set(before["parents"])
                    or (current.get("appProperties") or {}) != (before.get("appProperties") or {})):
                raise ValueError("bank_meaning_original_changed_before_apply")
            data = self.download(file_id)
            if not isinstance(data, bytes) or hashlib.sha256(data).hexdigest() != sha:
                raise ValueError("bank_meaning_original_changed_before_apply")

    def archived(self, file_id):
        # Our verified move is an intentional source mutation. Other originals
        # remain guarded for later writes/moves in this same serialized run.
        self.originals.pop(file_id, None)
