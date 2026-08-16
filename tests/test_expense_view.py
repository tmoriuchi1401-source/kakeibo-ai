from app.expense_view import active_expenses


def row(expense_id,date,amount,status="active"):
    return [expense_id,date,"店舗","商品",amount,"食費","食料品","カード","receipt","","","備考",status]


def test_only_active_and_legacy_blank_expenses_are_visible():
    result=active_expenses([
        row("e1","2026-08-10",100,"active"),
        row("e2","2026-08-11",200,"duplicate_excluded"),
        row("e3","2026-08-12",300,""),
    ])
    assert [x.expense_id for x in result]==["e3","e1"]
    assert sum(x.amount for x in result)==400


def test_expenses_are_newest_first():
    result=active_expenses([
        row("old","2026-01-01",100),
        row("new","2026-12-31",200),
    ])
    assert [x.expense_id for x in result]==["new","old"]
