"""Offline replay of the fixed ten Medical amount+issuer combinations."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys


REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from app.medical_image_ai_admission_shadow import build_image_ai_admission_signals
from app.medical_image_ai_result_shadow import (
    ANSWER_SCHEMA_VERSION,
    ImageAiProvenance,
    build_image_ai_shadow_result,
)
from app.medical_transaction_combined_shadow import (
    MedicalDocumentBinding,
    combine_medical_transaction_shadow,
    facility_ocr_evidence_sha256,
)
from scripts.evaluate_medical_issuer_selector_local import (
    _ground_truth as facility_ground_truth,
    _ocr_pages,
    _session_bindings,
)
from scripts.replay_medical_image_ai_admission_local import fixed_policy
from scripts.replay_medical_image_ai_results_local import (
    ReplayRejected,
    _binding,
    _digest,
    _human_ground_truth,
    _overrides,
    _read_json,
    _sha256_bytes,
    _strict_int,
    hmac_compare,
)
from app.medical_issuer_selector_shadow import select_medical_issuer_shadow


def replay_combined(
    *,
    artifact_root: Path,
    ocr_path: Path,
    human_session_path: Path,
    facility_ground_truth_path: Path,
    amount_ground_truth_overrides: dict[int, int],
) -> dict:
    artifact_root = artifact_root.resolve()
    manifest_path = artifact_root / "manifest.json"
    manifest_bytes = manifest_path.read_bytes()
    manifest_sha256 = _sha256_bytes(manifest_bytes)
    manifest = _read_json(manifest_path)
    if (
        manifest.get("schema_version") != "medical-final-images-local-v1"
        or manifest.get("human_review_complete") is not True
        or manifest.get("external_http") != 0
        or manifest.get("external_ai_requests") != 0
        or manifest.get("production_writes") != 0
        or manifest.get("drive_writes") != 0
        or manifest.get("sheets_writes") != 0
    ):
        raise ReplayRejected("invalid_manifest_state")
    records = manifest.get("records")
    if type(records) is not list or len(records) != 10:
        raise ReplayRejected("invalid_fixed_unit_set")

    ocr_raw = _read_json(ocr_path.resolve())
    human_session = _read_json(human_session_path.resolve())
    facility_truth_raw = _read_json(facility_ground_truth_path.resolve())
    pages = _ocr_pages(ocr_raw)
    session_bindings = _session_bindings(human_session)
    amount_truth = _human_ground_truth(human_session)
    facility_truth = facility_ground_truth(facility_truth_raw)

    amount_key = hashlib.sha256(b"local-shadow-replay-v1\0" + manifest_bytes).digest()
    combination_key = hashlib.sha256(
        b"local-combined-shadow-replay-v1\0"
        + manifest_bytes
        + _read_json(ocr_path.resolve())["schema_version"].encode("ascii")
    ).digest()
    policy = fixed_policy()
    rows = []
    seen = set()
    for record in records:
        if type(record) is not dict:
            raise ReplayRejected("invalid_manifest_binding")
        unit = _strict_int(record.get("unit"))
        if unit in seen:
            raise ReplayRejected("duplicate_manifest_unit")
        seen.add(unit)
        if (
            record.get("metadata_removed") is not True
            or record.get("human_send_review") != "APPROVED_FOR_AI_EVAL"
            or record.get("review_checks")
            != {
                "A_content": True,
                "B_privacy": True,
                "C_qr_barcode": True,
                "D_boundary": True,
            }
        ):
            raise ReplayRejected("crop_not_approved")
        output_file = record.get("output_file")
        if type(output_file) is not str or Path(output_file).name != output_file:
            raise ReplayRejected("invalid_manifest_binding")
        crop_bytes = (artifact_root / output_file).read_bytes()
        if not hmac_compare(
            _sha256_bytes(crop_bytes), _digest(record.get("output_sha256"))
        ):
            raise ReplayRejected("crop_digest_mismatch")

        session_binding = session_bindings[unit]
        if any(
            (
                record.get("source_sha256") != session_binding.source_sha256,
                record.get("source_image_sha256") != session_binding.image_sha256,
                record.get("page") != session_binding.page,
            )
        ):
            raise ReplayRejected("materialization_lineage_mismatch")
        expected_document = MedicalDocumentBinding(
            source_sha256=record["source_sha256"],
            source_image_sha256=record["source_image_sha256"],
            unit=unit,
            page=record["page"],
        )

        unit_dir = artifact_root / "ai-results" / f"unit-{unit:02d}"
        answer = _read_json(unit_dir / "answer.json")
        attempt = _read_json(unit_dir / "attempt.json")
        execution = _read_json(unit_dir / "result.json")
        if (
            attempt.get("unit") != unit
            or attempt.get("image_sha256") != record.get("output_sha256")
            or attempt.get("origin") != "human_approved_final_image"
            or execution.get("unit") != unit
            or execution.get("exit_code") != 0
            or execution.get("status") != answer.get("status")
            or execution.get("candidate_present")
            is not (answer.get("status") == "readable")
            or execution.get("tool_calls") != 0
        ):
            raise ReplayRejected("result_attempt_binding_mismatch")
        provenance = ImageAiProvenance.safe_validate(
            {
                "answer_schema_version": ANSWER_SCHEMA_VERSION,
                "model": attempt.get("model"),
                "prompt_sha256": attempt.get("prompt_sha256"),
                "input_mode": "fresh_codex_exec_image",
                "approval_ref": attempt.get("approval"),
            }
        )
        amount_binding = _binding(record, manifest_sha256)
        amount_result = build_image_ai_shadow_result(
            raw_answer=answer,
            binding=amount_binding,
            provenance=provenance,
            identity_key=amount_key,
        )
        amount_signals = build_image_ai_admission_signals(
            result=amount_result,
            raw_answer=answer,
            observed_candidate_amounts=(amount_result.amount_yen,),
            manual_conflict_state="none_known",
            identity_key=amount_key,
        )

        facility_page = pages[unit]
        facility_result = select_medical_issuer_shadow(
            facility_page, expected_binding=session_binding
        )
        facility_digest = facility_ocr_evidence_sha256(facility_page)
        combined = combine_medical_transaction_shadow(
            amount_result=amount_result,
            amount_signals=amount_signals,
            current_amount_binding=amount_binding,
            current_amount_provenance=provenance,
            amount_crop_bytes=crop_bytes,
            amount_identity_key=amount_key,
            amount_policy=policy,
            facility_page=facility_page,
            facility_result=facility_result,
            expected_document_binding=expected_document,
            expected_facility_evidence_sha256=facility_digest,
            combination_identity_key=combination_key,
        )

        # Ground truth is read only after the combined candidate exists.
        amount_adjudication = amount_truth[unit]
        amount_expected = amount_ground_truth_overrides.get(
            unit, amount_adjudication["amount"]
        )
        facility_adjudication = facility_truth[unit]
        amount_match = (
            combined.amount is not None
            and combined.amount.amount_yen == amount_expected
        )
        facility_match = (
            combined.facility is not None
            and combined.facility.facility_region_ordinal
            == facility_adjudication["selected_issuer_region_ordinal"]
            and combined.facility.facility_name
            == facility_adjudication["issuer_ocr_text"]
        )
        rows.append(
            {
                "unit": unit,
                "page": expected_document.page,
                "amount_yen": combined.amount.amount_yen if combined.amount else None,
                "amount_origin": (
                    combined.amount.amount_origin if combined.amount else None
                ),
                "amount_result_id": (
                    combined.amount.image_ai_result_id if combined.amount else None
                ),
                "amount_provenance": (
                    combined.amount.image_ai_provenance.model_dump(mode="json")
                    if combined.amount
                    else None
                ),
                "amount_admission": (
                    combined.amount.admission_status if combined.amount else None
                ),
                "facility_name": (
                    combined.facility.facility_name if combined.facility else None
                ),
                "facility_origin": (
                    combined.facility.facility_origin if combined.facility else None
                ),
                "facility_region_ordinal": (
                    combined.facility.facility_region_ordinal
                    if combined.facility
                    else None
                ),
                "facility_type": (
                    combined.facility.facility_type if combined.facility else None
                ),
                "facility_provenance": (
                    {
                        "selector_version": combined.facility.issuer_selector_version,
                        "ocr_evidence_sha256": combined.facility.facility_ocr_evidence_sha256,
                    }
                    if combined.facility
                    else None
                ),
                "referenced_providers": (
                    [
                        item.model_dump(mode="json")
                        for item in combined.facility.referenced_providers
                    ]
                    if combined.facility
                    else []
                ),
                "combined_binding": combined.binding.model_dump(mode="json"),
                "authority_ready": combined.authority_ready,
                "verdict": combined.verdict,
                "review_reasons": list(combined.review_reasons),
                "amount_ground_truth_match": amount_match,
                "facility_ground_truth_match": facility_match,
                "pair_ground_truth_match": amount_match and facility_match,
                "production_authority": combined.production_authority,
                "write_authority": combined.write_authority,
            }
        )
    rows.sort(key=lambda item: item["unit"])
    if seen != set(range(1, 11)):
        raise ReplayRejected("invalid_fixed_unit_set")
    return {
        "schema_version": "medical-transaction-combined-local-replay-v1",
        "ground_truth_used_for_combination": False,
        "units": rows,
        "summary": {
            "unit_count": 10,
            "authority_ready": sum(item["authority_ready"] for item in rows),
            "requires_human_review": sum(
                not item["authority_ready"] for item in rows
            ),
            "amount_ground_truth_matches": sum(
                item["amount_ground_truth_match"] for item in rows
            ),
            "facility_ground_truth_matches": sum(
                item["facility_ground_truth_match"] for item in rows
            ),
            "pair_ground_truth_matches": sum(
                item["pair_ground_truth_match"] for item in rows
            ),
            "external_ai_calls": 0,
            "http_calls": 0,
            "production_writes": 0,
            "drive_sheets_writes": 0,
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("artifact_root", type=Path)
    parser.add_argument("--ocr", type=Path, required=True)
    parser.add_argument("--human-session", type=Path, required=True)
    parser.add_argument("--facility-ground-truth", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--amount-ground-truth-override", action="append", default=[]
    )
    args = parser.parse_args()
    overrides = _overrides(args.amount_ground_truth_override)
    report = replay_combined(
        artifact_root=args.artifact_root,
        ocr_path=args.ocr,
        human_session_path=args.human_session,
        facility_ground_truth_path=args.facility_ground_truth,
        amount_ground_truth_overrides=overrides,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(report["summary"], ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
