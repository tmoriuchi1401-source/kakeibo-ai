"""The live harness is synthetic-only and emits no evidence values."""
from __future__ import annotations

import ast
from pathlib import Path


def test_harness_has_fixed_synthetic_source_and_data_free_output():
    path = Path(__file__).resolve().parents[1] / "scripts" / "run_synthetic_medical_ai_structured_shadow.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    source_kinds = {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
        and node.value in {"synthetic_fixture", "real_medical"}
    }
    assert source_kinds == {"synthetic_fixture"}
    printed_keys = {
        key.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Dict)
        for key in node.keys
        if isinstance(key, ast.Constant) and isinstance(key.value, str)
    }
    assert printed_keys >= {
        "fixture_kind", "status", "reason_code", "external_ai_http_attempts",
        "http_status", "http_failure", "response_bytes", "production_authorized",
        "envelope_candidate_count", "envelope_part_count", "envelope_part_shapes",
        "write_authorized",
    }
    assert not printed_keys.intersection(
        {"amount", "token_id", "document_id", "unit_ref", "ocr_text", "geometry"}
    )
