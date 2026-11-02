"""Shared strict evidence capture, reused from recovery preparation fecfd82.

This module has no executable audit, Preview, persistence, or CLI entry.
"""
import base64
from copy import deepcopy
from datetime import datetime
from email import policy as email_policy
from email.parser import BytesParser
import hashlib
import time

from .aupay_card_window_preview import START, END, digest
from .projection_store import ReadOnlyProjectionStore


class EvidenceError(RuntimeError):
    pass


def require(ok, code="evidence_precondition_failed"):
    if not ok:
        raise EvidenceError(code)


class GetOnly:
    """Allowlisted discovery GET surface; no retry, mutation, or API details out."""
    def __init__(self, api, deadline):
        self.api, self.deadline = api, deadline

    def __getattr__(self, name):
        if name in {"users", "messages", "files", "spreadsheets", "values"}:
            return lambda: GetOnly(getattr(self.api, name)(), self.deadline)
        require(name in {"list", "get", "get_media", "batchGet"}, "evidence_mutation_forbidden")
        def build(**kwargs):
            require(time.monotonic() < self.deadline, "evidence_timeout")
            request = getattr(self.api, name)(**kwargs)
            require(getattr(request, "method", "GET") == "GET", "evidence_mutation_forbidden")
            deadline = self.deadline
            class Request:
                def execute(self, **_ignored):
                    require(time.monotonic() < deadline, "evidence_timeout")
                    try:
                        return request.execute(num_retries=0)
                    except Exception:
                        # _collect's HttpError retry must not start a second attempt.
                        raise EvidenceError("evidence_read_failed_no_retry") from None
            return Request()
        return build


class ObservedStore(ReadOnlyProjectionStore):
    """Track every projection document read, including monthly duplicate inputs."""
    def __init__(self, backing):
        super().__init__(backing)
        self.observed = {}

    def read(self, key):
        value = self.backing.read(key)
        if key in self.observed:
            require(value == self.observed[key], "evidence_document_changed")
        self.observed[key] = deepcopy(value)
        return deepcopy(value)

    def verify(self):
        for key, value in self.observed.items():
            require(self.backing.read(key) == value, "evidence_document_changed")
        return {key: digest(value) for key, value in sorted(self.observed.items())}


class CapturedGmail:
    """Add private provenance to the existing strict collector, not a parser fork."""
    def __init__(self, service):
        self.api = service.users().messages()
        self.inventory = []

    def users(self): return self
    def messages(self): return self
    def list(self, **kwargs): return self.api.list(**kwargs)

    def get(self, **kwargs):
        from .aupay_mail_pipeline import parse_aupay_card_raw_partial
        request = self.api.get(**kwargs)
        inventory = self.inventory
        identity = kwargs["id"]
        class Request:
            def execute(self):
                response = request.execute()
                try:
                    require(response.get("id") == identity and response.get("raw"), "evidence_gmail_incomplete")
                    raw = base64.b64decode(response["raw"] + "=" * (-len(response["raw"]) % 4),
                                           altchars=b"-_", validate=True)
                    stamp = response["internalDate"]
                    require(isinstance(stamp, str) and stamp.isdecimal(), "evidence_gmail_timestamp_invalid")
                    require(int(datetime.fromisoformat(START).timestamp()) * 1000 <= int(stamp)
                            < int(datetime.fromisoformat(END).timestamp()) * 1000, "evidence_gmail_outside_window")
                    message_id = str(BytesParser(policy=email_policy.default).parsebytes(raw)["Message-ID"] or "")
                    require(bool(message_id), "evidence_gmail_message_id_missing")
                    parsed = parse_aupay_card_raw_partial(raw)
                    inventory.append({"gmail_id": identity, "internal_date_ms": stamp,
                        "rfc_message_id": message_id, "raw_sha256": hashlib.sha256(raw).hexdigest(),
                        "source_identities": [r["import_id"] for r in parsed.reconciliation_inputs()]})
                except EvidenceError:
                    raise
                except Exception:
                    raise EvidenceError("evidence_gmail_evidence_invalid") from None
                return response
        return Request()
