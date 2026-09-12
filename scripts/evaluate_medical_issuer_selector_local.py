"""Replay saved local OCR through the shadow issuer selector.

Ground truth is loaded only after selection and is used only to score outputs.
This script performs no OCR, HTTP, AI, Drive, Sheets, or production writes.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from app.medical_issuer_selector_shadow import (
    IssuerBinding,
    OcrFacilityRegion,
    OcrPageForIssuerSelection,
    generate_facility_candidates,
    select_medical_issuer_shadow,
)


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _session_bindings(session: dict) -> dict[int, IssuerBinding]:
    result = {}
    for item in session.get("units", []):
        unit = int(item["unit"])
        result[unit] = IssuerBinding(
            source_sha256=item["source_sha256"],
            image_sha256=item["image_sha256"],
            unit=unit,
            page=int(item["page"]),
        )
    if set(result) != set(range(1, 11)):
        raise RuntimeError("trusted session binding is not the fixed 10-unit corpus")
    return result


def _ocr_pages(ocr: dict) -> dict[int, OcrPageForIssuerSelection]:
    result = {}
    for item in ocr.get("units", []):
        unit = int(item["unit"])
        binding = IssuerBinding(
            source_sha256=item["source_sha256"],
            image_sha256=item["image_sha256"],
            unit=unit,
            page=int(item["page"]),
        )
        regions = tuple(
            OcrFacilityRegion(
                ordinal=int(region["ordinal"]),
                reading_order=int(region["reading_order"]),
                raw_text=region["raw_text"],
                confidence=float(region["confidence"]),
                bbox_xywh=tuple(float(value) for value in region["bbox_xywh"]),
            )
            for region in item["regions"]
            if region.get("raw_text") and region.get("bbox_xywh") is not None
        )
        result[unit] = OcrPageForIssuerSelection(
            binding=binding,
            width=int(item["width"]),
            height=int(item["height"]),
            regions=regions,
        )
    if set(result) != set(range(1, 11)):
        raise RuntimeError("OCR observation is not the fixed 10-unit corpus")
    return result


def _ground_truth(ground_truth: dict) -> dict[int, dict]:
    if ground_truth.get("schema_version") != "medical-issuer-ground-truth-local-v1":
        raise RuntimeError("unexpected ground truth schema")
    result = {int(item["unit"]): item for item in ground_truth.get("records", [])}
    if set(result) != set(range(1, 11)):
        raise RuntimeError("human issuer adjudication is incomplete")
    if any(
        item["decision"] != "ISSUER_CORRECT"
        or item["selected_issuer_region_ordinal"]
        != item["proposed_issuer_region_ordinal"]
        for item in result.values()
    ):
        raise RuntimeError("ground truth is not 10/10 ISSUER_CORRECT")
    return result


def evaluate(*, ocr: dict, session: dict, ground_truth: dict) -> dict:
    bindings = _session_bindings(session)
    pages = _ocr_pages(ocr)
    truth = _ground_truth(ground_truth)
    unit_results = []
    for unit in range(1, 11):
        page = pages[unit]
        expected = bindings[unit]
        # Selection is complete before the adjudication record is read below.
        outcome = select_medical_issuer_shadow(page, expected_binding=expected)
        candidates = generate_facility_candidates(page)
        candidate_by_ordinal = {item.region_ordinal: item for item in candidates}
        selected_candidate = candidate_by_ordinal.get(outcome.issuer_region_ordinal)

        adjudicated = truth[unit]
        ground_truth_binding_valid = all(
            (
                adjudicated["source_sha256"] == expected.source_sha256,
                adjudicated["image_sha256"] == expected.image_sha256,
                adjudicated["page"] == expected.page,
            )
        )
        region_match = (
            outcome.issuer_region_ordinal
            == adjudicated["selected_issuer_region_ordinal"]
        )
        text_match = outcome.issuer_facility_name == adjudicated["issuer_ocr_text"]
        issuer_match = ground_truth_binding_valid and region_match and text_match
        unit_results.append(
            {
                "unit": unit,
                "selected_ocr_text": outcome.issuer_facility_name,
                "selected_region_ordinal": outcome.issuer_region_ordinal,
                "issuer_facility_type": outcome.issuer_facility_type,
                "issuer_role": outcome.issuer_role,
                "role_signals": (
                    list(selected_candidate.role_signals)
                    if selected_candidate is not None
                    else []
                ),
                "competing_facilities": [
                    item.model_dump(mode="json")
                    for item in outcome.competing_facilities
                ],
                "referenced_providers": [
                    item.model_dump(mode="json")
                    for item in outcome.referenced_providers
                ],
                "verdict": outcome.verdict,
                "selection_reason": outcome.selection_reason,
                "ambiguity": outcome.ambiguity,
                "binding": outcome.binding.model_dump(mode="json"),
                "selector_version": outcome.selector_version,
                "ground_truth_binding_valid": ground_truth_binding_valid,
                "ground_truth_region_ordinal": adjudicated[
                    "selected_issuer_region_ordinal"
                ],
                "ground_truth_ocr_text": adjudicated["issuer_ocr_text"],
                "issuer_ground_truth_match": issuer_match,
                "facility_name_text_match": text_match,
                "ocr_correction_required": not text_match,
            }
        )
    auto_selected = sum(
        item["verdict"] == "SELECTED_ISSUER" for item in unit_results
    )
    matched = sum(item["issuer_ground_truth_match"] for item in unit_results)
    pharmacy_units = [
        item for item in unit_results if item["unit"] in {2, 3, 9}
    ]
    return {
        "schema_version": "medical-issuer-selector-local-evaluation-v1",
        "selector_ground_truth_input": False,
        "units": unit_results,
        "summary": {
            "unit_count": 10,
            "human_adjudication_complete": True,
            "issuer_ground_truth_matches": matched,
            "auto_select_count": auto_selected,
            "human_review_count": 10 - auto_selected,
            "false_issuer_selection_count": sum(
                item["verdict"] == "SELECTED_ISSUER"
                and not item["issuer_ground_truth_match"]
                for item in unit_results
            ),
            "facility_name_text_matches": sum(
                item["facility_name_text_match"] for item in unit_results
            ),
            "ocr_correction_unnecessary_count": sum(
                not item["ocr_correction_required"] for item in unit_results
            ),
            "pharmacy_role_discrimination": {
                "units": [2, 3, 9],
                "issuer_selected_as_pharmacy": sum(
                    item["issuer_facility_type"] == "pharmacy"
                    for item in pharmacy_units
                ),
                "referenced_provider_separated": sum(
                    bool(item["referenced_providers"]) for item in pharmacy_units
                ),
                "prescribing_provider_selected_as_issuer": sum(
                    any(
                        provider["region_ordinal"] == item["selected_region_ordinal"]
                        for provider in item["referenced_providers"]
                    )
                    for item in pharmacy_units
                ),
            },
            "production_write": 0,
            "external_http": 0,
            "external_ai": 0,
            "drive_sheets_write": 0,
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ocr", type=Path, required=True)
    parser.add_argument("--session", type=Path, required=True)
    parser.add_argument("--ground-truth", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = evaluate(
        ocr=_load(args.ocr),
        session=_load(args.session),
        ground_truth=_load(args.ground_truth),
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(report["summary"], ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
