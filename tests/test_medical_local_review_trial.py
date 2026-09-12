from __future__ import annotations
import ast
import importlib.util
import json
from pathlib import Path
import sys
import pytest

PATH = Path(__file__).resolve().parents[1] / "scripts" / "review_medical_10_unit_local.py"
SPEC = importlib.util.spec_from_file_location("medical_local_review_trial", PATH)
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def test_tool_has_no_network_or_external_writer_imports():
    tree = ast.parse(PATH.read_text(encoding="utf-8"))
    imports = {a.name for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom)) for a in n.names}
    assert not imports.intersection({"requests", "socket", "urllib", "gspread", "SheetsDB"})


def test_amount_validation_reuses_existing_validator():
    assert MODULE._amount("12,345円") == 12345
    with pytest.raises(ValueError, match="invalid_amount_format"):
        MODULE._amount("12x345")


def test_output_inside_repository_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="output_must_be_outside_repository"):
        MODULE._outside_repo(tmp_path / "out", tmp_path)


def test_review_keeps_candidate_and_manual_value_separate_and_zero_write(tmp_path, monkeypatch):
    source = tmp_path / "source.png"; source.write_bytes(b"source")
    digest = MODULE._sha256_bytes(b"source")
    session_path = tmp_path / "session.json"
    session_path.write_text(json.dumps({
        "records": [], "units": [{"unit": 1, "page": 1, "source_path": str(source),
        "source_sha256": digest, "image_sha256": "a" * 64,
        "candidate_origin": "none", "original_candidate": None,
        "stop_reason": "exact_payment_label_unavailable"}]}, ensure_ascii=False), encoding="utf-8")
    answers = iter(["confirm", "1,234円", "n", ""])
    monkeypatch.setattr("builtins.input", lambda _: next(answers))
    MODULE.review(session_path, open_original=False)
    record = json.loads(session_path.read_text(encoding="utf-8"))["records"][0]
    assert record["original_candidate"] is None
    assert record["human_entered_amount"] == record["confirmed_amount"] == 1234
    assert record["candidate_origin"] == "none"
    assert record["production_write"] is False
    assert record["handoff_status"] == "human_review_complete_handoff_unsupported"


def test_wrong_source_binding_is_rejected_before_input(tmp_path):
    source = tmp_path / "source.png"; source.write_bytes(b"changed")
    session_path = tmp_path / "session.json"
    session_path.write_text(json.dumps({"records": [], "units": [{
        "unit": 1, "page": 1, "source_path": str(source), "source_sha256": "0" * 64,
        "image_sha256": "a" * 64, "candidate_origin": "none",
        "original_candidate": None, "stop_reason": "incomplete"}]}), encoding="utf-8")
    with pytest.raises(ValueError, match="source_binding_mismatch"):
        MODULE.review(session_path, open_original=False)
