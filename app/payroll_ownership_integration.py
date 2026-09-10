"""Default-disabled bridge from authoritative Payroll plans to sidecar evidence.

This module is the only production-facing dependency on ownership provenance.
It exposes no writer, apply, parser, storage, or review mutation capability.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, replace
import hashlib
from pathlib import Path
from typing import Literal

from .drive_payroll import DrivePayrollPreview, _suffix, temporary_payroll_file
from .payroll_business_authority import resolve_payroll_business_authority
from .payroll_ocr_snapshot_bridge import capture_ocr_ownership_snapshot
from .payroll_pdf_text_snapshot_bridge import capture_pdf_text_ownership_snapshot
from .payroll_ownership_provenance import (
    CandidateClaim,
    PayrollOwnershipAdoptionCandidate,
    PayrollOwnershipAttestationEvaluation,
    PayrollOwnershipPlanBinding,
    PayrollStorageAuthorityEvidence,
    analyze_successful_claim_authority,
    attest_payroll_write_plan_ownership,
    capture_candidate_enumeration,
    close_consumption_provenance_from_instrumentation,
    evaluate_adoption_candidate,
    reconstruct_consumption,
    trace_incomplete_ownership,
)
from .payroll_statement_parser import preview_payroll_file
from .payroll_sheets import usable_aliases
from .payroll_storage import phase_a_to_storage_candidate
from .payroll_storage_preview import PayrollWritePlan, build_write_plan
from .payroll_write_plan_materialization import (
    payroll_write_plan_to_materialization_plan,
)
from .payroll_writer import preview_payroll_write, validate_payroll_write_contract


@dataclass(frozen=True)
class PayrollOwnershipAttestationRequest:
    """Typed, ephemeral input for one post-plan sidecar comparison."""

    statement_id: str
    candidate: PayrollOwnershipAdoptionCandidate | None
    binding: PayrollOwnershipPlanBinding


@dataclass(frozen=True)
class PayrollOwnershipAttestationRecord:
    """Read-only ownership evidence associated with an existing plan."""

    statement_id: str | None
    evaluation: PayrollOwnershipAttestationEvaluation


@dataclass(frozen=True)
class PayrollOwnershipIntegrationEvidence:
    """Supplemental metadata that never changes Payroll write authority."""

    enabled: bool
    status: Literal["disabled", "evaluated", "evaluation_failed"]
    records: tuple[PayrollOwnershipAttestationRecord, ...] = ()


DISABLED_PAYROLL_OWNERSHIP_EVIDENCE = PayrollOwnershipIntegrationEvidence(
    enabled=False,
    status="disabled",
)


def _rejection(reason: str) -> PayrollOwnershipAttestationEvaluation:
    return PayrollOwnershipAttestationEvaluation(
        accepted=False,
        reason_code=reason,
        attestation=None,
    )


def evaluate_payroll_ownership_attestation_integration(
    payroll_plans: Iterable[PayrollWritePlan],
    requests: Iterable[PayrollOwnershipAttestationRequest] = (),
    *,
    enabled: bool = False,
    local_key: bytes | None = None,
) -> PayrollOwnershipIntegrationEvidence:
    """Evaluate optional ownership evidence without writing or gating a plan.

    Disabled mode deliberately does not consume or validate either input. In
    enabled mode every error is reduced to read-only rejection metadata; this
    observer cannot block, replace, or rewrite an authoritative plan.
    """

    if enabled is not True:
        return DISABLED_PAYROLL_OWNERSHIP_EVIDENCE

    try:
        plans = validate_payroll_write_contract(payroll_plans)
        request_values = tuple(requests)
    except Exception:
        return PayrollOwnershipIntegrationEvidence(
            enabled=True,
            status="evaluation_failed",
        )

    plans_by_statement = {
        plan.identity.statement_id: plan for plan in plans
    }
    records: list[PayrollOwnershipAttestationRecord] = []
    for request in request_values:
        if not isinstance(request, PayrollOwnershipAttestationRequest):
            records.append(PayrollOwnershipAttestationRecord(
                statement_id=None,
                evaluation=_rejection("ownership_attestation_request_required"),
            ))
            continue
        plan = plans_by_statement.get(request.statement_id)
        if plan is None:
            records.append(PayrollOwnershipAttestationRecord(
                statement_id=request.statement_id,
                evaluation=_rejection("ownership_attestation_plan_not_found"),
            ))
            continue
        try:
            evaluation = attest_payroll_write_plan_ownership(
                plan,
                request.candidate,
                request.binding,
                local_key=local_key,
            )
        except Exception:
            evaluation = _rejection("ownership_attestation_evaluation_failed")
        records.append(PayrollOwnershipAttestationRecord(
            statement_id=request.statement_id,
            evaluation=evaluation,
        ))
    return PayrollOwnershipIntegrationEvidence(
        enabled=True,
        status="evaluated",
        records=tuple(records),
    )


@dataclass(frozen=True)
class PayrollOwnershipShadowArtifactResult:
    """Anonymous read-only result for one production Payroll source."""

    parser_mode: str
    plan_status: str
    storage_item_count: int
    storage_standard_count: int
    storage_unknown_with_value_count: int
    storage_review_count: int
    claim_count: int
    authoritative_standard_claim_count: int
    fallback_claim_count: int
    adoption_candidate_count: int
    attestation_success_count: int
    false_attestation_count: int
    candidate_rejections: tuple[tuple[str, int], ...] = ()
    attestation_rejections: tuple[tuple[str, int], ...] = ()
    negative_controls: tuple[tuple[str, int], ...] = ()
    capture_reason: str = "not_evaluated"
    differential_unchanged: bool = True


@dataclass(frozen=True)
class PayrollOwnershipShadowReport:
    """Safe aggregate with no source IDs, names, values, locators, or key."""

    enabled: bool
    reason: str
    sampled_files: int = 0
    evaluated_files: int = 0
    failed_files: int = 0
    artifacts: tuple[PayrollOwnershipShadowArtifactResult, ...] = ()

    def safe_dict(self) -> dict:
        parser_modes = Counter(item.parser_mode for item in self.artifacts)
        plan_statuses = Counter(item.plan_status for item in self.artifacts)
        candidate_rejections = Counter()
        attestation_rejections = Counter()
        negative_controls = Counter()
        for item in self.artifacts:
            candidate_rejections.update(dict(item.candidate_rejections))
            attestation_rejections.update(dict(item.attestation_rejections))
            negative_controls.update(dict(item.negative_controls))
        return {
            "read_only": True,
            "enabled": self.enabled,
            "reason": self.reason,
            "sampled_files": self.sampled_files,
            "evaluated_files": self.evaluated_files,
            "failed_files": self.failed_files,
            "parser_modes": dict(sorted(parser_modes.items())),
            "write_plan_statuses": dict(sorted(plan_statuses.items())),
            "storage_item_count": sum(x.storage_item_count for x in self.artifacts),
            "storage_standard_count": sum(x.storage_standard_count for x in self.artifacts),
            "storage_unknown_with_value_count": sum(
                x.storage_unknown_with_value_count for x in self.artifacts
            ),
            "storage_review_count": sum(x.storage_review_count for x in self.artifacts),
            "claim_count": sum(x.claim_count for x in self.artifacts),
            "authoritative_standard_claim_count": sum(
                x.authoritative_standard_claim_count for x in self.artifacts
            ),
            "fallback_claim_count": sum(x.fallback_claim_count for x in self.artifacts),
            "adoption_candidate_count": sum(
                x.adoption_candidate_count for x in self.artifacts
            ),
            "attestation_success_count": sum(
                x.attestation_success_count for x in self.artifacts
            ),
            "attestation_rejections": dict(sorted(attestation_rejections.items())),
            "candidate_rejections": dict(sorted(candidate_rejections.items())),
            "negative_controls": dict(sorted(negative_controls.items())),
            "false_attestation_count": sum(
                x.false_attestation_count for x in self.artifacts
            ),
            "differential_unchanged": all(
                x.differential_unchanged for x in self.artifacts
            ),
            "writer_invocation_count": 0,
            "apply_invocation_count": 0,
        }


DISABLED_PAYROLL_OWNERSHIP_SHADOW_REPORT = PayrollOwnershipShadowReport(
    enabled=False,
    reason="ownership_shadow_disabled",
)


def load_local_payroll_ownership_hmac_key(
    path: str | Path,
    *,
    repository_root: str | Path | None = None,
) -> bytes:
    """Load a runtime-only 32-byte hex key, rejecting repository-local secrets."""

    key_path = Path(path).resolve()
    if repository_root is not None:
        root = Path(repository_root).resolve()
        try:
            key_path.relative_to(root)
        except ValueError:
            pass
        else:
            raise ValueError("ownership_hmac_key_must_be_outside_repository")
    try:
        encoded = key_path.read_text(encoding="ascii").strip()
        key = bytes.fromhex(encoded)
    except Exception as exc:
        raise ValueError("ownership_hmac_key_unavailable") from exc
    if len(key) != 32:
        raise ValueError("ownership_hmac_key_invalid")
    return key


def _fact_signature(fact) -> tuple:
    return (
        fact.page, fact.x, fact.y, fact.name, fact.raw_value,
        fact.needs_review, fact.reason, fact.mapped,
    )


def _preview_item_signature(item) -> tuple:
    return (
        item.page, item.x, item.y, item.raw_item_name, item.raw_value,
        item.needs_review, item.review_reason_code,
        item.standard_item_candidate is not None,
    )


def _changed_plan_row(plan, row_index: int, field: str, value):
    rows = list(plan.planned_item_rows)
    row = rows[row_index]
    values = row.as_dict()
    values[field] = value
    rows[row_index] = row.model_copy(update={
        "values": tuple(values[column] for column in row.columns),
    })
    return plan.model_copy(update={"planned_item_rows": tuple(rows)})


def _negative_control_results(plan, request, *, local_key, row_index):
    """Run immutable controls over one real binding; never expose their values."""

    candidate = request.candidate
    binding = request.binding
    if candidate is None or plan.status != "ready":
        return Counter(), 0
    rows = plan.planned_item_rows
    current_value = rows[row_index].as_dict().get("value")
    other_value = (
        current_value + 1
        if isinstance(current_value, (int, float)) and not isinstance(current_value, bool)
        else "ownership-shadow-distinct-value"
    )
    controls = {
        "employer_mismatch": (
            plan, candidate, replace(binding, employer_scope="ownership-shadow-mismatch")
        ),
        "parser_mode_mismatch": (
            plan, candidate,
            replace(binding, parser_mode=("pdf" if binding.parser_mode == "ocr" else "ocr")),
        ),
        "snapshot_mismatch": (
            plan, candidate, replace(binding, snapshot_id="ownership-shadow-mismatch")
        ),
        "hidden_candidate": (
            plan, candidate,
            replace(binding, enumerated_candidate_id="ownership-shadow-hidden"),
        ),
        "stale_evidence": (
            plan, replace(candidate, adoption_contract_version="stale"), binding,
        ),
        "field_mismatch": (
            _changed_plan_row(
                plan, row_index, "standard_item_id", "ownership-shadow-other-field",
            ),
            candidate,
            binding,
        ),
        "value_mismatch": (
            _changed_plan_row(plan, row_index, "value", other_value),
            candidate,
            binding,
        ),
        "review_contamination": (
            _changed_plan_row(plan, row_index, "needs_review", True),
            candidate,
            binding,
        ),
    }
    rejected = Counter()
    accepted = 0
    for name, (control_plan, control_candidate, control_binding) in controls.items():
        result = attest_payroll_write_plan_ownership(
            control_plan,
            control_candidate,
            control_binding,
            local_key=local_key,
        )
        if result.accepted:
            accepted += 1
        else:
            rejected[name] += 1
    return rejected, accepted


def evaluate_payroll_ownership_shadow_artifact(
    preview,
    storage_candidate,
    plan: PayrollWritePlan,
    ownership_snapshot,
    *,
    enabled: bool = False,
    local_key: bytes | None = None,
    source_replay_closed: bool = False,
    capture_reason: str = "not_evaluated",
) -> PayrollOwnershipShadowArtifactResult:
    """Evaluate one already-parsed source without changing production values."""

    if enabled is not True:
        raise ValueError("ownership_shadow_caller_enable_required")
    if not isinstance(local_key, bytes) or len(local_key) != 32:
        raise ValueError("ownership_hmac_key_invalid")

    parser_before = preview.model_dump(mode="json")
    storage_before = storage_candidate.model_dump(mode="json")
    plan_before = plan.model_dump(mode="json")
    writer_before = preview_payroll_write([plan]).model_dump(mode="json")
    materialization_before = (
        payroll_write_plan_to_materialization_plan(plan)
        if plan.status == "ready" else None
    )

    provenance = reconstruct_consumption(
        ownership_snapshot,
        source_snapshot_id=ownership_snapshot.snapshot_id,
        page_scope_complete=source_replay_closed,
        success_set_complete=source_replay_closed,
    )
    instrumentation = trace_incomplete_ownership(
        ownership_snapshot,
        provenance,
        tuple(CandidateClaim("instrumentation", token_id)
              for token_id in ownership_snapshot.token_ids),
    )
    provenance = close_consumption_provenance_from_instrumentation(
        ownership_snapshot, provenance, instrumentation,
    )
    ledger = capture_candidate_enumeration(ownership_snapshot)
    authority = analyze_successful_claim_authority(
        ownership_snapshot, provenance, ledger,
    )
    candidate_rejections = Counter()
    requests = []
    request_safe = []
    request_row_indexes = []
    paired = tuple(zip(preview.items, storage_candidate.items))
    for claim in authority.claims:
        value_tokens = tuple(
            token for token_id, token in zip(
                ownership_snapshot.token_ids, ownership_snapshot.tokens,
            )
            if token_id == claim.physical_value_id
        )
        value_token = value_tokens[0] if len(value_tokens) == 1 else None
        matches = [
            (index, stored)
            for index, (parsed, stored) in enumerate(paired)
            if value_token is not None
            and parsed.page == value_token.page
            and parsed.raw_value == value_token.text
            and parsed.standard_item_candidate
            == claim.authoritative_standard_item_id
            and stored.standard_item_id == claim.authoritative_standard_item_id
            and sum(
                _preview_item_signature(parsed) == _fact_signature(fact)
                for fact in ownership_snapshot.facts
            ) == 1
        ]
        relation = matches[0] if len(matches) == 1 else None
        stored = relation[1] if relation is not None else None
        storage_evidence = PayrollStorageAuthorityEvidence(
            standard_item_id=(stored.standard_item_id if stored is not None else None),
            uncertain=(True if stored is None else stored.needs_review),
            value_persistable=(stored is not None and stored.value is not None),
            review_reason_code=(
                stored.review_reason_code if stored is not None else None
            ),
        )
        evaluated = evaluate_adoption_candidate(
            ownership_snapshot,
            provenance,
            ledger,
            claim,
            employer_scope=plan.identity.employer_id or "",
            expected_employer_scope=storage_candidate.statement.employer_id,
            source_replay_closed=source_replay_closed,
            storage_evidence=storage_evidence,
            review_authority_contaminated=(
                stored is not None
                and (stored.needs_review or stored.review_status == "pending")
            ),
        )
        if not evaluated.accepted or evaluated.candidate is None:
            candidate_rejections[evaluated.reason_code] += 1
            continue
        binding = PayrollOwnershipPlanBinding(
            portable_claim_id=evaluated.candidate.portable_claim_id,
            snapshot_id=evaluated.candidate.snapshot_id,
            parser_mode=evaluated.candidate.parser_mode,
            employer_scope=evaluated.candidate.employer_scope,
            source_file_id=plan.identity.source_file_id or "",
            content_hash=plan.identity.content_hash or "",
            authoritative_standard_item_id=(
                evaluated.candidate.authoritative_standard_item_id
            ),
            enumerated_candidate_id=evaluated.candidate.enumerated_candidate_id,
            authoritative_value=(stored.value if stored is not None else None),
            source_alignment_closed=(source_replay_closed and relation is not None),
        )
        requests.append(PayrollOwnershipAttestationRequest(
            statement_id=plan.identity.statement_id,
            candidate=evaluated.candidate,
            binding=binding,
        ))
        request_safe.append(bool(
            plan.status == "ready"
            and stored is not None
            and not stored.needs_review
            and stored.review_status != "pending"
            and stored.value is not None
            and claim.authority_result == "authoritative_standard_field"
        ))
        matching_plan_rows = tuple(
            index for index, row in enumerate(plan.planned_item_rows)
            if row.as_dict().get("standard_item_id")
            == evaluated.candidate.authoritative_standard_item_id
            and row.as_dict().get("value") == (stored.value if stored is not None else None)
        )
        request_row_indexes.append(
            matching_plan_rows[0] if len(matching_plan_rows) == 1 else 0
        )

    evidence = evaluate_payroll_ownership_attestation_integration(
        [plan], requests, enabled=True, local_key=local_key,
    )
    attestation_rejections = Counter(
        record.evaluation.reason_code for record in evidence.records
        if not record.evaluation.accepted
    )
    successes = sum(record.evaluation.accepted for record in evidence.records)
    false_attestations = sum(
        record.evaluation.accepted and not safe
        for record, safe in zip(evidence.records, request_safe)
    )
    negative_controls = Counter()
    for request, row_index in zip(requests, request_row_indexes):
        rejected, accepted = _negative_control_results(
            plan, request, local_key=local_key, row_index=row_index,
        )
        negative_controls.update(rejected)
        false_attestations += accepted

    parser_after = preview.model_dump(mode="json")
    storage_after = storage_candidate.model_dump(mode="json")
    plan_after = plan.model_dump(mode="json")
    writer_after = preview_payroll_write([plan]).model_dump(mode="json")
    materialization_after = (
        payroll_write_plan_to_materialization_plan(plan)
        if plan.status == "ready" else None
    )
    unchanged = (
        parser_before == parser_after
        and storage_before == storage_after
        and plan_before == plan_after
        and writer_before == writer_after
        and materialization_before == materialization_after
    )
    return PayrollOwnershipShadowArtifactResult(
        parser_mode=preview.extraction_method,
        plan_status=plan.status,
        storage_item_count=len(storage_candidate.items),
        storage_standard_count=sum(
            item.standard_item_id is not None for item in storage_candidate.items
        ),
        storage_unknown_with_value_count=sum(
            item.review_reason_code == "unknown_with_value"
            for item in storage_candidate.items
        ),
        storage_review_count=sum(item.needs_review for item in storage_candidate.items),
        claim_count=len(authority.claims),
        authoritative_standard_claim_count=sum(
            claim.authority_result == "authoritative_standard_field"
            for claim in authority.claims
        ),
        fallback_claim_count=sum(
            claim.authority_result == "snapshot_success_only"
            for claim in authority.claims
        ),
        adoption_candidate_count=len(requests),
        attestation_success_count=successes,
        false_attestation_count=false_attestations,
        candidate_rejections=tuple(sorted(candidate_rejections.items())),
        attestation_rejections=tuple(sorted(attestation_rejections.items())),
        negative_controls=tuple(sorted(negative_controls.items())),
        capture_reason=capture_reason,
        differential_unchanged=unchanged,
    )


def drive_payroll_ownership_shadow(
    folder_id: str,
    sheets_snapshot,
    *,
    enabled: bool = False,
    local_key: bytes | None = None,
    employer_id: str | None = None,
    statement_type: str | None = None,
    service=None,
    downloader=None,
    parser=preview_payroll_file,
    capture=capture_ocr_ownership_snapshot,
    capture_pdf_text=capture_pdf_text_ownership_snapshot,
) -> PayrollOwnershipShadowReport:
    """Read production sources and return anonymous evidence without mutation."""

    if enabled is not True:
        return DISABLED_PAYROLL_OWNERSHIP_SHADOW_REPORT
    if not isinstance(local_key, bytes) or len(local_key) != 32:
        raise ValueError("ownership_hmac_key_invalid")
    adapter = DrivePayrollPreview(
        folder_id, service=service, downloader=downloader, parser=parser,
    )
    aliases = usable_aliases(
        sheets_snapshot.standard_items, sheets_snapshot.aliases,
    )
    files = adapter._files()
    artifacts = []
    failed = 0
    for file in files:
        suffix = _suffix(file)
        if suffix is None:
            failed += 1
            continue
        try:
            data = adapter.downloader(file["id"])
            with temporary_payroll_file(data, suffix) as path:
                preview = adapter.parser(path)
                authority = resolve_payroll_business_authority(
                    preview, sheets_snapshot.employers,
                    employer_id=employer_id, statement_type=statement_type,
                )
                storage = phase_a_to_storage_candidate(
                    preview,
                    employer_id=authority.employer_id,
                    statement_type=authority.statement_type,
                    source_type="drive",
                    source_file_id=file["id"],
                    content_hash=hashlib.sha256(data).hexdigest(),
                    aliases=aliases,
                    standard_items=sheets_snapshot.standard_items,
                    file_name=file.get("name"),
                )
                plan = build_write_plan([storage], sheets_snapshot)[0]
                missing_business_authority = (
                    "employer_scope_missing" if authority.employer_id is None
                    else "statement_type_missing" if authority.statement_type is None
                    else None
                )
                if missing_business_authority is not None:
                    artifacts.append(PayrollOwnershipShadowArtifactResult(
                        parser_mode=preview.extraction_method,
                        plan_status=plan.status,
                        storage_item_count=len(storage.items),
                        storage_standard_count=sum(
                            item.standard_item_id is not None for item in storage.items
                        ),
                        storage_unknown_with_value_count=sum(
                            item.review_reason_code == "unknown_with_value"
                            for item in storage.items
                        ),
                        storage_review_count=sum(
                            item.needs_review for item in storage.items
                        ),
                        claim_count=0,
                        authoritative_standard_claim_count=0,
                        fallback_claim_count=0,
                        adoption_candidate_count=0,
                        attestation_success_count=0,
                        false_attestation_count=0,
                        candidate_rejections=((missing_business_authority, 1),),
                        capture_reason="business_authority_incomplete",
                    ))
                    continue
                captured = (
                    capture(path, local_key=local_key)
                    if preview.extraction_method == "ocr"
                    else capture_pdf_text(path, local_key=local_key)
                )
            artifacts.append(evaluate_payroll_ownership_shadow_artifact(
                preview,
                storage,
                plan,
                captured.snapshot,
                enabled=True,
                local_key=local_key,
                source_replay_closed=captured.ownership_ready,
                capture_reason=captured.reason,
            ))
        except Exception:
            failed += 1
    return PayrollOwnershipShadowReport(
        enabled=True,
        reason="ownership_shadow_evaluated",
        sampled_files=len(files),
        evaluated_files=len(artifacts),
        failed_files=failed,
        artifacts=tuple(artifacts),
    )
