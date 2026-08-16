from __future__ import annotations
import json, os, tempfile
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv()

@dataclass(frozen=True)
class Settings:
    gemini_api_key: str = os.getenv("GEMINI_API_KEY", "")
    gemini_model: str = os.getenv("GEMINI_MODEL", "gemini-3.6-flash")
    spreadsheet_id: str = os.getenv("SPREADSHEET_ID", "")
    service_account_file: str = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", "service-account.json")
    receipt_drive_folder_id: str = os.getenv("RECEIPT_DRIVE_FOLDER_ID", "")
    processed_drive_folder_id: str = os.getenv("PROCESSED_DRIVE_FOLDER_ID", "")
    gmail_token_json: str = os.getenv("GOOGLE_GMAIL_TOKEN_JSON", "")
    aupay_gmail_query: str = os.getenv("AUPAY_GMAIL_QUERY") or (
        'in:anywhere from:info@wallet.auone.jp '
        'subject:"【au PAY】ご利用のお知らせ" "メールコードP1002" newer_than:30d'
    )

    def validate(self, *, need_gemini=False, need_sheet=False, need_drive=False, need_gmail=False):
        missing=[]
        if need_gemini and not self.gemini_api_key: missing.append("GEMINI_API_KEY")
        if need_sheet and not self.spreadsheet_id: missing.append("SPREADSHEET_ID")
        if need_drive and not self.receipt_drive_folder_id: missing.append("RECEIPT_DRIVE_FOLDER_ID")
        if need_gmail and not self.gmail_token_json: missing.append("GOOGLE_GMAIL_TOKEN_JSON")
        if missing: raise RuntimeError("未設定: " + ", ".join(missing))


def service_account_source() -> tuple[str|None, dict|None]:
    raw=os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
    if raw:
        return None, json.loads(raw)
    return os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", "service-account.json"), None
