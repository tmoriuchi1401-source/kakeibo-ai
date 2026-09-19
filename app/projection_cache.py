"""Thirteen reusable owner-created files for disposable monthly detail.

Canonical history and long-term totals are not subject to this cache window.
The existing service account only updates files, including at month rollover.
"""
from datetime import datetime
from hashlib import sha256
from zoneinfo import ZoneInfo

from .monthly_projection import ProjectionError, month_key, shift_month


DOCUMENT_KEYS = ("catalog", "category-requests", "index", "journal", "summary", "corrections",
                 "money", "money-migration", "money-reviews", "money-notices", "coverage")
CACHE_KEYS = tuple(f"cache-{n:02d}" for n in range(13))


def cache_key(month):
    month_key(month)
    year, number = map(int, month.split("-"))
    return f"cache-{(year * 12 + number - 1) % 13:02d}"


def initial_files(source_id):
    """Payloads for explicit owner-side provisioning, never ordinary SA writes.

    The source-bound JSON envelope is checked across provisioning and runtime
    accounts, without depending on app-private Drive metadata.
    """
    binding = sha256(source_id.encode()).hexdigest()
    return [{"name": f"kakeibo-projection-{key}.json", "mimeType": "application/json",
             "payload": {"schema": 1, "binding": binding, "key": key, "data": None}}
            for key in (*DOCUMENT_KEYS, *CACHE_KEYS)]


class RollingProjectionStore:
    def __init__(self, backing, *, current_month=None):
        self.backing = backing
        self.current_month = current_month or datetime.now(ZoneInfo("Asia/Tokyo")).strftime("%Y-%m")
        month_key(self.current_month)
        self.cache_months = frozenset(shift_month(self.current_month, -n) for n in range(13))

    def __getattr__(self, name):
        return getattr(self.backing, name)

    def read(self, key):
        if not key.startswith("month-"):
            return self.backing.read(key)
        month = key[6:]
        month_key(month)
        if month not in self.cache_months:
            return None
        value = self.backing.read(cache_key(month))
        if value is None:
            return None
        if (not isinstance(value, dict) or set(value) != {"month", "projection"}
                or not isinstance(value["projection"], dict) or value["projection"].get("month") != value["month"]
                or cache_key(value["month"]) != cache_key(month)):
            raise ProjectionError("projection_cache_invalid")
        return value["projection"] if value["month"] == month else None

    def write(self, key, value):
        if not key.startswith("month-"):
            return self.backing.write(key, value)
        month = key[6:]
        month_key(month)
        if month not in self.cache_months:
            raise ProjectionError("projection_cache_outside_window")
        if not isinstance(value, dict) or value.get("month") != month:
            raise ProjectionError("projection_cache_invalid")
        self.backing.write(cache_key(month), {"month": month, "projection": value})
