from __future__ import annotations
import json, os, tempfile
import re
from dataclasses import dataclass, field
from dotenv import load_dotenv

load_dotenv()

@dataclass(frozen=True)
class Settings:
    gemini_api_key: str = os.getenv("GEMINI_API_KEY", "")
    gemini_model: str = os.getenv("GEMINI_MODEL", "gemini-3.6-flash")
    spreadsheet_id: str = os.getenv("SPREADSHEET_ID", "")
    service_account_file: str = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", "service-account.json")
    receipt_drive_folder_id: str = os.getenv("RECEIPT_DRIVE_FOLDER_ID", "")
    paypay_drive_folder_id: str = os.getenv("PAYPAY_DRIVE_FOLDER_ID", "")
    payroll_drive_folder_id: str = os.getenv("PAYROLL_DRIVE_FOLDER_ID", "")
    amazon_order_history_folder_id: str = os.getenv("AMAZON_ORDER_HISTORY_FOLDER_ID", "")
    processed_drive_folder_id: str = os.getenv("PROCESSED_DRIVE_FOLDER_ID", "")
    backup_drive_folder_id: str = os.getenv("BACKUP_DRIVE_FOLDER_ID", "")
    drive_backup_token_json: str = os.getenv("GOOGLE_DRIVE_BACKUP_TOKEN_JSON", "")
    drive_backup_token_file: str = os.getenv("GOOGLE_DRIVE_BACKUP_TOKEN_FILE", "drive-backup-token.json")
    reconciliation_lookback_months: int = int(os.getenv("RECONCILIATION_LOOKBACK_MONTHS", "6"))
    bank_internal_transfers_json: str = field(default_factory=lambda: os.getenv(
        "BANK_CONFIRMED_INTERNAL_TRANSFERS_JSON", "[]",
    ))
    gmail_token_json: str = os.getenv("GOOGLE_GMAIL_TOKEN_JSON", "")
    aupay_gmail_query: str = os.getenv("AUPAY_GMAIL_QUERY") or (
        'in:anywhere from:info@wallet.auone.jp '
        'subject:"【au PAY】ご利用のお知らせ" "メールコードP1002" newer_than:30d'
    )
    aupay_card_gmail_query: str = field(default_factory=lambda: os.getenv("AUPAY_CARD_GMAIL_QUERY") or (
        'in:anywhere from:kddi-fs.com '
        'subject:"【ご利用詳細】au PAY カード" newer_than:30d'
    ))
    aupay_card_statement_gmail_query: str = field(default_factory=lambda: os.getenv(
        "AUPAY_CARD_STATEMENT_GMAIL_QUERY",
    ) or (
        'in:anywhere from:kddi-fs.com "au PAY カード" '
        '{subject:"ご請求額" subject:"請求額確定"} newer_than:1y'
    ))

    def validate(self, *, need_gemini=False, need_sheet=False, need_drive=False,
                 need_gmail=False, need_backup=False, need_processed=False,
                 need_paypay_drive=False, need_payroll_drive=False):
        missing=[]
        if need_gemini and not self.gemini_api_key: missing.append("GEMINI_API_KEY")
        if need_sheet and not self.spreadsheet_id: missing.append("SPREADSHEET_ID")
        if need_drive and not self.receipt_drive_folder_id: missing.append("RECEIPT_DRIVE_FOLDER_ID")
        if need_paypay_drive and not self.paypay_drive_folder_id: missing.append("PAYPAY_DRIVE_FOLDER_ID")
        if need_payroll_drive and not self.payroll_drive_folder_id: missing.append("PAYROLL_DRIVE_FOLDER_ID")
        if need_processed and not self.processed_drive_folder_id: missing.append("PROCESSED_DRIVE_FOLDER_ID")
        if need_backup and not self.backup_drive_folder_id: missing.append("BACKUP_DRIVE_FOLDER_ID")
        if need_backup and not self.drive_backup_token(): missing.append("GOOGLE_DRIVE_BACKUP_TOKEN_JSON")
        if need_gmail and not self.gmail_token_json: missing.append("GOOGLE_GMAIL_TOKEN_JSON")
        if missing: raise RuntimeError("未設定: " + ", ".join(missing))

    def drive_backup_token(self) -> str:
        if self.drive_backup_token_json.strip():
            return self.drive_backup_token_json.strip()
        if self.drive_backup_token_file and os.path.exists(self.drive_backup_token_file):
            with open(self.drive_backup_token_file, encoding="utf-8") as handle:
                return handle.read()
        return ""

    def bank_confirmed_internal_transfers(self) -> frozenset[tuple[str, str, str]]:
        """Load active, exact owned-account rules from private environment JSON.

        The JSON remains outside Git.  Requiring already-normalized descriptions
        makes the operator-approved value, rather than a fuzzy name heuristic,
        the durable authority.
        """
        from .bank_reconciliation import normalize_bank_description

        try:
            values = json.loads(self.bank_internal_transfers_json)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                "BANK_CONFIRMED_INTERNAL_TRANSFERS_JSON must be a JSON array"
            ) from exc
        if not isinstance(values, list) or any(
            not isinstance(value, dict)
            or set(value) != {
                "normalized_description", "direction", "account_alias", "active",
            }
            or not isinstance(value["normalized_description"], str)
            or not value["normalized_description"].strip()
            or value["direction"] not in {"incoming", "outgoing"}
            or not isinstance(value["account_alias"], str)
            or not value["account_alias"].strip()
            or not isinstance(value["active"], bool)
            for value in values
        ):
            raise RuntimeError(
                "BANK_CONFIRMED_INTERNAL_TRANSFERS_JSON must contain exact "
                "normalized_description/direction/account_alias/active objects"
            )
        if any(
            value["normalized_description"]
            != normalize_bank_description(value["normalized_description"])
            or not re.fullmatch(
                r"[a-z0-9][a-z0-9_-]{1,63}", value["account_alias"].strip(),
            )
            for value in values
        ):
            raise RuntimeError(
                "BANK_CONFIRMED_INTERNAL_TRANSFERS_JSON descriptions must already "
                "be normalized and account_alias must be non-sensitive"
            )
        return frozenset(
            (
                value["normalized_description"], value["direction"],
                value["account_alias"].strip(),
            )
            for value in values
            if value["active"]
        )


def service_account_source() -> tuple[str|None, dict|None]:
    raw=os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
    if raw:
        return None, json.loads(raw)
    return os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", "service-account.json"), None
