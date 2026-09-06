"""Anonymous, shadow-only medical payloads; this module never sends a request.

The input observation is private local OCR evidence.  The only value that may
cross the prospective Gemini boundary is the JSON returned by
``FinalOutboundGate.serialize``.  That gate deliberately accepts an exact,
closed schema rather than arbitrary mappings, prompts, metadata, or OCR DTOs.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import math
import re
import secrets
import unicodedata
from typing import Literal

from .medical_layout_shadow import PageFrame, observe_layout
from .medical_ocr_observation_shadow import OcrObservation
from .medical_payment_evidence import _NUMERIC_RUN, _scope
from .medical_receipt_privacy import _StructuredOcrToken, _compact_ocr_token, _payment_labels_on_line, _structured_amount


_VERSION = "medical-anonymous-shadow-v1"
_UNIT_REF = re.compile(r"unit_[A-Za-z0-9_-]{24,64}\Z")
_REGION_ID = re.compile(r"region_[A-Z]+\Z")
_AMOUNT_ID = re.compile(r"amount_[A-Z]+\Z")
_MAX_REGIONS = 128


class OutboundRejected(ValueError):
    """A fixed, data-free failure for unsafe local-to-provider handoff."""


@dataclass(frozen=True)
class LocalAmountMap:
    """Local-only ID -> amount correspondence.  It has no wire representation."""

    _values: tuple[tuple[str, int], ...] = field(repr=False)

    def resolve(self, amount_id: str) -> int:
        for key, value in self._values:
            if key == amount_id:
                return value
        raise KeyError("unknown local amount id")

    @property
    def ids(self) -> tuple[str, ...]:
        return tuple(key for key, _ in self._values)


@dataclass(frozen=True)
class AnonymousShadowBuild:
    """Payload plus private correspondence; never serialize this wrapper."""

    payload: dict = field(repr=False)
    amount_map: LocalAmountMap = field(repr=False)


def _alpha(index: int) -> str:
    """A, B, ..., Z, AA: opaque labels, never derived from an amount."""
    result = ""
    index += 1
    while index:
        index, remainder = divmod(index - 1, 26)
        result = chr(65 + remainder) + result
    return result


def _confidence(value: float | None) -> Literal["high", "medium", "low"]:
    if value is None or value < 0.7:
        return "low"
    if value < 0.9:
        return "medium"
    return "high"


def _geometry(observation: OcrObservation, ordinal: int) -> dict[str, float]:
    box = observation.regions[ordinal].bbox
    if box is None:
        raise OutboundRejected("unsafe_observation")
    x, y, width, height = box
    result = {"x": x / observation.width, "y": y / observation.height,
              "width": width / observation.width, "height": height / observation.height}
    if not all(type(v) is float and math.isfinite(v) and 0 <= v <= 1 for v in result.values()):
        raise OutboundRejected("unsafe_geometry")
    return result


def _local_scope(text: str) -> str:
    compact = _compact_ocr_token(unicodedata.normalize("NFKC", text))
    return _scope(compact, bool(_payment_labels_on_line(text)))


def _layout(observation: OcrObservation):
    tokens = []
    for region in observation.regions:
        box = region.bbox
        if box is None or region.confidence is None:
            raise OutboundRejected("unsafe_observation")
        x, y, width, height = box
        tokens.append(_StructuredOcrToken(
            # The source PDF ordinal is intentionally not layout data or wire data.
            # This one-unit graph has a local page coordinate of one.
            region.text, 1, x, y, width, height, region.confidence * 100,
            (region.ordinal, 0, 0, 0),
        ))
    layout = observe_layout(tuple(tokens), (PageFrame(1, observation.width, observation.height),),
                            expected_pages=1, observation_complete=observation.complete)
    if "observation_incomplete" in layout.issues:
        raise OutboundRejected("unsafe_layout")
    return layout


def build_anonymous_shadow_payload(observation: OcrObservation) -> AnonymousShadowBuild:
    """Create a safe representation for exactly one image/PDF page.

    This is intentionally more conservative than local evidence collection:
    numeric text without an explicit, allowlisted payment/excluded context,
    possible-payment context, multiple numeric runs, or malformed numbers
    produces no payload.  Nothing in this function authorizes a network call.
    """
    if type(observation) is not OcrObservation or not observation.complete:
        raise OutboundRejected("observation_incomplete")
    if not 1 <= observation.page or not observation.regions or len(observation.regions) > _MAX_REGIONS:
        raise OutboundRejected("invalid_unit")

    layout = _layout(observation)
    included: dict[int, dict] = {}
    values: list[tuple[str, int]] = []
    for region in observation.regions:
        raw_runs = tuple(_NUMERIC_RUN.finditer(unicodedata.normalize("NFKC", region.text)))
        scope = _local_scope(region.text)
        if scope == "possible_payment_region":
            raise OutboundRejected("ambiguous_semantic_context")
        if not raw_runs:
            # Label-only anchors are useful only where their category is explicit.
            if scope in {"payment_region", "excluded"}:
                included[region.ordinal] = {
                    "kind": "context_anchor",
                    "context": "payment" if scope == "payment_region" else "excluded",
                    "amount_id": None,
                }
            continue
        if scope == "unassigned" or len(raw_runs) != 1:
            raise OutboundRejected("ambiguous_numeric_context")
        amount = _structured_amount(raw_runs[0].group().strip())
        if amount is None:
            raise OutboundRejected("malformed_numeric")
        amount_id = f"amount_{_alpha(len(values))}"
        values.append((amount_id, amount))
        included[region.ordinal] = {
            "kind": "numeric_evidence",
            "context": "payment" if scope == "payment_region" else "excluded",
            "amount_id": amount_id,
        }
    if not values:
        raise OutboundRejected("amount_not_observed")

    region_ids = {ordinal: f"region_{_alpha(index)}" for index, ordinal in enumerate(sorted(included))}
    regions = []
    for ordinal in sorted(included):
        item = included[ordinal]
        regions.append({
            "id": region_ids[ordinal], "kind": item["kind"], "context": item["context"],
            "amount_id": item["amount_id"], "geometry": _geometry(observation, ordinal),
            "confidence": _confidence(observation.regions[ordinal].confidence), "status": "observed",
        })
    relations = [
        {"left": region_ids[relation.left], "right": region_ids[relation.right], "kind": relation.axis}
        for relation in layout.relations
        if relation.left in region_ids and relation.right in region_ids
    ]
    competing = any(
        hypothesis.label in region_ids and hypothesis.numeric in region_ids
        and "competing_relationships" in hypothesis.issues
        for hypothesis in layout.hypotheses
    )
    payload = {
        "schema_version": _VERSION,
        # This random value is generated for this handoff only.  It is not a receipt,
        # filename, page ordinal, path, Drive ID, image digest, or persistent key.
        "unit_ref": "unit_" + secrets.token_urlsafe(24),
        "mode": "shadow_only",
        "state": "shadow_ready",
        # The local geometry is only an observation.  It never establishes a
        # payment role, so unresolved state is explicit even in a safe payload.
        "structure_state": "unresolved",
        "competing_structure": competing,
        "regions": regions,
        "relations": relations,
    }
    result = AnonymousShadowBuild(payload, LocalAmountMap(tuple(values)))
    # Gate the builder's own output as a regression tripwire before a caller gets it.
    FinalOutboundGate().serialize(result.payload, _private_literals(observation, result.amount_map))
    return result


def _private_literals(observation: OcrObservation, amount_map: LocalAmountMap) -> tuple[str, ...]:
    """Private material used only by final-byte defense in depth."""
    values = [region.text for region in observation.regions if region.text.strip()]
    for _, amount in amount_map._values:
        values.extend((str(amount), f"{amount}円", f"¥{amount}"))
    return tuple(values)


class FinalOutboundGate:
    """Independent exact-schema and final-bytes gate for a future client boundary."""

    def serialize(self, payload: object, private_literals: tuple[str, ...] = ()) -> bytes:
        self._validate(payload)
        try:
            encoded = json.dumps(payload, ensure_ascii=True, separators=(",", ":"), allow_nan=False).encode("ascii")
        except Exception as error:
            raise OutboundRejected("payload_serialization_rejected") from error
        text = encoded.decode("ascii")
        # OCR text / concrete values are never expected to be wire strings.  This
        # check catches accidental future passthrough before a provider client exists.
        for literal in private_literals:
            if type(literal) is not str:
                raise OutboundRejected("invalid_private_scan")
            escaped = json.dumps(literal, ensure_ascii=True)[1:-1]
            if escaped and escaped in text:
                raise OutboundRejected("private_literal_detected")
        return encoded

    def _validate(self, payload: object) -> None:
        if type(payload) is not dict or set(payload) != {"schema_version", "unit_ref", "mode", "state", "structure_state", "competing_structure", "regions", "relations"}:
            raise OutboundRejected("forbidden_or_unknown_field")
        if (payload["schema_version"] != _VERSION or payload["mode"] != "shadow_only"
                or payload["state"] != "shadow_ready" or type(payload["unit_ref"]) is not str
                or not _UNIT_REF.fullmatch(payload["unit_ref"])):
            raise OutboundRejected("invalid_payload_header")
        if payload["structure_state"] != "unresolved" or type(payload["competing_structure"]) is not bool:
            raise OutboundRejected("invalid_structure_state")
        regions = payload["regions"]
        relations = payload["relations"]
        if type(regions) is not list or not 1 <= len(regions) <= _MAX_REGIONS or type(relations) is not list:
            raise OutboundRejected("invalid_payload_collections")
        ids: set[str] = set()
        amount_ids: set[str] = set()
        for region in regions:
            if type(region) is not dict or set(region) != {"id", "kind", "context", "amount_id", "geometry", "confidence", "status"}:
                raise OutboundRejected("forbidden_or_unknown_field")
            identifier = region["id"]
            if type(identifier) is not str or not _REGION_ID.fullmatch(identifier) or identifier in ids:
                raise OutboundRejected("invalid_region_id")
            ids.add(identifier)
            if (region["kind"] not in {"numeric_evidence", "context_anchor"}
                    or region["context"] not in {"payment", "excluded"}
                    or region["confidence"] not in {"high", "medium", "low"}
                    or region["status"] != "observed"):
                raise OutboundRejected("invalid_semantic_category")
            amount_id = region["amount_id"]
            if region["kind"] == "numeric_evidence":
                if type(amount_id) is not str or not _AMOUNT_ID.fullmatch(amount_id) or amount_id in amount_ids:
                    raise OutboundRejected("invalid_amount_id")
                amount_ids.add(amount_id)
            elif amount_id is not None:
                raise OutboundRejected("invalid_anchor")
            geometry = region["geometry"]
            if type(geometry) is not dict or set(geometry) != {"x", "y", "width", "height"}:
                raise OutboundRejected("invalid_geometry")
            if not all(type(value) is float and math.isfinite(value) and 0 <= value <= 1 for value in geometry.values()):
                raise OutboundRejected("invalid_geometry")
        for relation in relations:
            if type(relation) is not dict or set(relation) != {"left", "right", "kind"}:
                raise OutboundRejected("forbidden_or_unknown_field")
            if (type(relation["left"]) is not str or type(relation["right"]) is not str
                    or relation["left"] not in ids or relation["right"] not in ids
                    or relation["left"] == relation["right"]
                    or relation["kind"] not in {"row", "column", "overlap"}):
                raise OutboundRejected("invalid_relation")


@dataclass(frozen=True)
class GeminiFreeTierShadowPolicy:
    """A stop switch with no paid mode and no transport implementation."""

    enabled: bool = False
    tier: Literal["free"] = "free"

    def prepare(self, build: AnonymousShadowBuild, observation: OcrObservation) -> bytes:
        if type(self.enabled) is not bool or self.tier != "free" or not self.enabled:
            raise OutboundRejected("free_tier_route_disabled")
        if type(build) is not AnonymousShadowBuild or type(observation) is not OcrObservation:
            raise OutboundRejected("invalid_shadow_handoff")
        return FinalOutboundGate().serialize(build.payload, _private_literals(observation, build.amount_map))
