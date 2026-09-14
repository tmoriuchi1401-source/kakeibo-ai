"""Count-only per-stage completion markers; supplements existing native state.

This one small private Drive JSON is neither a database nor a distributed lock.
The parent workflow holds the production lock. Pending stages are never retried
automatically, including receipt/PayPay/common writers without local checkpoints.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import re

from .drive_run_state import StateBinding, StateError, Transport
from .production_run import COUNT_KEYS, DEPENDENCIES


def _encode(value: dict) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("ascii")


def initial_ledger(binding: StateBinding) -> bytes:
    if binding.source != "production_run":
        raise StateError("ledger_binding_invalid")
    return _encode({"schema": 1, "binding": binding.reference, "generation": 0,
                    "sources": {source: {"phase": "ready", "last_success": None, "counts": {},
                                         "attempt": "", "error": "", "duration_seconds": 0.0}
                                for source in DEPENDENCIES}})


def validate_ledger(payload: bytes, binding: StateBinding) -> dict:
    try:
        if len(payload) > 64 * 1024 or binding.source != "production_run":
            raise StateError("ledger_invalid")
        value = json.loads(payload)
        if (set(value) != {"schema", "binding", "generation", "sources"}
                or value["schema"] != 1 or value["binding"] != binding.reference
                or type(value["generation"]) is not int or value["generation"] < 0
                or set(value["sources"]) != set(DEPENDENCIES)):
            raise StateError("ledger_invalid")
        for item in value["sources"].values():
            if (set(item) != {"phase", "last_success", "counts", "attempt", "error", "duration_seconds"}
                    or item["phase"] not in {"ready", "pending"}
                    or item["error"] not in {"", "source_execution_failed"}
                    or not isinstance(item["counts"], dict) or not set(item["counts"]) <= COUNT_KEYS
                    or any(type(count) is not int or count < 0 for count in item["counts"].values())
                    or type(item["duration_seconds"]) not in {float, int}
                    or not 0 <= item["duration_seconds"] <= 86400):
                raise StateError("ledger_invalid")
            if item["last_success"] is not None:
                stamp = datetime.fromisoformat(item["last_success"])
                if stamp.tzinfo is None:
                    raise StateError("ledger_invalid")
            if not re.fullmatch(r"[0-9a-f]{32}|", item["attempt"]):
                raise StateError("ledger_invalid")
            if item["phase"] == "pending" and not item["attempt"]:
                raise StateError("ledger_invalid")
        return value
    except Exception:
        raise StateError("ledger_invalid") from None


class ProductionLedger:
    def __init__(self, transport: Transport, binding: StateBinding):
        self.transport, self.binding = transport, binding
        self.payload = transport.read()  # Missing state is never initialized here.
        self.value = validate_ledger(self.payload, binding)

    def _save(self, value: dict) -> None:
        value["generation"] += 1
        proposed = _encode(value)
        validate_ledger(proposed, self.binding)
        if self.transport.read() != self.payload:
            raise StateError("ledger_changed_since_read")
        self.transport.write(proposed)
        if self.transport.read() != proposed:
            raise StateError("ledger_readback_failed")
        self.payload, self.value = proposed, value

    def begin(self, source: str, attempt: str) -> None:
        value = validate_ledger(self.payload, self.binding)
        if value["sources"][source]["phase"] == "pending":
            raise StateError("source_reconciliation_required")
        value["sources"][source].update(phase="pending", attempt=attempt, error="")
        self._save(value)

    def complete(self, source: str, result: dict, duration: float) -> None:
        value = validate_ledger(self.payload, self.binding)
        if value["sources"][source]["phase"] != "pending":
            raise StateError("ledger_begin_required")
        value["sources"][source].update(
            phase="ready", attempt="", error="", last_success=datetime.now(timezone.utc).isoformat(),
            counts={key: count for key, count in result.items() if key in COUNT_KEYS and type(count) is int and count >= 0},
            duration_seconds=round(duration, 3),
        )
        self._save(value)

    def fail(self, source: str) -> None:
        value = validate_ledger(self.payload, self.binding)
        if value["sources"][source]["phase"] != "pending":
            return
        value["sources"][source]["error"] = "source_execution_failed"
        self._save(value)

    def release_after_reconciliation(self, source: str, *, observed_digest: str, evidence_reference: str) -> None:
        """Operator only; native source state pending must also be reconciled."""
        if (hashlib.sha256(self.payload).hexdigest() != observed_digest
                or not re.fullmatch(r"[0-9a-f]{64}", evidence_reference)):
            raise StateError("ledger_recovery_evidence_required")
        value = validate_ledger(self.payload, self.binding)
        if value["sources"][source]["phase"] != "pending":
            raise StateError("ledger_recovery_pending_required")
        value["sources"][source].update(phase="ready", attempt="", error="")
        self._save(value)
