from app.payroll_ocr import PositionedText
from app.payroll_parser import candidate, parse_items, parse_period_and_date, parse_positioned_items


def test_period_and_date_support_spaced_and_classic_formats():
    assert parse_period_and_date("2022年05月度 （支給日：2022年05月25日）") == (
        "2022-05", "2022-05-25")
    assert parse_period_and_date("2026 08\n給与明細書") == ("2026-08", None)


def test_raw_unknown_item_is_preserved():
    items = parse_items("基本給 独自手当 支給合計\n300,000 12,000 312,000")
    unknown = next(item for item in items if item.raw_item_name == "独自手当")
    assert unknown.section == "earnings"
    assert unknown.value == 12000
    assert unknown.standard_item_candidate is None


def test_standard_candidates_do_not_replace_raw_name():
    items = parse_items("基本給 支給合計\n300,000 300,000")
    assert items[0].raw_item_name == "基本給"
    assert items[0].standard_item_candidate == "basic_pay"
    assert candidate("健康保険料") == "health_insurance"


def token(text, x, y, confidence=100):
    return PositionedText(text, 1, x, y, 50, 10, confidence)


def test_positioned_horizontal_table_preserves_blank_cell():
    items = parse_positioned_items((
        token("基本給", 10, 10), token("独自手当", 100, 10), token("支給合計", 190, 10),
        token("300,000", 10, 25), token("320,000", 190, 25),
    ))
    by_name = {item.raw_item_name: item for item in items}
    assert by_name["基本給"].value == 300000
    assert by_name["独自手当"].value is None
    assert by_name["独自手当"].needs_review
    assert by_name["支給合計"].value == 320000


def test_positioned_multiple_columns_pair_on_same_row():
    items = parse_positioned_items((
        token("基本給", 10, 10), token("300,000", 100, 10),
        token("健康保険料", 250, 10), token("15,000", 360, 10),
    ))
    assert [(item.raw_item_name, item.value) for item in items] == [
        ("基本給", 300000), ("健康保険料", 15000)]


def test_low_ocr_confidence_is_reviewed_without_value():
    item = parse_positioned_items((token("基本給", 10, 10, 45), token("300,000", 100, 10, 90)),
                                  ocr=True)[0]
    assert item.value is None
    assert item.needs_review


def test_unknown_payroll_item_is_retained_with_geometry():
    item = parse_positioned_items((token("独自手当", 10, 10), token("12,000", 100, 10)))[0]
    assert item.raw_item_name == "独自手当"
    assert item.standard_item_candidate is None
    assert (item.page, item.row, item.column) == (1, 0, 0)


def test_non_item_headings_are_not_parsed_as_items():
    items = parse_positioned_items((
        token("給与支給明細書", 10, 10), token("支給日：", 10, 30),
        token("課税処理", 10, 50), token("＜年次有給休暇＞", 10, 70),
        token("基本給", 10, 90), token("300,000", 100, 90),
    ))
    assert [item.raw_item_name for item in items] == ["基本給"]


def test_positioned_value_immediately_above_same_column_is_paired():
    items = parse_positioned_items((
        token("12,000", 110, 10), token("独自手当", 100, 23),
    ))
    assert len(items) == 1
    assert items[0].value == 12000
    assert not items[0].needs_review


def test_ambiguous_values_above_are_not_confirmed():
    items = parse_positioned_items((
        token("10,000", 100, 10), token("12,000", 108, 10),
        token("独自手当", 100, 23),
    ))
    assert len(items) == 1
    assert items[0].value is None
    assert items[0].needs_review


def test_nearby_ocr_tokens_restore_complete_item_name():
    items = parse_positioned_items((
        token("健康", 10, 10), token("保険", 62, 10),
        token("15,000", 120, 10),
    ), ocr=True)
    assert len(items) == 1
    assert items[0].raw_item_name == "健康保険"
    assert items[0].standard_item_candidate == "health_insurance"
    assert items[0].value == 15000


def test_short_ocr_fragments_and_near_duplicate_are_suppressed():
    items = parse_positioned_items((
        token("支給", 10, 10), token("支給", 200, 10),
        token("所得税", 10, 40), token("所得税", 15, 45),
        token("5,000", 100, 40),
    ), ocr=True)
    assert [item.raw_item_name for item in items] == ["所得税"]


def test_existing_standard_items_still_resolve_after_ocr_joining():
    items = parse_positioned_items((
        token("基本給", 10, 10), token("300,000", 100, 10),
        token("健康", 10, 40), token("保険", 62, 40), token("15,000", 120, 40),
        token("所得税", 10, 70), token("8,000", 100, 70),
    ), ocr=True)
    assert [item.standard_item_candidate for item in items] == [
        "basic_pay", "health_insurance", "income_tax",
    ]
