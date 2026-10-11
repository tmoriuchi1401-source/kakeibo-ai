from copy import deepcopy
import pytest

from app.category_navigation import (HEADER, RULE_MARKER, NEXT_MARKER,
    representative_summary_formula, representative_summary, return_formula, source_rows)


def workflow(ids=("EXP-2", "EXP-1")):
    return [["control"], [RULE_MARKER], ["header"],
            *[["label", "status", "selected", "", "登録しない", True,
               f"key-{i}", identity, "date", "source", "service", "proof"]
              for i, identity in enumerate(ids)], [], [NEXT_MARKER], ["header"]]


def test_summary_uses_only_h_identity_and_has_no_navigation_or_technical_fields():
    formula = representative_summary_formula()
    assert "INDEX($H:$H,ROW())" in formula
    assert "MATCH(TRUE,ARRAYFORMULA('支出明細'!A:A=id),0)" in formula
    assert 'IF(OR(id="",n<>1),"代表取引を確認できません"' in formula
    for forbidden in ("HYPERLINK", "COUNTIF", "取込ID", "計上状態", "代表取引を見る"):
        assert forbidden not in formula
    for column in "BCDEHI": assert f"'支出明細'!{column}:{column}" in formula
    for formula in (formula, return_formula("synthetic", 20)):
        depth=0; quoted=False
        for char in formula:
            if char == '"': quoted = not quoted
            if not quoted and char == '(': depth += 1
            if not quoted and char == ')': depth -= 1
            assert depth >= 0
        assert not quoted and depth == 0


def test_return_links_scope_literal_ids_and_never_choose_ambiguous_source():
    formula = return_formula("synthetic", 20)
    assert "INDEX($A:$A,ROW())" in formula and "MATCH(TRUE,ARRAYFORMULA(ids=id),0)" in formula
    assert 'IF(n=0,"",IF(n=1,' in formula and "複数候補" in formula
    assert source_rows(workflow(("EXP-*", "EXP-*"))) == [(4, "EXP-*"), (5, "EXP-*")]
    with pytest.raises(ValueError, match="markers_invalid"): source_rows([["no marker"]])


def test_summary_zero_and_empty_fields_are_not_confused():
    row = ["ID", "2025-11-16", "よこちゃん", "自動計上", 0, "", "", "PayPay残高", "PayPay", "", "", "", "active"]
    assert representative_summary(row) == "代表取引\nよこちゃん\n2025/11/16｜0円\nPayPay残高｜PayPay\n内容：自動計上"
    row[2]=""; row[3]=""; row[4]=""
    assert representative_summary(row).endswith("内容：—") and "｜—" in representative_summary(row)


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


@pytest.mark.parametrize("value,shown", [("750", "750円"), ("1234", "1,234円"), ("0", "0円"), ("-1234.5", "-1,235円")])
def test_native_text_amounts_are_verified_without_changing_the_record(value, shown):
    row = ["ID", "2025-11-16", "店", "内容", value, "", "", "現金", "synthetic", "", "", "", "active"]
    before = deepcopy(row)
    assert f"2025/11/16｜{shown}" in representative_summary(row)
    assert row == before


def test_invalid_amount_stops_with_a_safe_reason():
    with pytest.raises(ValueError, match="category_navigation_amount_invalid"):
        representative_summary(["ID", "2025-11-16", "店", "内容", "invalid", "", "", "現金", "synthetic", "", "", "", "active"])
