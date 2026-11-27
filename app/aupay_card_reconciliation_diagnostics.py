"""Bounded, value-free diagnostics. Never grants authority or changes state."""
from __future__ import annotations


STAGES = frozenset({
    "entry_start", "release_guard", "dependencies", "capsule_decode", "bank_premises",
    "bindings", "state_revision", "runtime_clients", "cached_projection",
    "reconciliation_preflight", "financial_preflight", "source_preflight",
    "capability_issue", "archive_prepare", "native_guard", "native_save",
    "native_independent_readback", "native_reserved", "parent_guard",
    "parent_save", "parent_independent_readback", "parent_reserved",
    "native_validate", "native_prewrite_read", "native_write", "native_transport_readback",
    "parent_validate", "parent_prewrite_read", "parent_write", "parent_transport_readback",
    "writer_prepare", "writer_execute", "financial_guard",
    "before_financial_append", "financial_attempt_finished", "outcome_save",
    "outcome_independent_readback", "outcome_persisted", "financial_readback",
    "checkpoint_proof", "checkpoint_guard", "checkpoint_commit",
    "checkpoint_independent_readback", "checkpoint_committed",
    "parent_completion", "parent_completion_readback", "parent_completed",
    "final_readback", "complete", "unused_revocation", "dispatch_intent",
})
EFFECT_STATES = {
    "native_reservation": {"not_started", "write_possible", "readback_confirmed"},
    "parent_reservation": {"not_started", "write_possible", "readback_confirmed"},
    "financial": {"not_reached", "writer_entered", "append_possible", "exact_readback_confirmed"},
    "checkpoint": {"not_started", "write_possible", "readback_confirmed"},
    "parent_completion": {"not_started", "write_possible", "readback_confirmed"},
}
# Exact literals only. A syntactically safe-looking external message is NOT a code.
ERROR_CODES = frozenset({
    "bank_premises_invalid", "bank_premises_revision_changed", "bank_premises_incomplete",
    "bank_premises_chain_broken", "bank_premises_parent_changed", "bank_premises_native_changed",
    "bank_premises_history_changed", "bank_premises_not_noop", "bank_premises_run_unproven",
    "bank_premises_code_changed", "bank_premises_snapshot_changed", "bank_premises_other_writer",
    "bank_premises_business_changed",
    "repair_failed_closed", "repair_arguments_forbidden", "repair_hosted_owner_once_required",
    "repair_release_boundary_changed", "repair_main_changed", "repair_run_changed",
    "repair_dispatch_consumed_no_retry", "repair_competing_run", "repair_capsule_too_large",
    "repair_capsule_invalid", "repair_signed_approval_invalid", "repair_record_not_canonical",
    "repair_target_changed", "repair_binding_changed", "repair_state_revision_changed",
    "repair_documents_changed", "repair_acl_invalid", "repair_acl_changed",
    "repair_fixed_source_changed", "repair_timeout", "repair_old_failure_changed",
    "repair_peer_binding_changed", "repair_peer_native_pending_or_changed",
    "repair_state_changed", "repair_checkpoint_invalid", "repair_signed_count_changed",
    "repair_existing_authority_required", "repair_record_changed", "repair_parent_changed",
    "repair_financial_changed", "repair_checkpoint_changed", "repair_window_exceeds_authority",
    "repair_candidates_changed", "repair_approved_projection_changed", "repair_cache_invalid",
    "repair_native_file_forbidden", "repair_reservation_independent_readback",
    "repair_parent_reservation_independent_readback", "repair_reservation_changed",
    "repair_projection_changed_before_dispatch", "repair_unknown_no_replay",
    "repair_outcome_independent_readback", "repair_checkpoint_independent_readback",
    "repair_parent_completion_independent_readback", "repair_final_state_changed",
    "repair_other_source_changed", "financial_exact_delta_failed",
    "state_changed_during_read", "state_target_mismatch", "state_save_readback_mismatch",
    "state_changed_since_restore", "state_drive_read_failed", "state_drive_write_unknown",
    "state_drive_not_editable", "state_drive_target_mismatch", "state_corrupt",
    "state_envelope_invalid", "state_checksum_mismatch", "state_size_limit",
    "ledger_changed_since_read", "ledger_readback_failed", "ledger_invalid",
    "evidence_timeout", "evidence_read_failed_no_retry", "evidence_mutation_forbidden",
    "batch_approval_binding_or_ttl_invalid", "batch_capability_reused_or_expired",
    "protected_batch_approval_invalid", "persistent_audit_key_required",
    "unused_capability_revocation_denied", "batch_authorization_context_invalid",
    "recovery_reservation_owner_changed", "recovery_reservation_record_changed", "recovery_origin_missing",
    "recovery_parent_barrier_changed", "recovery_financial_dispatch_possible", "recovery_parent_revision_changed",
    "recovery_unused_batch_missing", "recovery_unused_capability_unproven", "recovery_unused_manifest_changed",
    "recovery_checkpoint_changed", "recovery_revocation_archive_missing", "recovery_revocation_archive_changed",
    "recovery_revocation_changed", "recovery_archive_cycle", "recovery_independent_revision_required",
    "recovery_state_revision_changed", "recovery_failure_identity_invalid", "recovery_intent_independent_readback",
    "recovery_adopted_archive_changed", "recovery_archive_chain_changed", "recovery_execution_time_invalid",
    "recovery_dispatch_intent_missing", "recovery_execution_identity_required", "recovery_completion_execution_unproven",
    "recovery_archive_history_changed", "recovery_revocation_time_invalid", "recovery_business_plan_changed",
    *{f"state_drive_read_failed_{status}" for status in (401,403,404,429,500,502,503,504)},
})


def _exception_types(error):
    return {(t.__module__, t.__name__) for t in type(error).__mro__}


def exception_summary(error):
    """No str/repr, traceback, response body, URL, header, or exception class name out."""
    kinds = _exception_types(error)
    if isinstance(error, KeyboardInterrupt): kind = "interrupted"
    elif isinstance(error, SystemExit): kind = "system_exit"
    elif isinstance(error, TimeoutError): kind = "timeout"
    elif isinstance(error, ConnectionError): kind = "connection"
    elif ("app.drive_run_state", "StateError") in kinds: kind = "state_error"
    elif ("app.aupay_card_window_preview", "Stop") in kinds: kind = "guard_rejected"
    elif ("app.aupay_card_window_evidence", "EvidenceError") in kinds: kind = "evidence_error"
    elif ("googleapiclient.errors", "HttpError") in kinds or ("urllib.error", "HTTPError") in kinds: kind = "http_error"
    elif isinstance(error, (ValueError, TypeError, KeyError, AssertionError)): kind = "validation_error"
    else: kind = "unexpected_exception"
    code = error.args[0] if error.args and type(error.args[0]) is str and error.args[0] in ERROR_CODES else "repair_failed_closed"
    status = None
    current, seen = error, set()
    # Wrappers intentionally suppress text with `from None`; context still
    # permits a bounded status-only observation without leaking their bodies.
    for _ in range(8):
        if current is None or id(current) in seen: break
        seen.add(id(current)); types = _exception_types(current)
        if ("googleapiclient.errors", "HttpError") in types:
            value = getattr(getattr(current, "resp", None), "status", None)
        elif ("urllib.error", "HTTPError") in types:
            value = current.code
        else: value = None
        if type(value) is int and 100 <= value <= 599:
            status = value; break
        current = current.__cause__ or current.__context__
    return dict(error_code=code, exception_class=kind, http_status=status)


class ReconciliationDiagnostics:
    def __init__(self):
        self.stage = "entry_start"
        self.effects = {k: "not_reached" if k == "financial" else "not_started" for k in EFFECT_STATES}

    def mark(self, stage, effect=None, state=None):
        if stage not in STAGES or (effect is not None and state not in EFFECT_STATES.get(effect, ())):
            raise ValueError("repair_failed_closed")
        self.stage = stage
        if effect is not None: self.effects[effect] = state

    def report(self, error=None):
        failure = exception_summary(error) if error is not None else dict(error_code="none", exception_class="none", http_status=None)
        return dict(last_stage=self.stage, **failure, effects=dict(self.effects))

    def state_save(self, scope, event):
        if scope not in {"native", "parent"} or event not in {"validate", "prewrite_read", "write", "transport_readback"}:
            raise ValueError("repair_failed_closed")
        self.mark(scope + "_" + event)
        if event == "write": self.effects[scope + "_reservation"] = "write_possible"
