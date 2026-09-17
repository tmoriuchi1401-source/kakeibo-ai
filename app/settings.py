from __future__ import annotations
import base64, json, os, tempfile
import re
from pathlib import Path
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
    bank_pdf_drive_folder_id: str = os.getenv("BANK_PDF_DRIVE_FOLDER_ID", "")
    amazon_order_history_folder_id: str = os.getenv("AMAZON_ORDER_HISTORY_FOLDER_ID", "")
    processed_drive_folder_id: str = os.getenv("PROCESSED_DRIVE_FOLDER_ID", "")
    backup_drive_folder_id: str = os.getenv("BACKUP_DRIVE_FOLDER_ID", "")
    drive_backup_token_json: str = os.getenv("GOOGLE_DRIVE_BACKUP_TOKEN_JSON", "")
    drive_backup_token_file: str = os.getenv("GOOGLE_DRIVE_BACKUP_TOKEN_FILE", "drive-backup-token.json")
    reconciliation_lookback_months: int = int(os.getenv("RECONCILIATION_LOOKBACK_MONTHS", "6"))
    bank_internal_transfers_json: str = field(default_factory=lambda: os.getenv(
        "BANK_CONFIRMED_INTERNAL_TRANSFERS_JSON", "[]",
    ))
    bank_non_own_classifications_json: str = field(default_factory=lambda: os.getenv(
        "BANK_CONFIRMED_NON_OWN_CLASSIFICATIONS_JSON", "[]",
    ))
    bank_docomo_operator_rules_json: str = field(default_factory=lambda: os.getenv(
        "BANK_CONFIRMED_DOCOMO_SMTB_RULES_JSON", "[]",
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
    medical_review_store_path: str = os.getenv("MEDICAL_REVIEW_STORE_PATH") or str(
        Path(os.getenv("LOCALAPPDATA") or (Path.home() / ".local" / "share"))
        / "kakeibo-ai" / "medical-review.json"
    )
    medical_review_identity_key: str = os.getenv("MEDICAL_REVIEW_IDENTITY_KEY", "")
    medical_review_shadow_enabled: bool = os.getenv(
        "MEDICAL_REVIEW_SHADOW_ENABLED", "false"
    ).strip().lower() in {"1", "true", "yes", "on"}
    # These are intentionally independent.  Shipping code alone never exposes
    # the UI, saves a rule, or uses rules for automatic posting.
    category_rule_ui_enabled: bool = os.getenv(
        "CATEGORY_RULE_UI_ENABLED", "false"
    ).strip().lower() in {"1", "true", "yes", "on"}
    category_rule_save_enabled: bool = os.getenv(
        "CATEGORY_RULE_SAVE_ENABLED", "false"
    ).strip().lower() in {"1", "true", "yes", "on"}
    category_rule_auto_apply_enabled: bool = os.getenv(
        "CATEGORY_RULE_AUTO_APPLY_ENABLED", "false"
    ).strip().lower() in {"1", "true", "yes", "on"}
    # Historical backfill has independent opt-in controls.  A stored rule or a
    # future auto-apply flag never authorizes changing prior ledger rows.
    category_backfill_preview_enabled: bool = os.getenv(
        "CATEGORY_BACKFILL_PREVIEW_ENABLED", "false"
    ).strip().lower() in {"1", "true", "yes", "on"}
    category_backfill_apply_enabled: bool = os.getenv(
        "CATEGORY_BACKFILL_APPLY_ENABLED", "false"
    ).strip().lower() in {"1", "true", "yes", "on"}
    aupay_card_statement_gmail_query: str = field(default_factory=lambda: os.getenv(
        "AUPAY_CARD_STATEMENT_GMAIL_QUERY",
    ) or (
        'in:anywhere from:kddi-fs.com "au PAY カード" '
        '{subject:"ご請求金額確定" subject:"ご請求額" '
        'subject:"請求額確定"} newer_than:1y'
    ))
    def validate(self, *, need_gemini=False, need_sheet=False, need_drive=False,
                 need_gmail=False, need_backup=False, need_processed=False,
                 need_paypay_drive=False, need_payroll_drive=False,
                 need_bank_pdf_drive=False):
        missing=[]
        if need_gemini and not self.gemini_api_key: missing.append("GEMINI_API_KEY")
        if need_sheet and not self.spreadsheet_id: missing.append("SPREADSHEET_ID")
        if need_drive and not self.receipt_drive_folder_id: missing.append("RECEIPT_DRIVE_FOLDER_ID")
        if need_paypay_drive and not self.paypay_drive_folder_id: missing.append("PAYPAY_DRIVE_FOLDER_ID")
        if need_payroll_drive and not self.payroll_drive_folder_id: missing.append("PAYROLL_DRIVE_FOLDER_ID")
        if need_bank_pdf_drive and not self.bank_pdf_drive_folder_id: missing.append("BANK_PDF_DRIVE_FOLDER_ID")
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

    def medical_review_identity_key_bytes(self) -> bytes:
        """Decode the pre-existing HMAC key contract without persisting it."""
        raw = self.medical_review_identity_key.strip()
        if not raw:
            raise RuntimeError("未設定: MEDICAL_REVIEW_IDENTITY_KEY")
        try:
            key = base64.b64decode(raw, validate=True)
        except Exception:
            raise RuntimeError("MEDICAL_REVIEW_IDENTITY_KEY はbase64形式で設定してください") from None
        if len(key) < 16:
            raise RuntimeError("MEDICAL_REVIEW_IDENTITY_KEY が短すぎます")
        return key

    def medical_review_store_parent(self) -> Path:
        path = Path(self.medical_review_store_path).expanduser()
        if not path.is_absolute():
            raise RuntimeError("MEDICAL_REVIEW_STORE_PATH は絶対パスで設定してください")
        try:
            path.resolve().relative_to(Path(__file__).resolve().parents[1])
        except ValueError:
            pass
        else:
            raise RuntimeError("MEDICAL_REVIEW_STORE_PATH はrepository外に設定してください")
        return path.parent

    def medical_review_store_file(self) -> Path:
        self.medical_review_store_parent()
        return Path(self.medical_review_store_path).expanduser()

    def ensure_medical_review_store_parent(self) -> Path:
        parent = self.medical_review_store_parent()
        try:
            parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            raise RuntimeError("MEDICAL_REVIEW_STORE_PATH の保存先を初期化できません") from None
        return parent

    def medical_review_handoff(self):
        """Build the configured local handoff; callers opt in explicitly."""
        from .medical_inbox_handoff_shadow import MedicalInboxHandoffShadow

        key = self.medical_review_identity_key_bytes()
        self.ensure_medical_review_store_parent()
        return MedicalInboxHandoffShadow(
            identity_key=key,
            store_path=self.medical_review_store_file(),
        )

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
        rules = {
            (
                value["normalized_description"], value["direction"],
                value["account_alias"].strip(),
            )
            for value in values
            if value["active"]
        }
        rules.update(
            (
                value["normalized_description"], value["direction"],
                value["account_alias"].strip(),
            )
            for value in self.bank_confirmed_docomo_smtb_rules()
            if value["classification"] == "transfer" and value["active"]
        )
        return frozenset(rules)

    def bank_confirmed_non_own_classifications(
        self,
    ) -> frozenset[tuple[str, str, str, str]]:
        """Load exact operator-confirmed non-write classifications privately.

        This is intentionally separate from owned-account transfer authority.
        Only exact normalized description/direction/account matches are accepted;
        no fuzzy ownership or amount-based inference is permitted.
        """
        from .bank_reconciliation import normalize_bank_description

        try:
            values = json.loads(self.bank_non_own_classifications_json)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                "BANK_CONFIRMED_NON_OWN_CLASSIFICATIONS_JSON must be a JSON array"
            ) from exc
        if not isinstance(values, list) or any(
            not isinstance(value, dict)
            or set(value) != {
                "normalized_description", "direction", "account_alias",
                "classification", "active",
            }
            or not isinstance(value["normalized_description"], str)
            or not value["normalized_description"].strip()
            or value["direction"] not in {"incoming", "outgoing"}
            or not isinstance(value["account_alias"], str)
            or not value["account_alias"].strip()
            or value["classification"] not in {
                "income", "expense", "reimbursement", "other_nonwrite",
                "needs_review",
            }
            or not isinstance(value["active"], bool)
            for value in values
        ):
            raise RuntimeError(
                "BANK_CONFIRMED_NON_OWN_CLASSIFICATIONS_JSON must contain exact "
                "normalized_description/direction/account_alias/classification/active objects"
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
                "BANK_CONFIRMED_NON_OWN_CLASSIFICATIONS_JSON descriptions must already "
                "be normalized and account_alias must be non-sensitive"
            )
        rules = {
            (
                value["normalized_description"], value["direction"],
                value["account_alias"].strip(), value["classification"],
            )
            for value in values
            if value["active"]
        }
        rules.update(
            (
                value["normalized_description"], value["direction"],
                value["account_alias"].strip(), value["classification"],
            )
            for value in self.bank_confirmed_docomo_smtb_rules()
            if value["classification"] != "transfer" and value["active"]
        )
        return frozenset(rules)

    def bank_confirmed_docomo_smtb_rules(self) -> tuple[dict, ...]:
        """Load exact Docomo SMTB operator rules from Git-ignored config."""
        from .bank_reconciliation import normalize_bank_description

        try:
            values = json.loads(self.bank_docomo_operator_rules_json)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                "BANK_CONFIRMED_DOCOMO_SMTB_RULES_JSON must be a JSON array"
            ) from exc
        allowed = {
            "transfer", "income", "expense", "reimbursement",
            "other_nonwrite", "needs_review",
        }
        if not isinstance(values, list) or any(
            not isinstance(value, dict)
            or set(value) != {
                "normalized_description", "direction", "account_alias",
                "classification", "active",
            }
            or not isinstance(value["normalized_description"], str)
            or not value["normalized_description"].strip()
            or value["direction"] not in {"incoming", "outgoing"}
            or not isinstance(value["account_alias"], str)
            or not value["account_alias"].strip()
            or value["classification"] not in allowed
            or not isinstance(value["active"], bool)
            for value in values
        ):
            raise RuntimeError(
                "BANK_CONFIRMED_DOCOMO_SMTB_RULES_JSON must contain exact "
                "normalized_description/direction/account_alias/classification/active objects"
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
                "BANK_CONFIRMED_DOCOMO_SMTB_RULES_JSON descriptions must already "
                "be normalized and account_alias must be non-sensitive"
            )
        return tuple(values)


def service_account_source() -> tuple[str|None, dict|None]:
    raw=os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
    if raw:
        return None, json.loads(raw)
    return os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", "service-account.json"), None
