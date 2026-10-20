"""Read source text only. This is a census/change detector, not a gate or proof.

No application imports, credentials, network, writers or historical code execution.
Git supplies immutable source text; dynamic dispatch remains a manual-review limit.
"""
from __future__ import annotations

import ast
from collections import Counter
import hashlib
import io
import json
from pathlib import Path
import re
import subprocess
import tarfile

ROOT = Path(__file__).resolve().parents[1]
BASE = "7e13f6034b20ad75719ac553e031ae0813e8a3d4"
HISTORICAL = "c29ecc44e1fb9fcc854cde1da5cce940e633e8fa"
METHODS = {
    "append", "append_raw", "update_row", "update_row_raw", "update_rows",
    "update_expense_fields", "update_expense_categories", "set_raw_range",
    "clear", "batchUpdate", "update", "delete", "create", "set", "save",
    "write", "replace", "transition", "reserve", "complete", "write_once",
    "write_batch_once", "release_after_reconciliation", "_materialize_result",
    "_write_accounting_plan", "process_bytes", "post_normal", "post_manual",
}
ALLOWED = {
    'staging_shadow/ledger_execution_boundary.py', 'tests/ledger_execution_fixtures.py',
    'tests/test_ledger_execution_boundary.py', 'tests/test_ledger_execution_static.py',
    'docs/r2-2-m5-m6.md', 'docs/r2-2-m5-m6-inventory.json', '.github/workflows/r2-2-m5-m6.yml',
    'staging_shadow/mandatory_receipt_gate.py', 'tests/mandatory_gate_fixtures.py',
    'tests/test_mandatory_receipt_gate.py', 'tests/test_mandatory_gate_static.py',
    'docs/r2-2-mandatory-gate.md', '.github/workflows/r2-2-mandatory-gate.yml',
    'tests/test_iphone_mvp_r0_adoption.py', 'tests/test_iphone_draft_request_design.py',
    "docs/r2-1-writer-audit.md", "docs/r2-1-ingresses.json",
    "docs/r2-1-static-census.json", "docs/r2-1-pr-baseline.json",
    "tests/r2_1_static_audit.py", "tests/test_r2_1_static_audit.py",
    "tests/test_production_workflows.py",
    ".github/workflows/r2-1-static-audit.yml",
}
# Exact documentation/metadata additions authorized for the R2-3 read-only audit.
ALLOWED.update({
    'docs/r2-3-actions.md',
    'docs/r2-3-cloud-host.md',
    'docs/r2-3-ingresses.json',
    'docs/r2-3-ingresses.md',
    'docs/r2-3-minimal-adapters.md',
    'docs/r2-3-read-only-audit.md',
    'docs/r2-3-validation.md',
    'docs/r2-3-evidence/active-runs-end.json',
    'docs/r2-3-evidence/active-runs-start.json',
    'docs/r2-3-evidence/latest-operational-jobs.json',
    'docs/r2-3-evidence/local-scheduler.json',
    'docs/r2-3-evidence/operational-history.json',
    'docs/r2-3-evidence/pr-baseline.json',
    'docs/r2-3-evidence/pr-final.json',
    'docs/r2-3-evidence/production-last-executed.json',
    'docs/r2-3-evidence/production-success-job.json',
    'docs/r2-3-evidence/recent-runs.json',
    'docs/r2-3-evidence/requests-start.json',
    'docs/r2-3-evidence/workflow-definitions.json',
    'docs/r2-3-evidence/workflows.json',
})



def git(*args):
    return subprocess.check_output(
        ["git", "-c", "safe.directory=" + ROOT.as_posix(), "-C", str(ROOT), *args]
    )


def paths(ref):
    return git("ls-tree", "-r", "--name-only", ref).decode().splitlines()


def source(ref, path):
    return git("show", f"{ref}:{path}").decode("utf-8-sig")


def digest(text):
    return hashlib.sha256(text.replace("\r\n", "\n").encode()).hexdigest()


class Calls(ast.NodeVisitor):
    def __init__(self):
        self.stack = []
        self.calls = []

    def scope(self, node):
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()

    visit_ClassDef = scope
    visit_FunctionDef = scope
    visit_AsyncFunctionDef = scope

    def visit_Call(self, node):
        target = ast.unparse(node.func)
        leaf = target.rsplit(".", 1)[-1]
        if leaf in METHODS:
            # Keep even list.append/dict.update: no type inference silently
            # discards a possible sink. Arguments are source expressions only.
            self.calls.append({"line": node.lineno,
                               "caller": ".".join(self.stack) or "<module>",
                               "call": target,
                               "args": [ast.unparse(a) for a in node.args],
                               "keywords": [k.arg for k in node.keywords]})
        self.generic_visit(node)


def scan_python(text):
    visitor = Calls()
    visitor.visit(ast.parse(text))
    return sorted(visitor.calls, key=lambda c: (c["line"], c["call"]))


def scan_refs(text):
    pattern = r"ref:|cron:|python -m app\.|test_tree|baseline_commit|git.*(archive|show)|workflow_dispatch|workflow_call|schedule:|concurrency:|group:|KAKEIBO_.*(ENABLED|DISABLED|SHA)"
    return [{"line": n, "text": line.strip()} for n, line in enumerate(text.splitlines(), 1)
            if re.search(pattern, line)]


def census(ref=BASE):
    result = {"ref": ref, "python": {}, "execution_refs": {}, "scripts": {}, "cli_commands": []}
    with tarfile.open(fileobj=io.BytesIO(git("archive", ref))) as archive:
        files = {m.name: archive.extractfile(m).read().decode("utf-8-sig")
                 for m in archive.getmembers() if m.isfile()
                 and m.name.endswith((".py", ".json", ".yml", ".yaml", ".gs", ".js", ".cjs", ".sh", ".ps1"))}
    for path, text in sorted(files.items()):
        if path.endswith(".py") and path.startswith(("app/", "staging_shadow/", "auth-session/", "services/")):
            result["python"][path] = {"sha256_lf": digest(text), "calls": scan_python(text)}
            if path == 'app/cli.py':
                for node in ast.walk(ast.parse(text)):
                    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                            and node.func.attr == 'add_parser' and node.args):
                        result['cli_commands'].append({'line': node.lineno,
                                                       'command': ast.unparse(node.args[0])})
                result['cli_commands'].sort(key=lambda r: r['line'])
        if path.startswith(".github/workflows/") or path in {"auth-session/baseline.json", "auth-session/compose.py"}:
            result["execution_refs"][path] = {"sha256_lf": digest(text), "references": scan_refs(text)}
        if path.endswith((".gs", ".js", ".cjs", ".sh", ".ps1")) and not path.startswith("tests/"):
            result["scripts"][path] = {"sha256_lf": digest(text), "mutations": [
                {"line": n, "text": line.strip()} for n, line in enumerate(text.splitlines(), 1)
                if re.search(r"appendRow|setValue|setValues|batchUpdate|UrlFetchApp|workflow|\.post\(|\.put\(|\.delete\(", line)]}
    return result


def stats(c):
    calls = [call for file in c["python"].values() for call in file["calls"]]
    return {"python_files": len(c["python"]), "candidate_calls": len(calls),
            "workflow_and_reference_files": len(c["execution_refs"]),
            "scripts": len(c["scripts"]),
            "methods": dict(sorted(Counter(x["call"].rsplit(".", 1)[-1] for x in calls).items()))}


if __name__ == "__main__":
    current = census()
    historical = census(HISTORICAL)
    output = {"schema": "r2-1-static-census-v1", "limits":
              "Lexical candidates include in-memory calls. No dynamic reachability, deployment or runtime safety proof.",
              "baseline": current, "historical": historical,
              "counts": {"baseline": stats(current), "historical": stats(historical)}}
    def compact_records(value, depth=0):
        if not isinstance(value, dict) or depth >= 3:
            return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        pad = '  ' * depth
        return '{\n' + ',\n'.join('  ' + pad + json.dumps(k) + ': ' + compact_records(v, depth + 1)
                                    for k, v in value.items()) + '\n' + pad + '}'

    # One file record per line keeps this generated source index reviewable.
    (ROOT / "docs/r2-1-static-census.json").write_text(
        compact_records(output) + "\n", encoding="utf-8")
    print(json.dumps(output["counts"]))
