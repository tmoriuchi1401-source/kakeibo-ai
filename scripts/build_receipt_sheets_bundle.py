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


def bundle_files(root=ROOT):
    source = (root / "apps-script/receipt-validation/Code.gs").read_text(encoding="utf-8")
    chunks = re.split(r"(?=^function \w+\()", source, flags=re.MULTILINE)
    functions = {re.match(r"function (\w+)\(", s)[1]: s for s in chunks[1:]}
    code = "/** New-mode-only pure cell decoder/snapshot helpers. No legacy journal. */\n"
    code += "\n".join(functions[name].rstrip() for name in PURE) + "\n"
    # The dedicated receiver uses ordinary cells only; omit the legacy text parser.
    code = code.replace("  if (typeof candidate.draft === 'string') return receiptParseDraft_(candidate.draft,hash);",
                        "  if (!candidate.draft || typeof candidate.draft !== 'object') throw Error('receipt_sheets_draft_invalid');")
    files = {"Code.gs": code.encode(), "Sheets.gs": (root / "apps-script/receipt-validation/Sheets.gs").read_text(encoding="utf-8").encode()}
    for name in ("Connection.gs", "appsscript.json"):
        files[name] = (root / "apps-script/receipt-sheets-authority" / name).read_text(encoding="utf-8").encode()
    return files


def build(destination, *, source_commit=None):
    files = bundle_files()
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
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--commit", required=True, help="Exact clean local review commit; no network")
    args = parser.parse_args()
    git = ['git', '--git-dir=.git', '--work-tree=.']
    def read(*a): return subprocess.check_output(git + list(a), cwd=ROOT).decode().strip()
    if not re.fullmatch(r'[a-f0-9]{40}', args.commit) or read('rev-parse', 'HEAD') != args.commit:
        raise SystemExit('exact local commit required')
    if read('status', '--porcelain'):
        raise SystemExit('clean review checkout required')
    print(json.dumps(build(args.destination, source_commit=args.commit)))
