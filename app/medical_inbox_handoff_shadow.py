"""Opt-in receipt-inbox handoff to the existing local Medical review workflow.

The adapter receives only the opaque Drive source identity and the already
data-minimised privacy-gate result.  It performs no OCR, network, Sheets, or
Drive I/O and grants no production write authority.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from .medical_review_workflow_shadow import (
    MedicalReviewItem,
    ReviewReconciliation,
    SafeReviewValidationError,
    build_medical_review_item,
    reconcile_medical_review_item,
)
from .receipt_privacy_gate import ReceiptPrivacyGateResult


class MedicalInboxHandoffShadow:
    """Idempotent queue for unresolved Medical review items.

    ``store_path`` is optional to preserve the original in-memory shadow mode.
    When supplied, only the value-free ``MedicalReviewItem`` records are
    persisted locally; no source bytes or OCR material is ever written.
    """

    def __init__(
        self,
        *,
        identity_key: bytes,
        store_path: str | os.PathLike[str] | None = None,
        parser_version: str = "receipt-privacy-gate-v1",
        policy_version: str = "medical-review-policy-v1",
    ) -> None:
        if type(identity_key) is not bytes or len(identity_key) < 16:
            raise SafeReviewValidationError()
        self._identity_key = identity_key
        self._parser_version = parser_version
        self._policy_version = policy_version
        self._store_path = Path(store_path) if store_path is not None else None
        self._items: dict[str, MedicalReviewItem] = {}
        if self._store_path is not None:
            self._load()

    def _load(self) -> None:
        assert self._store_path is not None
        try:
            with self._store_path.open("r", encoding="utf-8") as handle:
                document = json.load(handle)
            if type(document) is not dict or document.get("schema_version") != 1:
                raise ValueError
            raw_items = document.get("items")
            if type(raw_items) is not list:
                raise ValueError
            loaded = [MedicalReviewItem.safe_validate(value) for value in raw_items]
            self._items = {item.review_item_id: item for item in loaded}
            if len(self._items) != len(loaded):
                raise ValueError
        except FileNotFoundError:
            return
        except Exception as exc:
            raise SafeReviewValidationError() from None

    def _persist(self) -> None:
        if self._store_path is None:
            return
        parent = self._store_path.parent
        if not parent.is_dir():
            raise SafeReviewValidationError()
        document = {
            "schema_version": 1,
            "items": [item.model_dump(mode="json") for item in self._items.values()],
        }
        temporary_name: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=parent, prefix=f".{self._store_path.name}.",
                suffix=".tmp", delete=False,
            ) as handle:
                temporary_name = handle.name
                json.dump(document, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_name, self._store_path)
            temporary_name = None
        except Exception:
            raise SafeReviewValidationError() from None
        finally:
            if temporary_name is not None:
                try:
                    os.unlink(temporary_name)
                except OSError:
                    pass

    def observe(
        self, *, source_id: str, gate: ReceiptPrivacyGateResult
    ) -> ReviewReconciliation | None:
        """Create one review item for an unresolved Medical gate decision.

        Confirmed Medical decisions already have a complete local privacy
        preview.  Sensitive-unknown decisions deliberately remain on hold and
        are never promoted into the Medical queue by this adapter.
        """
        if gate.classification != "medical" or gate.status != "needs_review":
            return None

        materialization_status = (
            "stable" if gate.extraction_status == "extracted" and gate.text_present
            else "incomplete"
        )
        proposed = build_medical_review_item(
            source_unit_identity=source_id,
            identity_key=self._identity_key,
            gate=gate,
            materialization_status=materialization_status,
            parser_version=self._parser_version,
            policy_version=self._policy_version,
        )
        existing = self._items.get(proposed.review_item_id)
        result = reconcile_medical_review_item(existing, proposed)
        if result.action == "new_item":
            self._items[proposed.review_item_id] = proposed
            try:
                self._persist()
            except Exception:
                self._items.pop(proposed.review_item_id, None)
                raise
        return result

    def items(self) -> tuple[MedicalReviewItem, ...]:
        """Return the safe, value-free artifacts currently awaiting review."""
        return tuple(self._items.values())
