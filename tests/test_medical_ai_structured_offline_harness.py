"""Static safety contract for the real-medical offline aggregate harness."""
from __future__ import annotations

import ast
from pathlib import Path


def test_offline_harness_has_no_transport_or_sensitive_output_fields():
    path = Path(__file__).resolve().parents[1] / "scripts" / "evaluate_medical_ai_structured_offline.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imports = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }
    assert not any(name in imports for name in {
        "GeminiStructuredSyntheticTransport", "UrllibHttpExecutor", "requests", "socket",
        "SheetsDB",
    })
    output_keys = {
        key.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Dict)
        for key in node.keys
        if isinstance(key, ast.Constant) and isinstance(key.value, str)
    }
    assert output_keys.issuperset({
        "selected_files", "receipt_units", "observed_units", "incomplete_units",
        "anonymous_payload_generated", "real_medical_outbound_rejected",
        "level2_connected", "evidence_binding_possible", "review_first_required", "production_authorized",
        "write_authorized", "external_ai_http_attempts", "drive_sheets_writes",
    })
    assert not output_keys.intersection({
        "filename", "path", "ocr_text", "amount", "token_id", "geometry", "source_id",
    })
