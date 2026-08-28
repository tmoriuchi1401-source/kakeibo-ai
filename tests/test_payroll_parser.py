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


def test_employment_insurance_reference_amount_is_not_insurance_fee():
    assert candidate("雇用保険") == "employment_insurance"
    assert candidate("雇用保険料") == "employment_insurance"
    assert candidate("雇用保険対象額") is None


def token(text, x, y, confidence=100):
    return PositionedText(text, 1, x, y, 50, 10, confidence)


def legacy_table_tokens():
    return (
        token("基本給", 10, 10), token("独自手当", 70, 10),
        token("支給合計", 130, 10), token("深夜勤務", 190, 10),
        token("300,000", 10, 23), token("320,000", 130, 23),
        token("財形貯蓄", 10, 40), token("持株積立他", 70, 40),
        token("一斉預金", 130, 40), token("組合費", 190, 40),
        token("社宅使用料", 250, 40),
        token("2,000", 150, 53), token("7,100", 207, 53),
    )


def test_positioned_legacy_table_preserves_blank_cell():
    items = parse_positioned_items(legacy_table_tokens())
    by_name = {item.raw_item_name: item for item in items}
    assert by_name["基本給"].value == 300000
    assert by_name["独自手当"].value is None
    assert by_name["独自手当"].needs_review
    assert by_name["支給合計"].value == 320000


def test_legacy_sparse_value_row_uses_unique_nearest_columns():
    by_name = {item.raw_item_name: item
               for item in parse_positioned_items(legacy_table_tokens())}
    assert by_name["一斉預金"].value == 2000
    assert by_name["組合費"].value == 7100
    assert by_name["財形貯蓄"].value is None
    assert by_name["持株積立他"].value is None
    assert by_name["社宅使用料"].value is None


def test_detected_legacy_page_also_pairs_short_summary_row():
    items = parse_positioned_items((
        *legacy_table_tokens(),
        token("総支給額", 10, 70), token("差引支給額", 70, 70),
        token("320,000", 10, 83), token("270,000", 70, 83),
    ))
    by_name = {item.raw_item_name: item for item in items}
    assert by_name["総支給額"].value == 320000
    assert by_name["差引支給額"].value == 270000


def test_legacy_value_is_not_reused_by_multiple_labels():
    items = parse_positioned_items((
        *legacy_table_tokens(),
        token("一斉預金調整", 151, 70), token("組合費調整", 170, 70),
        token("社宅調整", 230, 70), token("支給調整", 290, 70),
        token("3,000", 160, 83),
    ))
    by_name = {item.raw_item_name: item for item in items}
    assert by_name["一斉預金調整"].value is None
    assert by_name["組合費調整"].value is None


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


def test_pdf_value_above_next_label_is_not_reused():
    items = parse_positioned_items((
        token("12,000", 110, 10), token("独自手当", 100, 23),
    ))
    assert len(items) == 1
    assert items[0].value is None
    assert items[0].raw_value is None
    assert items[0].needs_review


def test_ambiguous_ocr_values_above_are_not_confirmed():
    items = parse_positioned_items((
        token("10,000", 100, 10), token("12,000", 108, 10),
        token("独自手当", 100, 23),
    ), ocr=True)
    assert len(items) == 1
    assert items[0].value is None
    assert items[0].needs_review


def test_ocr_value_immediately_above_same_column_is_still_paired():
    items = parse_positioned_items((
        token("12,000", 110, 10), token("独自手当", 100, 23),
    ), ocr=True)
    assert items[0].value == 12000
    assert not items[0].needs_review


def test_pdf_single_below_row_does_not_trigger_legacy_layout():
    items = parse_positioned_items((
        token("独自手当", 100, 10), token("12,000", 110, 25),
    ))
    assert items[0].value is None
    assert items[0].needs_review


def test_legacy_value_beyond_x_threshold_is_not_paired():
    items = parse_positioned_items((
        *legacy_table_tokens(),
        token("手当A", 10, 70), token("手当B", 70, 70),
        token("手当C", 130, 70), token("手当D", 190, 70),
        token("1,000", 10, 83), token("9,000", 400, 83),
    ))
    by_name = {item.raw_item_name: item for item in items}
    assert by_name["手当A"].value == 1000
    assert by_name["手当D"].value is None


def test_legacy_value_with_ambiguous_nearest_column_is_not_paired():
    items = parse_positioned_items((
        *legacy_table_tokens(),
        token("手当A", 100, 70), token("手当B", 140, 70),
        token("手当C", 220, 70), token("手当D", 280, 70),
        token("3,000", 120, 83),
    ))
    by_name = {item.raw_item_name: item for item in items}
    assert by_name["手当A"].value is None
    assert by_name["手当B"].value is None


def test_blank_commuting_allowance_total_stays_without_value():
    item = parse_positioned_items((token("通勤手当計", 100, 10),))[0]
    assert item.standard_item_candidate == "commuting_allowance"
    assert item.raw_value is None
    assert item.value is None
    assert item.needs_review


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
