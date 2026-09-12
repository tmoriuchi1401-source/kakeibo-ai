"""Local-only operator trial for the fixed Medical 10-unit corpus.

Sensitive values and source paths are written only to an explicitly supplied
directory outside the repository.  This tool has no transport or production
writer and never turns a manual amount into OCR evidence.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import time

try:
    from scripts.evaluate_medical_ai_structured_offline import _images, _runtime, _selected_files
except ModuleNotFoundError:  # direct ``python scripts/...`` execution
    from evaluate_medical_ai_structured_offline import _images, _runtime, _selected_files
from app.medical_ai_structured_shadow import prepare_structured_shadow_from_level2
from app.medical_ocr_observation_shadow import ReceiptImage
from app.medical_payment_level2_shadow import classify_structural_relation, evaluate_level2_payment_shadow
from app.medical_rapidocr_shadow import RapidOcrShadowAdapter
from app.medical_receipt_privacy import _structured_amount


@dataclass(frozen=True)
class UnitCard:
    unit: int
    page: int
    source_path: str
    source_sha256: str
    image_sha256: str
    observation_status: str
    candidate_origin: str
    original_candidate: int | None
    stop_reason: str


def _outside_repo(path: Path, repository_root: Path) -> Path:
    target = path.resolve()
    try:
        target.relative_to(repository_root.resolve())
    except ValueError:
        return target
    raise ValueError("output_must_be_outside_repository")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _candidate(build, observation):
    labels = tuple(t for t in build.tokens if t.exact_strong_label)
    regions = {r.ordinal: r for r in observation.regions}
    viable = []
    for token in build.tokens:
        if token.numeric_amount is None or token.scope == "excluded":
            continue
        related = any(
            label.unit_ref == token.unit_ref and label.page == token.page and
            (label.token_id == token.token_id or (
                label.ordinal in regions and token.ordinal in regions and
                classify_structural_relation(regions[label.ordinal], regions[token.ordinal]).state == "STRONG"
            )) for label in labels
        )
        if related:
            viable.append(token.numeric_amount)
    unique = sorted(set(viable))
    if len(unique) == 1 and labels:
        return unique[0], "existing_structured_shadow_strong_relation", "candidate_available"
    if not labels:
        return None, "none", "exact_payment_label_unavailable"
    if not unique:
        return None, "none", "strong_related_numeric_unavailable"
    return None, "none", "ambiguous_strong_related_numeric"


def prepare(runtime_root: Path, corpus_root: Path, output_dir: Path, repository_root: Path):
    output_dir = _outside_repo(output_dir, repository_root)
    output_dir.mkdir(parents=True, exist_ok=True)
    manifest, executable = _runtime(runtime_root.resolve())
    adapter = RapidOcrShadowAdapter(manifest, python_executable=executable)
    cards = []
    unit = 0
    for path in _selected_files(corpus_root.resolve()):
        source_bytes = path.read_bytes()
        for page, image_bytes in enumerate(_images(path), 1):
            unit += 1
            observation = adapter.observe(ReceiptImage(f"local-review-unit-{unit}", 1, image_bytes))
            if observation.complete:
                level2 = evaluate_level2_payment_shadow(observation)
                build = prepare_structured_shadow_from_level2(observation, level2, source_kind="real_medical").build
                amount, origin, reason = _candidate(build, observation)
                status = "complete"
            else:
                amount, origin, reason, status = None, "none", "ocr_observation_incomplete", "incomplete"
            cards.append(UnitCard(unit, page, str(path.resolve()), _sha256_bytes(source_bytes),
                                  _sha256_bytes(image_bytes), status, origin, amount, reason))
    if len(cards) != 10:
        raise ValueError("fixed_corpus_unit_count_mismatch")
    session = {
        "schema_version": "medical-local-review-trial-v1",
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "corpus_unit_count": 10,
        "records": [],
        "units": [asdict(card) for card in cards],
        "comparison_units": {
            "candidate_available": next(c.unit for c in cards if c.original_candidate is not None),
            "ocr_usable_no_candidate": next(c.unit for c in cards if c.observation_status == "complete" and c.original_candidate is None),
            "ocr_incomplete": next(c.unit for c in cards if c.observation_status == "incomplete"),
        },
        "network_calls": 0,
        "drive_sheets_writes": 0,
        "production_writes": 0,
    }
    target = output_dir / "medical-review-session.json"
    if target.exists():
        raise ValueError("review_session_already_exists")
    target.write_text(json.dumps(session, ensure_ascii=False, indent=2), encoding="utf-8")
    return target


def _amount(value: str) -> int:
    parsed = _structured_amount(value.strip())
    if parsed is None or parsed <= 0:
        raise ValueError("invalid_amount_format")
    return parsed


def review(session_path: Path, *, open_original: bool = True, mode: str = "assisted", units=None):
    if mode not in {"assisted", "original-only"}:
        raise ValueError("invalid_review_mode")
    session = json.loads(session_path.read_text(encoding="utf-8"))
    selected = set(units or range(1, 11))
    done = {(r["unit"], r.get("mode", "assisted")) for r in session["records"]}
    for card in session["units"]:
        if card["unit"] not in selected or (card["unit"], mode) in done:
            continue
        if _sha256_bytes(Path(card["source_path"]).read_bytes()) != card["source_sha256"]:
            raise ValueError("source_binding_mismatch")
        if open_original:
            os.startfile(card["source_path"])
        print(f'Unit {card["unit"]} / page {card["page"]}')
        if mode == "assisted":
            print("Candidate:", card["original_candidate"] if card["original_candidate"] is not None else "none")
            print("Reason:", card["stop_reason"])
        else:
            print("Candidate: hidden (original-only comparison)")
        started = time.monotonic()
        action = input("Action [confirm/correct/unreadable/defer]: ").strip().lower()
        if action not in {"confirm", "correct", "unreadable", "defer"}:
            raise ValueError("invalid_review_action")
        human_amount = None
        if action == "confirm":
            entered = input("Amount read from original [Enter accepts shown candidate]: ").strip()
            if entered:
                human_amount = _amount(entered)
            elif mode == "assisted" and card["original_candidate"] is not None:
                human_amount = card["original_candidate"]
            else:
                raise ValueError("amount_required_for_original_only_or_missing_candidate")
        elif action == "correct":
            human_amount = _amount(input("Correct amount read from original: "))
        interrupted = input("Interrupted? [y/N]: ").strip().lower() == "y"
        operation_note = input("Optional short operation note [Enter to skip]: ").strip()
        elapsed = round(time.monotonic() - started, 3)
        record = {
            "trial_sequence": len(session["records"]) + 1,
            "completed_at_utc": datetime.now(timezone.utc).isoformat(),
            "unit": card["unit"], "page": card["page"], "mode": mode, "source_sha256": card["source_sha256"],
            "image_sha256": card["image_sha256"], "candidate_origin": card["candidate_origin"],
            "original_candidate": card["original_candidate"], "operator_action": action,
            "human_entered_amount": human_amount if action in {"correct"} or card["original_candidate"] is None else None,
            "confirmed_amount": human_amount, "elapsed_seconds": elapsed, "interrupted": interrupted,
            "operation_note": operation_note,
            "local_saved": True, "handoff_status": "human_review_complete_handoff_unsupported",
            "handoff_reason": "existing_handoff_is_value_free_and_cannot_carry_manual_amount",
            "production_write": False,
        }
        session["records"].append(record)
        temp = session_path.with_suffix(".tmp")
        temp.write_text(json.dumps(session, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(session_path)


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("prepare")
    p.add_argument("runtime_root", type=Path); p.add_argument("corpus_root", type=Path)
    p.add_argument("output_dir", type=Path); p.add_argument("repository_root", type=Path)
    r = sub.add_parser("review"); r.add_argument("session_path", type=Path)
    r.add_argument("--no-open", action="store_true")
    r.add_argument("--mode", choices=("assisted", "original-only"), default="assisted")
    r.add_argument("--units", help="comma-separated unit numbers; default all")
    args = parser.parse_args()
    if args.command == "prepare":
        print(prepare(args.runtime_root, args.corpus_root, args.output_dir, args.repository_root))
    else:
        units = tuple(int(value) for value in args.units.split(",")) if args.units else None
        review(args.session_path, open_original=not args.no_open, mode=args.mode, units=units)


if __name__ == "__main__":
    main()
