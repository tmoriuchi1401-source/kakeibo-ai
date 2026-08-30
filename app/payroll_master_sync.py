from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal, Protocol

from pydantic import BaseModel, Field

from .payroll_sheets import PayrollSheetsSnapshot, SHEET_TITLES
from .payroll_storage import (
    INITIAL_ALIASES,
    INITIAL_STANDARD_ITEMS,
    PayrollItemAliasRecord,
    PayrollStandardItemRecord,
)


class MasterSyncIssue(BaseModel):
    kind: Literal["schema", "standard_item", "alias"]
    code_id: str
    reason: str


class PayrollMasterSyncPlan(BaseModel):
    schema_ok: bool
    code_standard_item_count: int
    sheets_active_standard_item_count: int
    code_exact_alias_count: int
    sheets_active_alias_count: int
    would_add_standard_items: list[PayrollStandardItemRecord] = Field(default_factory=list)
    would_add_aliases: list[PayrollItemAliasRecord] = Field(default_factory=list)
    already_present: list[dict[str, str]] = Field(default_factory=list)
    conflict_unsafe: list[MasterSyncIssue] = Field(default_factory=list)


def _standard_signature(item: PayrollStandardItemRecord) -> tuple[str, str, str]:
    return item.standard_name, item.section, item.value_type


def _alias_signature(alias: PayrollItemAliasRecord) -> tuple[str, str, str | None]:
    return alias.raw_item_name, alias.standard_item_id, alias.employer_id


def build_master_sync_plan(snapshot: PayrollSheetsSnapshot) -> PayrollMasterSyncPlan:
    """Compare code masters with a Sheets snapshot without writing either source."""
    active_standards = [item for item in snapshot.standard_items if item.active]
    active_aliases = [alias for alias in snapshot.aliases if alias.active]
    if not snapshot.schema_ok:
        return PayrollMasterSyncPlan(
            schema_ok=False,
            code_standard_item_count=len(INITIAL_STANDARD_ITEMS),
            sheets_active_standard_item_count=len(active_standards),
            code_exact_alias_count=len(INITIAL_ALIASES),
            sheets_active_alias_count=len(active_aliases),
            conflict_unsafe=[MasterSyncIssue(
                kind="schema", code_id="payroll_master",
                reason="sheet_schema_not_safe",
            )],
        )
    standards_by_id: dict[str, list[PayrollStandardItemRecord]] = {}
    standards_by_name: dict[str, list[PayrollStandardItemRecord]] = {}
    for item in snapshot.standard_items:
        standards_by_id.setdefault(item.standard_item_id, []).append(item)
        standards_by_name.setdefault(item.standard_name, []).append(item)

    would_add_standards = []
    already_present: list[dict[str, str]] = []
    issues: list[MasterSyncIssue] = []
    safe_standard_ids = set()
    for code_item in INITIAL_STANDARD_ITEMS:
        same_id = standards_by_id.get(code_item.standard_item_id, [])
        if same_id:
            if (len(same_id) == 1 and same_id[0].active
                    and _standard_signature(same_id[0]) == _standard_signature(code_item)):
                safe_standard_ids.add(code_item.standard_item_id)
                already_present.append({"kind": "standard_item", "id": code_item.standard_item_id})
            else:
                issues.append(MasterSyncIssue(
                    kind="standard_item", code_id=code_item.standard_item_id,
                    reason="standard_item_id_collision_or_inactive",
                ))
            continue
        same_name = standards_by_name.get(code_item.standard_name, [])
        if same_name:
            issues.append(MasterSyncIssue(
                kind="standard_item", code_id=code_item.standard_item_id,
                reason="standard_name_used_by_different_id",
            ))
            continue
        would_add_standards.append(code_item)
        safe_standard_ids.add(code_item.standard_item_id)

    aliases_by_id: dict[str, list[PayrollItemAliasRecord]] = {}
    aliases_by_exact_key: dict[tuple[str, str | None], list[PayrollItemAliasRecord]] = {}
    for alias in snapshot.aliases:
        aliases_by_id.setdefault(alias.alias_id, []).append(alias)
        aliases_by_exact_key.setdefault(
            (alias.raw_item_name, alias.employer_id), [],
        ).append(alias)

    would_add_aliases = []
    for code_alias in INITIAL_ALIASES:
        same_id = aliases_by_id.get(code_alias.alias_id, [])
        if same_id:
            if (len(same_id) == 1 and same_id[0].active
                    and _alias_signature(same_id[0]) == _alias_signature(code_alias)):
                already_present.append({"kind": "alias", "id": code_alias.alias_id})
            else:
                issues.append(MasterSyncIssue(
                    kind="alias", code_id=code_alias.alias_id,
                    reason="alias_id_collision_or_inactive",
                ))
            continue
        exact_key = (code_alias.raw_item_name, code_alias.employer_id)
        existing_for_key = aliases_by_exact_key.get(exact_key, [])
        if existing_for_key:
            if (len(existing_for_key) == 1
                    and existing_for_key[0].active
                    and existing_for_key[0].standard_item_id == code_alias.standard_item_id):
                already_present.append({"kind": "alias", "id": code_alias.alias_id})
            else:
                issues.append(MasterSyncIssue(
                    kind="alias", code_id=code_alias.alias_id,
                    reason="exact_alias_collision_or_inactive",
                ))
            continue
        if code_alias.standard_item_id not in safe_standard_ids:
            issues.append(MasterSyncIssue(
                kind="alias", code_id=code_alias.alias_id,
                reason="standard_item_dependency_not_safe",
            ))
            continue
        would_add_aliases.append(code_alias)

    return PayrollMasterSyncPlan(
        schema_ok=True,
        code_standard_item_count=len(INITIAL_STANDARD_ITEMS),
        sheets_active_standard_item_count=len(active_standards),
        code_exact_alias_count=len(INITIAL_ALIASES),
        sheets_active_alias_count=len(active_aliases),
        would_add_standard_items=would_add_standards,
        would_add_aliases=would_add_aliases,
        already_present=already_present,
        conflict_unsafe=issues,
    )


def master_sync_preview(plan: PayrollMasterSyncPlan) -> dict:
    return plan.model_dump(mode="json")


class MasterSyncReader(Protocol):
    def snapshot(self) -> PayrollSheetsSnapshot: ...


class MasterSyncWriter(Protocol):
    def append_standard_items(self, items: list[PayrollStandardItemRecord]) -> None: ...
    def append_aliases(self, aliases: list[PayrollItemAliasRecord]) -> None: ...


class PayrollMasterSyncWriteRepository:
    """Append-only writer restricted to rows selected from the code masters."""

    def __init__(self, spreadsheet_id: str, *, service=None):
        from .google_clients import sheets_service
        self.spreadsheet_id = spreadsheet_id
        self.service = service or sheets_service()

    def _append(self, sheet_key: str, records: list[BaseModel]) -> None:
        if not records:
            return
        from .payroll_storage import PAYROLL_SCHEMAS
        allowed = ({item.standard_item_id for item in INITIAL_STANDARD_ITEMS}
                   if sheet_key == "payroll_standard_items"
                   else {alias.alias_id for alias in INITIAL_ALIASES})
        id_field = "standard_item_id" if sheet_key == "payroll_standard_items" else "alias_id"
        if sheet_key not in {"payroll_standard_items", "payroll_item_aliases"} or any(
            getattr(record, id_field) not in allowed for record in records
        ):
            raise ValueError("Payroll code master以外の行はappendできません")
        columns = PAYROLL_SCHEMAS[sheet_key]
        rows = [[record.model_dump(mode="json").get(column) for column in columns]
                for record in records]
        self.service.spreadsheets().values().append(
            spreadsheetId=self.spreadsheet_id,
            range=f"'{SHEET_TITLES[sheet_key]}'!A:A",
            valueInputOption="RAW", insertDataOption="INSERT_ROWS",
            body={"values": rows},
        ).execute()

    def append_standard_items(self, items: list[PayrollStandardItemRecord]) -> None:
        self._append("payroll_standard_items", items)

    def append_aliases(self, aliases: list[PayrollItemAliasRecord]) -> None:
        self._append("payroll_item_aliases", aliases)


def _candidate_keys(plan: PayrollMasterSyncPlan) -> tuple[set[str], set[str]]:
    return (
        {item.standard_item_id for item in plan.would_add_standard_items},
        {alias.alias_id for alias in plan.would_add_aliases},
    )


def _already_ids(plan: PayrollMasterSyncPlan, kind: str) -> set[str]:
    return {entry["id"] for entry in plan.already_present
            if entry.get("kind") == kind}


def apply_master_sync(
    preview_plan: PayrollMasterSyncPlan,
    reader: MasterSyncReader,
    writer: MasterSyncWriter,
    *,
    confirmed: bool,
) -> dict:
    """Re-read, validate and append only preview-approved code master rows."""
    if not confirmed:
        raise RuntimeError("--apply が必要です")

    result = {
        "applied": False,
        "added_standard_items": [], "added_aliases": [],
        "already_present": [], "skipped": [], "conflicts": [], "errors": [],
        "applied_at": datetime.now(timezone.utc).isoformat(),
    }
    latest = build_master_sync_plan(reader.snapshot())
    initial_standard_ids, initial_alias_ids = _candidate_keys(preview_plan)
    latest_standard_ids, latest_alias_ids = _candidate_keys(latest)
    unexpected = ((latest_standard_ids - initial_standard_ids)
                  or (latest_alias_ids - initial_alias_ids))
    if (not latest.schema_ok or latest.conflict_unsafe or unexpected):
        result["conflicts"] = [issue.model_dump(mode="json")
                               for issue in latest.conflict_unsafe]
        if unexpected:
            result["conflicts"].append({
                "kind": "schema", "code_id": "payroll_master",
                "reason": "unexpected_state_change_since_preview",
            })
        result["skipped"] = [
            {"kind": "standard_item", "id": item.standard_item_id}
            for item in latest.would_add_standard_items
        ] + [{"kind": "alias", "id": alias.alias_id}
             for alias in latest.would_add_aliases]
        return result

    result["already_present"] = latest.already_present
    try:
        if latest.would_add_standard_items:
            writer.append_standard_items(latest.would_add_standard_items)
    except Exception as exc:
        verification = build_master_sync_plan(reader.snapshot())
        remaining, _ = _candidate_keys(verification)
        result["added_standard_items"] = sorted(latest_standard_ids - remaining)
        result["errors"].append({
            "stage": "standard_items", "error": str(exc),
            "outcome": "read_back_reconciled",
            "unconfirmed_ids": sorted(latest_standard_ids & remaining),
        })
        result["skipped"] = [{"kind": "alias", "id": alias.alias_id}
                             for alias in latest.would_add_aliases]
        return result

    # Re-read after the first append. Aliases are never written unless every
    # target standard is now uniquely present and active in Sheets.
    after_standards = build_master_sync_plan(reader.snapshot())
    result["added_standard_items"] = sorted(
        latest_standard_ids & _already_ids(after_standards, "standard_item")
    )
    if (not after_standards.schema_ok or after_standards.conflict_unsafe
            or any(item.standard_item_id in latest_standard_ids
                   for item in after_standards.would_add_standard_items)):
        result["errors"].append({
            "stage": "standard_items", "error": "post_write_verification_failed",
        })
        result["conflicts"] = [issue.model_dump(mode="json")
                               for issue in after_standards.conflict_unsafe]
        result["skipped"] = [{"kind": "alias", "id": alias.alias_id}
                             for alias in latest.would_add_aliases]
        return result
    safe_aliases = [alias for alias in after_standards.would_add_aliases
                    if alias.alias_id in latest_alias_ids]
    try:
        if safe_aliases:
            writer.append_aliases(safe_aliases)
    except Exception as exc:
        verification = build_master_sync_plan(reader.snapshot())
        _, remaining = _candidate_keys(verification)
        attempted = {alias.alias_id for alias in safe_aliases}
        result["added_aliases"] = sorted(attempted - remaining)
        result["errors"].append({
            "stage": "aliases", "error": str(exc),
            "outcome": "read_back_reconciled",
            "unconfirmed_ids": sorted(attempted & remaining),
        })
        return result

    final = build_master_sync_plan(reader.snapshot())
    attempted = {alias.alias_id for alias in safe_aliases}
    result["added_aliases"] = sorted(
        attempted & _already_ids(final, "alias")
    )
    remaining = {alias.alias_id for alias in final.would_add_aliases}
    failed = sorted(attempted & remaining)
    if not final.schema_ok or final.conflict_unsafe or failed:
        result["errors"].append({
            "stage": "aliases", "error": "post_write_verification_failed",
        })
        result["conflicts"] = [issue.model_dump(mode="json")
                               for issue in final.conflict_unsafe]
        return result
    result["applied"] = True
    return result
