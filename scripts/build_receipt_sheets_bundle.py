"""Build a local new-mode-only Apps Script ZIP; never upload, authorize or run it."""
import argparse
from hashlib import sha256
import json
from pathlib import Path
import re
import subprocess
import zipfile

ROOT = Path(__file__).resolve().parents[1]
PURE = ("receiptTrim_", "receiptName_", "receiptQuantity_", "receiptOperations_",
        "receiptCanonical_", "receiptSnapshot_", "receiptConnectionDecodeRow_")


def profile_for(binding, script_id, source_commit):
    """Only local supplied observations; never discovers assets or credentials."""
    from app.receipt_sheets_worker import validate_binding
    from app.receipt_sheets_authority import canonical, request_id
    from app.receipt_sheets_owner_check import owner_binding_digest
    validate_binding(binding)
    if binding["enabled"] or not re.fullmatch(r"[a-f0-9]{40}", source_commit or ""):
        raise ValueError("disabled binding and exact worker commit required")
    if not re.fullmatch(r"[A-Za-z0-9_-]{10,150}", script_id or ""):
        raise ValueError("independent script identity required")
    cid, item = next(iter(binding["cases"].items()))
    expected = json.loads(item["expected_json"])
    rid = request_id(expected["machine"][7])
    # Preserve the consumed trial and its fixed binding/digests. These are
    # identifiers, never credentials. They cannot be activation targets here.
    old = {"c4e61a92-7fe2-4e38-84cd-651c4eef314f",
           "1IsGyeAI83NWVOVvJhL11zdl9BFCBWM3iaxxlP47_nhKrD-Ev4AgCD1i2",
           "1pNXXr_FwhadUFpiAOZdubAvrKiU5ZnAngXkL8NhnDJM",
           "1qpsNLmSi3s3uhzOTJrAetwvPT8qJKo2G"}
    if {rid, script_id, binding["spreadsheet_id"], expected["source"]["source_id"]} & old:
        raise ValueError("previous trial is preserved, not reusable")
    def hashed(value):
        return sha256(canonical(value).encode()).hexdigest()
    return dict(schema="receipt-p1-profile-v1", script_id=script_id, request_id=rid,
                worker_sha=source_commit, binding_sha256=owner_binding_digest(binding),
                disabled_sha256=hashed(binding), enabled_sha256=hashed(dict(binding, enabled=True)))


def bundle_files(root=ROOT, *, binding=None, script_id=None, source_commit=None):
    profile = profile_for(binding, script_id, source_commit)
    source = (root / "apps-script/receipt-validation/Code.gs").read_text(encoding="utf-8")
    chunks = re.split(r"(?=^function \w+\()", source, flags=re.MULTILINE)
    declared = [re.match(r"function (\w+)\(", s)[1] for s in chunks[1:]]
    if len(declared) != len(set(declared)):
        raise ValueError("duplicate Apps Script function")
    functions = {re.match(r"function (\w+)\(", s)[1]: s for s in chunks[1:]}
    code = "/** New-mode-only pure cell decoder/snapshot helpers. No legacy journal. */\n"
    code += "\n".join(functions[name].rstrip() for name in PURE) + "\n"
    # The dedicated receiver uses ordinary cells only; omit the legacy text parser.
    code = code.replace("  if (typeof candidate.draft === 'string') return receiptParseDraft_(candidate.draft,hash);",
                        "  if (!candidate.draft || typeof candidate.draft !== 'object') throw Error('receipt_sheets_draft_invalid');")
    files = {"Code.gs": code.encode(), "Sheets.gs": (root / "apps-script/receipt-validation/Sheets.gs").read_text(encoding="utf-8").encode()}
    for name in ("Connection.gs", "appsscript.json"):
        files[name] = (root / "apps-script/receipt-sheets-authority" / name).read_text(encoding="utf-8").encode()
    files["Profile.gs"] = ("/** Independent synthetic trial; generated offline. */\n"
                           "const RECEIPT_P1_PROFILE = Object.freeze(" + json.dumps(profile, sort_keys=True) + ");\n").encode()
    files["P1Audit.gs"] = (root / "apps-script/receipt-sheets-authority/P1Audit.gs").read_text(encoding="utf-8").encode()
    names = []
    for name, raw in files.items():
        if name.endswith('.gs'):
            names.extend(re.findall(r'^function (\w+)\(', raw.decode(), re.MULTILINE))
    if len(names) != len(set(names)):
        raise ValueError("duplicate Apps Script function")
    return files


def build(destination, *, binding, script_id, source_commit):
    files = bundle_files(binding=binding, script_id=script_id, source_commit=source_commit)
    if destination.exists() and any(destination.iterdir()):
        raise ValueError("empty independent output directory required")
    destination.mkdir(parents=True, exist_ok=True)
    for name, raw in files.items():
        (destination / name).write_bytes(raw)
    archive = destination.with_suffix(".zip")
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as z:
        for name, raw in sorted(files.items()):
            info = zipfile.ZipInfo(name, date_time=(2026, 11, 16, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(info, raw)
    proof = dict(source_commit=source_commit, zip_sha256=sha256(archive.read_bytes()).hexdigest(),
                 files={n: sha256(b).hexdigest() for n, b in files.items()},
                 oauth_scopes=json.loads(files["appsscript.json"])["oauthScopes"],
                 deployed=False, authorized=False)
    destination.with_suffix(".manifest.json").write_text(json.dumps(proof, indent=2), encoding="utf-8")
    return proof


if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(ROOT))
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--commit", required=True, help="Exact clean local review commit; no network")
    parser.add_argument("--binding", type=Path, required=True, help="Private disabled binding JSON; never printed")
    parser.add_argument("--script-id", required=True, help="Independent trial Script ID")
    args = parser.parse_args()
    git = ['git', '--git-dir=.git', '--work-tree=.']
    def read(*a): return subprocess.check_output(git + list(a), cwd=ROOT).decode().strip()
    if not re.fullmatch(r'[a-f0-9]{40}', args.commit) or read('rev-parse', 'HEAD') != args.commit:
        raise SystemExit('exact local commit required')
    if read('status', '--porcelain'):
        raise SystemExit('clean review checkout required')
    print(json.dumps(build(args.destination, source_commit=args.commit,
                           binding=json.loads(args.binding.read_text(encoding='utf-8')), script_id=args.script_id)))
