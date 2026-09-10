"""One-shot synthetic connectivity check; prints data-free counters only."""
from __future__ import annotations

import json

from app.medical_ai_structured_shadow import (
    GeminiStructuredSyntheticTransport,
    StructuredShadowPolicy,
    build_structured_shadow_payload,
    run_structured_shadow,
)
from app.medical_gemini_shadow import UrllibHttpExecutor
from app.medical_ocr_observation_shadow import ReceiptImage, make_observation


class CountingExecutor:
    def __init__(self) -> None:
        self.attempts = 0
        self._delegate = UrllibHttpExecutor()
        self.last_response = None

    def execute(self, request):
        self.attempts += 1
        self.last_response = self._delegate.execute(request)
        return self.last_response


def main() -> None:
    image = ReceiptImage("synthetic-connectivity-fixture", 1, b"synthetic-fixture-bytes")
    observation = make_observation(
        image,
        "rapidocr-shadow",
        ("a" * 64,),
        300,
        200,
        [
            {
                "text": "領収金額",
                "polygon": [(10, 10), (90, 10), (90, 20), (10, 20)],
                "confidence": 0.96,
                "detection_confidence": 0.98,
            },
            {
                "text": "1,200円",
                "polygon": [(100, 11), (160, 11), (160, 21), (100, 21)],
                "confidence": 0.94,
                "detection_confidence": 0.97,
            },
        ],
    )
    build = build_structured_shadow_payload(observation, source_kind="synthetic_fixture")
    executor = CountingExecutor()
    result = run_structured_shadow(
        build,
        observation,
        GeminiStructuredSyntheticTransport(executor=executor),
        StructuredShadowPolicy(transport_enabled=True, kill_switch_engaged=False),
    )
    response = executor.last_response
    envelope_candidate_count = 0
    envelope_part_count = 0
    envelope_part_shapes: list[str] = []
    if response is not None and response.body:
        try:
            envelope = json.loads(response.body)
            candidates = envelope.get("candidates", []) if type(envelope) is dict else []
            envelope_candidate_count = len(candidates) if type(candidates) is list else -1
            parts = candidates[0].get("content", {}).get("parts", []) if candidates else []
            envelope_part_count = len(parts) if type(parts) is list else -1
            envelope_part_shapes = [
                "+".join(sorted(part)) if type(part) is dict else "non_object"
                for part in parts
            ]
        except Exception:
            envelope_candidate_count = -1
            envelope_part_count = -1
            envelope_part_shapes = ["invalid_json"]
    print(
        json.dumps(
            {
                "fixture_kind": "synthetic_fixture",
                "status": result.status,
                "reason_code": result.reason_code,
                "external_ai_http_attempts": executor.attempts,
                "http_status": response.status_code if response is not None else 0,
                "http_failure": response.failure if response is not None else "no_response",
                "response_bytes": len(response.body) if response is not None else 0,
                "envelope_candidate_count": envelope_candidate_count,
                "envelope_part_count": envelope_part_count,
                "envelope_part_shapes": envelope_part_shapes,
                "production_authorized": result.production_authorized,
                "write_authorized": result.write_authorized,
            },
            separators=(",", ":"),
        )
    )


if __name__ == "__main__":
    main()
