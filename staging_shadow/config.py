"""Explicit isolated configuration; no credential/secret file readers."""
from dataclasses import dataclass
from urllib.parse import urlsplit


@dataclass(frozen=True)
class Config:
    origin: str
    ttl: int = 600
    mode: str = 'ci'
    port: int = 8080
    @property
    def bind_host(self):
        return '0.0.0.0' if self.mode == 'cloud-run' else '127.0.0.1'
    @classmethod
    def read(cls, env):
        if env.get('UNIFIED_STAGING_ENABLED') != 'true': raise ValueError('staging_explicit_opt_in_required')
        allowed = {'UNIFIED_STAGING_ENABLED', 'UNIFIED_STAGING_MODE', 'HGA_ORIGIN', 'UNIFIED_RECEIPT_WRITE_ENABLED'}
        for name in env:
            if name in allowed: continue
            if (name.startswith(('HGA_', 'GOOGLE_', 'GCP_', 'UNIFIED_STAGING_', 'UNIFIED_RECEIPT_', 'FIRESTORE_', 'AWS_', 'KAKEIBO_', 'BANK_'))
                    or any(word in name for word in ('SECRET', 'CREDENTIAL', 'AUTHORITY', 'SPREADSHEET', 'FOLDER_ID', 'FILE_ID', 'GEMINI', 'TOKEN', 'PROXY', 'ACCOUNTING'))):
                raise ValueError('staging_connection_configuration_forbidden')
        if env.get('UNIFIED_RECEIPT_WRITE_ENABLED', 'false') != 'false': raise ValueError('staging_write_forbidden')
        mode = env.get('UNIFIED_STAGING_MODE', 'ci')
        if mode not in ('ci', 'cloud-run'): raise ValueError('staging_mode_invalid')
        if mode == 'ci' and any(name in env for name in ('K_SERVICE', 'K_REVISION', 'K_CONFIGURATION')):
            raise ValueError('staging_cloud_mode_required')
        if mode == 'cloud-run' and ('HGA_ORIGIN' not in env or 'PORT' not in env):
            raise ValueError('staging_cloud_origin_and_port_required')
        raw_port = env.get('PORT', '8080')
        if not isinstance(raw_port, str) or not raw_port.isascii() or not raw_port.isdecimal():
            raise ValueError('staging_port_invalid')
        port = int(raw_port)
        if not 1024 <= port <= 65535: raise ValueError('staging_port_invalid')
        origin = env.get('HGA_ORIGIN', 'https://staging.invalid')
        u = urlsplit(origin)
        if u.scheme != 'https' or not u.hostname or u.username or u.password or u.query or u.fragment or u.path:
            raise ValueError('staging_https_origin_required')
        if mode == 'cloud-run' and origin == 'https://staging.invalid':
            raise ValueError('staging_cloud_origin_required')
        return cls(origin, mode=mode, port=port)
