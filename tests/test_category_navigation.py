from copy import deepcopy
import pytest

from app.category_navigation import (
    HEADER, RULE_MARKER, NEXT_MARKER, navigation_plan,
    representative_transaction_formula, return_formula, source_rows,
)


def workflow(ids=("EXP-2", "EXP-1")):
    return [["control"], [RULE_MARKER], ["header"],
            *[["label", "status", "selected", "", "登録しない", True,
               f"key-{i}", identity, "date", "source", "service", "proof"]
              for i, identity in enumerate(ids)], [], [NEXT_MARKER], ["header"]]


def plan(rows=None, ledger=None, current=None, **kw):
    return navigation_plan(spreadsheet_id="synthetic", ledger_sheet_id=0, workflow_sheet_id=20,
        ledger_ids=ledger if ledger is not None else [["EXP-1"], ["EXP-2"], ["OTHER"]],
        workflow_rows=rows if rows is not None else workflow(),
        current=current or [], install=True, **kw)


def apply(state, requests):
    for request in requests:
        body = request.get("updateCells")
        if not body:
            continue
        start = body["start"]
        table = state[start["sheetId"]]
        r, c = start["rowIndex"], start["columnIndex"]
        while len(table) <= r:
            table.append([])
        while len(table[r]) <= c:
            table[r].append("")
        value = body["rows"][0]["values"][0].get("userEnteredValue", {})
        table[r][c] = next(iter(value.values()), "")


def test_roundtrip_multiple_sources_reorder_regeneration_and_idempotence():
    rows = workflow()
    before = deepcopy(rows)
    state = {0: [["accounting"] * 13 for _ in range(4)], 20: deepcopy(rows)}
    first = plan(rows=rows)
    assert first["return_links"] == 2 and first["formula_rows"] == 3
    apply(state, first["requests"])
    assert [r[:13] for r in state[0]] == [["accounting"] * 13 for _ in range(4)]
    assert [r[1:] for r in state[20]] == [r[1:] for r in before]
    current = [[r[13]] for r in state[0]]
    assert plan(rows=state[20], current=current)["requests"] == []
    # N uses current A/ROW, so partial A:M or whole-row sorting has the same
    # binding. It is independent of expense row, dates, amounts and selection.
    reordered = [state[20][0], *state[20][1:3], state[20][4], state[20][3],
                 *state[20][5:]]
    assert source_rows(reordered) == [(4, "EXP-1"), (5, "EXP-2")]
    assert plan(rows=reordered, ledger=[["OTHER"], ["EXP-2"], ["EXP-1"]],
                current=current)["requests"] == []
    regenerated = workflow(("EXP-1",))
    regenerated[3][0] = representative_transaction_formula("synthetic", 0, 4)
    assert plan(rows=regenerated, current=current)["requests"] == []


def test_removed_and_ambiguous_sources_never_choose_first_silently():
    result = plan(rows=workflow(("EXP-1", "EXP-1")))
    assert result["return_links"] == 1 and result["ambiguous"] == 1
    formula = return_formula("synthetic", 20)
    assert 'IF(n=0,"",IF(n=1,' in formula
    assert '&range=A1","↩ カテゴリ操作の一覧（複数候補）"' in formula
    assert "INDEX($A:$A,ROW())" in formula
    assert "MATCH(TRUE,ARRAYFORMULA(ids=id),0)" in formula
    assert RULE_MARKER in formula and NEXT_MARKER in formula
    # Formula stays in place when the source vanishes, recalculating to blank.
    assert plan(rows=workflow(()))["return_links"] == 0


@pytest.mark.parametrize("kwargs,error", [
    ({"current": [["occupied"]]}, "column_occupied"),
    ({"current": [[HEADER], ["unexpected"]]}, "column_changed"),
    ({"ledger": [["EXP-1"], ["EXP-1"]]}, "duplicate_id"),
    ({"ledger": [["EXP-1"]]}, "representative_missing"),
    ({"rows": [["no markers"]]}, "markers_invalid"),
    ({"canary_ids": ["missing"]}, "canary_unknown"),
])
def test_preflight_stops_without_writes(kwargs, error):
    with pytest.raises(ValueError, match=error):
        plan(**kwargs)


def test_canary_is_bounded_and_forward_missing_duplicate_ids_have_no_link():
    result = plan(canary_ids=["EXP-2"])
    cells = [r["updateCells"] for r in result["requests"] if "updateCells" in r]
    assert [(c["start"]["sheetId"], c["start"]["rowIndex"], c["start"]["columnIndex"])
            for c in cells] == [(0, 0, 13), (0, 2, 13), (20, 3, 0)]
    formula = representative_transaction_formula("synthetic", 0, 999)
    assert "SUMPRODUCT(N('支出明細'!A:A=INDEX($H:$H,ROW())))<>1" in formula
    assert '"代表取引を確認（ID不一致）"' in formula
    assert "999" not in formula and "&range=N" in formula and ":N" not in formula


def test_dynamic_formulas_are_balanced_and_id_comparison_is_literal():
    for formula in (return_formula("synthetic", 20),
                    representative_transaction_formula("synthetic", 0, 4)):
        depth = 0
        quoted = False
        for char in formula:
            if char == '"': quoted = not quoted
            if not quoted and char == '(': depth += 1
            if not quoted and char == ')': depth -= 1
            assert depth >= 0
        assert depth == 0 and not quoted
        assert "COUNTIF" not in formula  # No wildcard interpretation of IDs.


def test_append_only_fills_installed_new_rows_and_rejects_foreign_cells():
    from app.sheets import SheetsDB
    from app.category_navigation import MARKER, VERSION
    class Call:
        def __init__(self, result): self.result = result
        def execute(self, **kw): return self.result
    class Service:
        def __init__(self): self.requests = []; self.current = []
        def spreadsheets(self): return self
        def values(self): return self
        def get(self, **kw):
            assert kw["range"] == "'支出明細'!N8001:N8002"
            assert kw["valueRenderOption"] == "FORMULA"
            return Call({"values": self.current})
        def batchUpdate(self, **kw):
            self.requests.extend(kw["body"]["requests"])
            return Call({})
    service = Service()
    db = SheetsDB("synthetic", service=service)
    db._sheet_metadata_cache = {"sheets": [
        {"properties": {"title": "支出明細", "sheetId": 0}, "developerMetadata": [
            {"metadataKey": MARKER, "metadataValue": VERSION}]},
        {"properties": {"title": "カテゴリ操作", "sheetId": 20}}]}
    db.get_raw = lambda rng: [[HEADER]]
    reply = {"updates": {"updatedRange": "'支出明細'!A8001:M8002"}}
    db._restore_expense_navigation_for_append(reply)
    rule = service.requests[0]["repeatCell"]
    assert rule["range"] == {"sheetId": 0, "startRowIndex": 8000, "endRowIndex": 8002,
                              "startColumnIndex": 13, "endColumnIndex": 14}
    assert rule["cell"]["userEnteredValue"] == {"formulaValue": return_formula("synthetic", 20)}
    service.current = [["someone's value"]]
    with pytest.raises(ValueError, match="column_changed"):
        db._restore_expense_navigation_for_append(reply)
    assert len(service.requests) == 1
