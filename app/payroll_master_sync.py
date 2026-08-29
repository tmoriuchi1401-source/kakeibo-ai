from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from .payroll_sheets import PayrollSheetsSnapshot
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
