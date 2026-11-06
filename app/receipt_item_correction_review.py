"""Pure one-tab review/registration contract, NOT a live request endpoint.

Trusted backend readers must supply source, candidate rows, master/rules and
prior owner confirmation independently of editable cells. Digests detect stale
input; they are not authentication, source proof, authority or a signature.
The Sheets/OIDC/event transport is deliberately not provisioned here.
"""
from copy import deepcopy
import re

from .page_receipt_model import digest
from .receipt_item_correction_plan import preview_item_corrections

STATES = (
    "編集中・未登録", "登録受付済み", "検証中", "修正が必要",
    "検証済み・未記帳", "記帳完了", "結果不明・照合待ち",
)
REGISTER = "修正内容を登録する"


def review(tx, indexed_rows, source, operations, categories, *,
           total_confirmation=None, approved_product_rules=()):
    """Return one case projection and a snapshot for a future trusted host.

    source.sha256 is the ORIGINAL bytes hash. tx.row[10] is the ANALYSIS
    hash and must never be substituted for the original's hash/version.
    total_confirmation comes from a trusted owner-confirmation store, not UI.
    Row numbers are presentation coordinates only; identity is case/source.
    """
    required = {"source_id", "sha256", "version", "mime_type", "classification", "transaction_kind"}
    categories = list(categories)
    approved_product_rules = tuple(approved_product_rules)
    if (not isinstance(source, dict) or set(source) != required
            or source["source_id"] != tx.row[3]
            or not re.fullmatch("[A-Za-z0-9_-]{10,128}", str(source["source_id"]))
            or not isinstance(source["sha256"], str)
            or not re.fullmatch("[0-9a-f]{64}", source["sha256"])
            or not isinstance(source["version"], str) or not source["version"]
            or source["mime_type"] not in {"image/jpeg", "image/png", "image/heic", "image/heif"}
            or source["classification"] != "normal" or source["transaction_kind"] != "purchase"):
        raise ValueError("original_identity_or_purpose_invalid")
    confirmed = bool(isinstance(total_confirmation, dict)
                     and set(total_confirmation) == {"source", "receipt_total", "actor_id"}
                     and total_confirmation["source"] == source
                     and type(total_confirmation["receipt_total"]) is int
                     and total_confirmation["receipt_total"] == tx.amount
                     and isinstance(total_confirmation["actor_id"], str)
                     and re.fullmatch("[0-9a-f]{64}", total_confirmation["actor_id"]))
    preview = preview_item_corrections(
        tx, indexed_rows, operations, categories, total_confirmed=confirmed,
        approved_product_rules=approved_product_rules)
    snapshot = dict(schema="receipt-item-correction-review-v1", case_id=tx.import_id,
                    source=deepcopy(source), operations=deepcopy(operations),
                    preview_digest=preview["snapshot_digest"],
                    validation_digest=digest(preview["issues"]),
                    classification_digest=digest([sorted(map(list, categories)),
                                                  [r.to_row() for r in approved_product_rules]]),
                    total_confirmation_digest=digest(total_confirmation))
    snapshot["digest"] = digest(snapshot)
    difference = tx.amount - preview["original_item_total"]
    return dict(
        sheet="確認", case_id=tx.import_id, state=STATES[0],
        original_link="https://drive.google.com/file/d/" + source["source_id"] + "/view",
        receipt_total=tx.amount, original_item_total=preview["original_item_total"],
        difference=difference, difference_kind="不足" if difference > 0 else "超過",
        calculation=f"{tx.amount:,} − {preview['original_item_total']:,} = {difference:+,}円",
        total_origin="原本の支払総額欄・本人照合済み" if confirmed else "原本の支払総額欄・本人未確認",
        item_total_origin="保存済みAI商品明細のamountを整数円で合算（数量を再乗算しない）",
        original_item_calculation=" + ".join(str(i["amount"]) for i in preview["original_items"])
                                 + f" = {preview['original_item_total']}円",
        corrected_calculation=" + ".join(str(i["amount"]) for i in preview["items"])
                              + f" = {preview['item_total']}円",
        total_confirmation_required=not confirmed,
        reason="原因未特定。原本で商品・税・値引きを確認。特定できなければ保留。",
        preview=preview, snapshot=snapshot, registration_label=REGISTER,
        posting_authority=False, financial_write_count=0,
    )


def registration_plan(submitted, fresh_review):
    """Validate a captured snapshot against an independently rebuilt review.

    Pure validation only. A caller MUST authenticate/authorize the owner and
    bind the request ID/session/CSRF/expiry to this exact snapshot separately.
    There is no Sheet polling, trigger, writer call or retry in this function.
    """
    if submitted != fresh_review["snapshot"]:
        raise ValueError("correction_registration_snapshot_stale")
    preview = fresh_review["preview"]
    return dict(state=STATES[4] if preview["status"] == "preview_balanced" else STATES[3],
                issues=deepcopy(preview["issues"]), snapshot_digest=submitted["digest"],
                items=deepcopy(preview["items"]), receipt_total=preview["receipt_total"],
                posting_authority=False, financial_write_count=0)
