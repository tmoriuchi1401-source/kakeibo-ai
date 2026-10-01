"""Follow a captured bank meaning answer with the existing durable bank runner.

The shared category-request workflow already holds kakeibo-production. No new
writer, state store, source identity or historical settlement grant is created.
Only the bank runner is invoked; global accounting stages are not selected.
"""
from pathlib import Path
import subprocess
import tempfile
from uuid import UUID

from .drive_run_state import DriveStateTransport, StateBinding, StateError
from .production_flow import REPO, assemble, verify_execution_boundary
from .production_ledger import ProductionLedger
from .production_run import COUNT_KEYS

WORKFLOW_REF = "tmoriuchi1401-source/kakeibo-ai/.github/workflows/category-sheet-request.yml@refs/heads/main"


def require_request_context(env, head, request_id):
    verify_execution_boundary(env, head)
    try:
        canonical = str(UUID(request_id))
    except (ValueError, TypeError, AttributeError):
        raise StateError("bank_review_replay_request_invalid") from None
    if (canonical != request_id or env.get("CATEGORY_REQUEST_ID") != request_id
            or env.get("GITHUB_EVENT_NAME") != "workflow_dispatch"
            or env.get("GITHUB_WORKFLOW_REF") != WORKFLOW_REF
            or env.get("KAKEIBO_LEGACY_DISABLED") != "true"
            or env.get("BANK_REVIEW_ENABLED") != "true"
            or env.get("BANK_REVIEW_REPLAY_ENABLED") != "true"):
        raise StateError("bank_review_replay_request_invalid")


def run_bank_review_replay(env, *, request_id, confirmed_groups):
    """Run after metadata read-back, once, under unchanged production authority."""
    if env.get("BANK_REVIEW_REPLAY_ENABLED", "false") != "true":
        return {"bank_replay_disabled": 1}
    if type(confirmed_groups) is not int or confirmed_groups < 1:
        raise StateError("bank_review_replay_no_confirmed_answer")
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPO, text=True).strip()
    require_request_context(env, head, request_id)
    from .private_state_bindings import decode_environment
    from .google_clients import drive_service
    prepared, _ = decode_environment(dict(env))
    prepared["BANK_REVIEW_REQUEST_ID"] = request_id
    binding = StateBinding("production_run", prepared.get("SPREADSHEET_ID", ""),
        prepared.get("KAKEIBO_STATE_FOLDER_ID", ""), prepared.get("KAKEIBO_RUN_LEDGER_FILE_ID", ""))
    ledger = ProductionLedger(DriveStateTransport(drive_service(), binding), binding)
    with tempfile.TemporaryDirectory(prefix="bank-review-replay-") as directory:
        result = assemble(prepared, Path(directory), apply=True, bank_apply=True, ledger=ledger)["bank"]()
    # Re-read the durable parent marker. An ambiguous state save never authorizes
    # a retry, a successful request status, or archive by this wrapper.
    final = ProductionLedger(ledger.transport, binding)
    if final.value["sources"]["bank"]["phase"] != "ready":
        raise StateError("source_reconciliation_required")
    counts = {"bank_replay_" + key: value for key, value in result.items()
              if key in COUNT_KEYS and type(value) is int and value >= 0}
    counts["bank_ledger_writes"] = int(result.get("written", 0)) + int(result.get("deposit_imports_created", 0))
    counts["bank_replay_completed"] = 1
    return counts
