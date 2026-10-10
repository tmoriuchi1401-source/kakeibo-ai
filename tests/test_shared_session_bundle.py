"""Credential-free checks of the reviewed, existing-container-only delta."""
from copy import deepcopy
from hashlib import sha256
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("session_bundle", ROOT / "auth-session/compose.py")
BUNDLE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BUNDLE)


@pytest.mark.parametrize("path", ["../secret", "/secret", "a/../../secret", "a\\secret", "C:/secret", "C:secret", "./secret", ""])
def test_composer_rejects_unreviewed_paths(path):
    with pytest.raises(ValueError, match="bundle_path_invalid"):
        BUNDLE.checked_path(path)


def test_patch_additions_and_scope_are_exactly_hash_pinned():
    manifest = BUNDLE.MANIFEST
    assert manifest["baseline_commit"] == "c29ecc44e1fb9fcc854cde1da5cce940e633e8fa"
    assert len(manifest["baseline_files"]) == 40
    assert len(manifest["added_files"]) == 6
    patch = BUNDLE.normalized(BUNDLE.HERE / "existing-service.patch")
    assert sha256(patch).hexdigest() == manifest["patch_sha256"]
    paths = {line.split()[2][2:] for line in patch.decode().splitlines() if line.startswith("diff --git ")}
    assert paths == set(manifest["changed_files"])
    assert paths <= set(manifest["baseline_files"])
    assert set(manifest["added_files"]) == {
        "services/human_general/shared_login.py",
        "services/human_general/session_canary.py",
        "services/human_general/session_fixture.json",
        "services/human_general/generic_intake.py",
        "services/human_general/generic_review.py",
        "services/human_general/generic_archive.py",
    }
    assert set(manifest['repository_files'])=={'app/pdf_intake_authority.py','app/pdf_intake_registry.py'}
    for name,expected in manifest['repository_files'].items():
        assert sha256(BUNDLE.normalized(ROOT/name)).hexdigest()==expected
    for name, expected in manifest["added_files"].items():
        assert sha256(BUNDLE.normalized(BUNDLE.HERE / Path(name).name)).hexdigest() == expected
    assert not {"app/sheets.py", "app/receipt_pipeline.py", "app/medical_manual.py", ".env"} & set(manifest["baseline_files"])


def test_changed_baseline_fails_before_patch_or_any_external_call(tmp_path, monkeypatch):
    manifest = deepcopy(BUNDLE.MANIFEST)
    manifest["baseline_files"] = {"app/__init__.py": "0" * 64}
    monkeypatch.setattr(BUNDLE, "MANIFEST", manifest)
    monkeypatch.setattr(BUNDLE, "git", lambda *args: b"changed")
    monkeypatch.setattr(BUNDLE.subprocess, "run", lambda *args, **kwargs: pytest.fail("patch must not execute"))
    with pytest.raises(ValueError, match="deployed_baseline_hash_changed"):
        BUNDLE.compose(tmp_path / "new")


def test_changed_patch_fails_without_fallback(tmp_path, monkeypatch):
    manifest = deepcopy(BUNDLE.MANIFEST)
    manifest["baseline_files"] = {}
    manifest["patch_sha256"] = "0" * 64
    monkeypatch.setattr(BUNDLE, "MANIFEST", manifest)
    monkeypatch.setattr(BUNDLE.subprocess, "run", lambda *args, **kwargs: pytest.fail("patch must not execute"))
    with pytest.raises(ValueError, match="bundle_patch_changed"):
        BUNDLE.compose(tmp_path / "new")


def test_existing_output_is_never_overwritten(tmp_path):
    target = tmp_path / "existing"
    target.mkdir()
    marker = target / "retained"
    marker.write_bytes(b"preserve")
    with pytest.raises(FileExistsError):
        BUNDLE.compose(target)
    assert marker.read_bytes() == b"preserve"
