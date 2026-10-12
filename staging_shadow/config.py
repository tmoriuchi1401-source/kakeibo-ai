"""Explicit isolated configuration; no credential/secret file readers."""
from dataclasses import dataclass
from urllib.parse import urlsplit


@dataclass(frozen=True)
class Config:
    origin: str
    ttl: int = 600
    @classmethod
    def read(cls, env):
        if env.get('UNIFIED_STAGING_ENABLED') != 'true': raise ValueError('staging_explicit_opt_in_required')
        allowed = {'UNIFIED_STAGING_ENABLED', 'HGA_ORIGIN', 'UNIFIED_RECEIPT_WRITE_ENABLED'}
        for name in env:
            if name in allowed: continue
            if (name.startswith(('HGA_', 'GOOGLE_', 'GCP_', 'UNIFIED_RECEIPT_', 'FIRESTORE_', 'AWS_', 'KAKEIBO_', 'BANK_'))
                    or any(word in name for word in ('SECRET', 'CREDENTIAL', 'AUTHORITY', 'SPREADSHEET', 'FOLDER_ID', 'FILE_ID', 'GEMINI', 'TOKEN', 'PROXY', 'ACCOUNTING'))):
                raise ValueError('staging_connection_configuration_forbidden')
        if env.get('UNIFIED_RECEIPT_WRITE_ENABLED', 'false') != 'false': raise ValueError('staging_write_forbidden')
        origin = env.get('HGA_ORIGIN', 'https://staging.invalid')
        u = urlsplit(origin)
        if u.scheme != 'https' or not u.hostname or u.username or u.password or u.query or u.fragment or u.path:
            raise ValueError('staging_https_origin_required')
        return cls(origin)
