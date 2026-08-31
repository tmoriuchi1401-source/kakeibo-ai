from app.payroll_ocr import PositionedText
from app.payroll_parser import (
    candidate,
    parse_items,
    parse_period_and_date,
    parse_positioned_items,
    section_for,
)


def test_period_and_date_support_spaced_and_classic_formats():
    assert parse_period_and_date("2022年05月度 （支給日：2022年05月25日）") == (
        "2022-05", "2022-05-25")
    assert parse_period_and_date("2026 08\n給与明細書") == ("2026-08", None)


def test_raw_unknown_item_is_preserved():
    items = parse_items("基本給 独自手当 支給合計\n300,000 12,000 312,000")
    unknown = next(item for item in items if item.raw_item_name == "独自手当")
    assert unknown.section == "earning"
    assert unknown.value == 12000
    assert unknown.standard_item_candidate is None


def test_standard_candidates_do_not_replace_raw_name():
    items = parse_items("基本給 支給合計\n300,000 300,000")
    assert items[0].raw_item_name == "基本給"
    assert items[0].standard_item_candidate == "basic_pay"
    assert candidate("健康保険料") == "health_insurance"


def test_collective_savings_uses_exact_deduction_mapping():
    assert candidate("一斉預金") == "collective_savings"
    assert section_for("一斉預金") == "deduction"
    assert candidate("一斉預金調整") is None
    assert candidate("財形貯蓄") is None


def test_night_work_pay_does_not_collide_with_attendance_hours():
    assert candidate("深夜勤務") == "night_work_pay"
    assert section_for("深夜勤務") == "earning"
    assert candidate("深夜勤務ｈ") is None
    assert section_for("深夜勤務ｈ") == "attendance"

    item = parse_positioned_items((
        token("深夜勤務", 10, 10), token("2,461", 100, 10),
    ))[0]
    assert item.raw_item_name == "深夜勤務"
    assert item.value == 2461
    assert item.section == "earning"
    assert item.standard_item_candidate == "night_work_pay"


def test_employment_insurance_reference_amount_is_not_insurance_fee():
    assert candidate("雇用保険") == "employment_insurance"
    assert candidate("雇用保険料") == "employment_insurance"
    assert candidate("雇用保険対象額") is None


def test_safe_public_sample_labels_use_distinct_existing_meanings():
    assert (candidate("法定内残業手当"), section_for("法定内残業手当")) == (
        "overtime_pay", "earning")
    assert (candidate("介護保険料"), section_for("介護保険料")) == (
        "nursing_care_insurance", "deduction")
    assert (candidate("標準報酬月額"), section_for("標準報酬月額")) == (
        "standard_monthly_remuneration", "reference")
    assert candidate("課税支給額") is None
    assert candidate("振込支給額") is None


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
    assert by_name["一斉預金"].section == "deduction"
    assert by_name["一斉預金"].standard_item_candidate == "collective_savings"
    assert by_name["一斉預金"].raw_item_name == "一斉預金"


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


def test_attendance_days_does_not_confirm_adjacent_money_value():
    item = parse_positioned_items((
        token("出勤日数（平日）", 10, 10), token("500,000", 120, 10),
    ))[0]
    assert item.section == "attendance"
    assert item.value is None
    assert item.raw_value is None
    assert item.needs_review


def test_attendance_hours_does_not_confirm_adjacent_money_value():
    items = parse_positioned_items((
        token("所定時間（平日）", 10, 10), token("30,865", 120, 10),
        token("所定外時間（平日）", 10, 30), token("45,750", 120, 30),
    ))
    assert all(item.section == "attendance" for item in items)
    assert all(item.value is None and item.raw_value is None for item in items)
    assert all(item.needs_review for item in items)


def test_explicit_attendance_units_remain_confirmed():
    items = parse_positioned_items((
        token("出勤日数（平日）", 10, 10), token("20.0日", 120, 10),
        token("所定時間（平日）", 10, 30), token("160.00時間", 120, 30),
        token("所定外時間（平日）", 10, 50), token("10.00時間", 120, 50),
    ))
    assert [(item.value, item.raw_value, item.needs_review) for item in items] == [
        (20, "20.0日", False),
        (160, "160.00時間", False),
        (10, "10.00時間", False),
    ]


def test_money_sections_keep_confirming_money_values():
    items = parse_positioned_items((
        token("基本給", 10, 10), token("500,000", 120, 10),
        token("健康保険料", 10, 30), token("30,865", 120, 30),
    ))
    assert [(item.section, item.value, item.needs_review) for item in items] == [
        ("earning", 500000, False),
        ("deduction", 30865, False),
    ]


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


def test_pdf_summary_values_directly_below_are_recovered_when_consistent():
    items = parse_positioned_items((
        token("総支給額", 10, 10), token("控除合計", 80, 10), token("差引支給額", 150, 10),
        token("530,865", 10, 22), token("94,185", 80, 22), token("436,680", 150, 22),
    ))
    assert {item.standard_item_candidate: item.value for item in items} == {
        "gross_pay": 530865, "total_deductions": 94185, "net_pay": 436680,
    }


def test_pdf_summary_below_rejects_multiple_candidates():
    item = parse_positioned_items((
        token("総支給額", 10, 10), token("530,865", 10, 21), token("531,000", 10, 24),
    ))[0]
    assert item.value is None and item.needs_review


def test_pdf_summary_below_does_not_reuse_one_value():
    items = parse_positioned_items((
        token("総支給額", 10, 10), token("支給額計", 12, 10), token("530,865", 11, 22),
    ))
    assert all(item.value is None for item in items)


def test_pdf_summary_below_rejects_distant_value():
    item = parse_positioned_items((token("総支給額", 10, 10), token("530,865", 10, 36)))[0]
    assert item.value is None


def test_pdf_summary_below_rejects_value_outside_column():
    item = parse_positioned_items((token("総支給額", 10, 10), token("530,865", 55, 22)))[0]
    assert item.value is None


def test_pdf_summary_below_rejects_value_on_other_page():
    value = PositionedText("530,865", 2, 10, 22, 50, 10, 100)
    item = parse_positioned_items((token("総支給額", 10, 10), value))[0]
    assert item.value is None


def test_pdf_summary_below_keeps_one_safe_value():
    item = parse_positioned_items((token("総支給額", 10, 10), token("530,865", 10, 22)))[0]
    assert item.value == 530865 and not item.needs_review


def test_pdf_summary_below_does_not_apply_to_excluded_alias():
    item = parse_positioned_items((token("差引不足額", 10, 10), token("10,000", 10, 22)))[0]
    assert item.value is None


def test_pdf_summary_below_rejects_inconsistent_complete_totals():
    items = parse_positioned_items((
        token("総支給額", 10, 10), token("控除合計", 80, 10), token("差引支給額", 150, 10),
        token("530,865", 10, 22), token("94,185", 80, 22), token("400,000", 150, 22),
    ))
    assert all(item.value is None for item in items)


def test_pdf_summary_below_yields_to_closer_other_label():
    items = parse_positioned_items((
        token("総支給額", 10, 10), token("課税支給額", 10, 13), token("530,865", 10, 25),
    ))
    summary = next(item for item in items if item.raw_item_name == "総支給額")
    assert summary.value is None


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


def test_ocr_dot_group_money_uses_near_value_without_crossing_ytd_column():
    items = parse_positioned_items((
        token("非課税合計", 10, 10),
        token("38.430", 100, 10, 83),
        PositionedText("|", 1, 180, 0, 3, 30, 90),
        token("総支給額累計", 200, 10),
        token("7,087,172", 300, 10, 70),
    ), ocr=True)
    by_name = {item.raw_item_name: item for item in items}
    current = by_name["非課税合計"]
    assert current.raw_value == "38.430"
    assert current.value == 38430
    assert not current.needs_review
    assert by_name["総支給額累計"].raw_value == "7,087,172"


def test_ocr_dot_group_money_requires_strict_token_and_confidence():
    for raw_value, confidence in (
        ("38.430", 59),
        ("2011.300", 95),
        ("409.257|", 95),
    ):
        item = parse_positioned_items((
            token("住民税", 10, 10), token(raw_value, 100, 10, confidence),
        ), ocr=True)[0]
        assert item.raw_value is None
        assert item.value is None
        assert item.needs_review


def test_ocr_dot_group_money_is_not_used_for_attendance():
    item = parse_positioned_items((
        token("残業時間", 10, 10), token("7.500", 100, 10),
    ), ocr=True)[0]
    assert item.section == "attendance"
    assert item.raw_value is None
    assert item.value is None
    assert item.needs_review


def test_ocr_horizontal_value_does_not_cross_vertical_rule():
    item = parse_positioned_items((
        token("独自手当", 10, 10),
        PositionedText("｜", 1, 80, 0, 3, 30, 90),
        token("12,000", 120, 10),
    ), ocr=True)[0]
    assert item.raw_value is None
    assert item.value is None
    assert item.needs_review


def test_ocr_horizontal_value_belongs_to_nearest_left_payroll_label_only():
    items = parse_positioned_items((
        token("独自手当", 10, 10), token("所得税", 100, 10),
        token("12,000", 180, 10),
    ), ocr=True)
    by_name = {item.raw_item_name: item for item in items}
    assert by_name["独自手当"].value is None
    assert by_name["独自手当"].needs_review
    assert by_name["所得税"].value == 12000
    assert not by_name["所得税"].needs_review


def test_ocr_fragmented_generic_suffix_does_not_steal_value_ownership():
    items = parse_positioned_items((
        token("厚生年金", 10, 10), token("保険", 65, 9),
        token("137.250", 150, 10),
        token("課税", 10, 40), token("合計", 65, 39),
        token("4,930,861", 150, 40),
    ), ocr=True)
    by_name = {item.raw_item_name: item for item in items}
    assert by_name["厚生年金"].value == 137250
    assert by_name["課税"].value == 4930861


def test_ocr_horizontal_ambiguous_ownership_is_not_confirmed():
    items = parse_positioned_items((
        token("独自手当", 10, 10), token("所得税", 15, 10),
        token("12,000", 110, 10),
    ), ocr=True)
    assert all(item.value is None and item.needs_review for item in items)


def test_dot_group_money_does_not_change_pdf_text_path():
    dot = parse_positioned_items((
        token("住民税", 10, 10), token("51.500", 100, 10),
    ))[0]
    comma = parse_positioned_items((
        token("住民税", 10, 10), token("51,500", 100, 10),
    ))[0]
    assert dot.value is None and dot.needs_review
    assert comma.value == 51500 and not comma.needs_review


def test_existing_comma_money_still_pairs_in_ocr():
    item = parse_positioned_items((
        token("住民税", 10, 10), token("51,500", 100, 10),
    ), ocr=True)[0]
    assert item.raw_value == "51,500"
    assert item.value == 51500
    assert not item.needs_review


def fragmented_employment_insurance(*extra, value="10,056", value_confidence=92):
    return (
        PositionedText("雇用", 1, 1310, 235, 40, 20, 86),
        PositionedText("保険", 1, 1358, 234, 37, 21, 96),
        PositionedText("料", 1, 1405, 236, 12, 18, 59),
        *extra,
        PositionedText(value, 1, 1725, 229, 67, 34, value_confidence),
    )


def local_fragmented_employment_insurance(
    *extra,
    xs=(10, 34, 58),
    ys=(10, 10, 10),
    confidences=(86, 96, 59),
    value="10,056",
    value_confidence=92,
    value_x=120,
    value_y=10,
):
    return (
        PositionedText("雇用", 1, xs[0], ys[0], 20, 10, confidences[0]),
        PositionedText("保険", 1, xs[1], ys[1], 20, 10, confidences[1]),
        PositionedText("料", 1, xs[2], ys[2], 8, 10, confidences[2]),
        *extra,
        PositionedText(value, 1, value_x, value_y, 50, 10, value_confidence),
    )


def test_exact_known_ocr_label_can_use_one_low_confidence_suffix():
    items = parse_positioned_items(fragmented_employment_insurance(), ocr=True)

    assert [(item.raw_item_name, item.value, item.needs_review) for item in items] == [
        ("雇用保険料", 10056, False),
    ]
    item = items[0]
    assert item.standard_item_candidate == "employment_insurance"
    assert item.section == "deduction"
    assert item.raw_value == "10,056"
    assert item.confidence == 59


def test_exact_ocr_label_reconstruction_requires_local_ordered_tokens():
    distant = (
        PositionedText("雇用", 1, 1310, 235, 40, 20, 86),
        PositionedText("保険", 1, 1500, 234, 37, 21, 96),
        PositionedText("料", 1, 1545, 236, 12, 18, 59),
        PositionedText("10,056", 1, 1725, 229, 67, 34, 92),
    )
    wrong_order = (
        PositionedText("雇用", 1, 1310, 235, 40, 20, 86),
        PositionedText("料", 1, 1358, 236, 12, 18, 59),
        PositionedText("保険", 1, 1378, 234, 37, 21, 96),
        PositionedText("10,056", 1, 1725, 229, 67, 34, 92),
    )
    for tokens in (distant, wrong_order):
        assert not any(
            item.standard_item_candidate == "employment_insurance"
            and item.value == 10056 and not item.needs_review
            for item in parse_positioned_items(tokens, ocr=True)
        )


def test_exact_ocr_label_reconstruction_requires_one_common_line():
    boundary = local_fragmented_employment_insurance(ys=(10, 12.75, 15.5))
    beyond = local_fragmented_employment_insurance(ys=(10, 12.8, 15.6))

    for tokens, expected in ((boundary, True), (beyond, False)):
        items = parse_positioned_items(tokens, ocr=True)
        confirmed = [item for item in items
                     if item.standard_item_candidate == "employment_insurance"
                     and not item.needs_review]
        assert bool(confirmed) is expected


def test_exact_ocr_label_reconstruction_rejects_cumulative_y_drift():
    items = parse_positioned_items(
        local_fragmented_employment_insurance(ys=(0, 5, 10), value_y=0), ocr=True)

    assert not any(item.standard_item_candidate == "employment_insurance"
                   and item.value == 10056 and not item.needs_review for item in items)


def test_exact_ocr_label_reconstruction_honors_token_gap_boundary():
    boundary = local_fragmented_employment_insurance(xs=(10, 54, 98), value_x=160)
    beyond = local_fragmented_employment_insurance(xs=(10, 54.1, 98.1), value_x=160)

    for tokens, expected in ((boundary, True), (beyond, False)):
        items = parse_positioned_items(tokens, ocr=True)
        confirmed = [item for item in items
                     if item.standard_item_candidate == "employment_insurance"
                     and not item.needs_review]
        assert bool(confirmed) is expected


def test_exact_ocr_label_reconstruction_requires_every_component():
    cases = (
        (PositionedText("保険", 1, 34, 10, 20, 10, 96),
         PositionedText("料", 1, 58, 10, 8, 10, 59)),
        (PositionedText("雇用", 1, 10, 10, 20, 10, 86),
         PositionedText("料", 1, 58, 10, 8, 10, 59)),
        (PositionedText("雇用", 1, 10, 10, 20, 10, 86),
         PositionedText("保険", 1, 34, 10, 20, 10, 59)),
    )
    for parts in cases:
        items = parse_positioned_items((*parts, token("10,056", 120, 10, 92)), ocr=True)
        assert not any(item.standard_item_candidate == "employment_insurance"
                       and not item.needs_review for item in items)


def test_exact_ocr_label_reconstruction_rejects_duplicate_or_unrelated_words():
    duplicate = local_fragmented_employment_insurance(
        PositionedText("保険", 1, 58, 10, 20, 10, 96),
        xs=(10, 34, 82), value_x=150,
    )
    unrelated = local_fragmented_employment_insurance(
        PositionedText("対象", 1, 70, 10, 20, 10, 96),
    )
    for tokens in (duplicate, unrelated):
        items = parse_positioned_items(tokens, ocr=True)
        assert not any(item.standard_item_candidate == "employment_insurance"
                       and not item.needs_review for item in items)


def test_exact_ocr_label_reconstruction_does_not_cross_component_rule():
    items = parse_positioned_items(local_fragmented_employment_insurance(
        PositionedText("|", 1, 55, 0, 3, 30, 90),
    ), ocr=True)

    assert not any(item.standard_item_candidate == "employment_insurance"
                   and not item.needs_review for item in items)


def test_exact_ocr_label_reconstruction_rejects_adjacent_unknown_text():
    items = parse_positioned_items(fragmented_employment_insurance(
        PositionedText("X", 1, 1422, 235, 10, 20, 95),
    ), ocr=True)

    assert not any(item.value == 10056 and not item.needs_review for item in items)


def test_low_confidence_exact_ocr_label_requires_strong_comma_money():
    for tokens in (
        fragmented_employment_insurance(value_confidence=79),
        fragmented_employment_insurance(value="10056"),
    ):
        item = next(item for item in parse_positioned_items(tokens, ocr=True)
                    if item.raw_item_name == "雇用保険料")
        assert item.raw_value is None
        assert item.value is None
        assert item.needs_review


def test_low_confidence_exact_ocr_label_confidence_boundaries():
    for confidence, expected in ((49, False), (50, True), (59, True), (60, True)):
        items = parse_positioned_items(
            local_fragmented_employment_insurance(confidences=(86, 96, confidence)), ocr=True)
        confirmed = [item for item in items if item.standard_item_candidate == "employment_insurance"
                     and not item.needs_review]
        assert bool(confirmed) is expected

    for confidence, expected in ((79, False), (80, True)):
        items = parse_positioned_items(
            local_fragmented_employment_insurance(confidences=(confidence, 96, 59)), ocr=True)
        confirmed = [item for item in items if item.standard_item_candidate == "employment_insurance"
                     and not item.needs_review]
        assert bool(confirmed) is expected

    for confidence, expected in ((79, False), (80, True)):
        items = parse_positioned_items(
            local_fragmented_employment_insurance(value_confidence=confidence), ocr=True)
        confirmed = [item for item in items if item.standard_item_candidate == "employment_insurance"
                     and not item.needs_review]
        assert bool(confirmed) is expected


def test_low_confidence_exact_ocr_label_does_not_cross_column_rule():
    item = next(item for item in parse_positioned_items(
        fragmented_employment_insurance(
            PositionedText("|", 1, 1600, 220, 3, 50, 90),
        ), ocr=True
    ) if item.raw_item_name == "雇用保険料")

    assert item.value is None
    assert item.needs_review


def test_low_confidence_exact_ocr_label_rejects_multiple_values():
    item = next(item for item in parse_positioned_items(
        fragmented_employment_insurance(
            PositionedText("12,000", 1, 2200, 229, 67, 34, 95),
        ), ocr=True
    ) if item.raw_item_name == "雇用保険料")

    assert item.value is None
    assert item.needs_review


def test_low_confidence_exact_ocr_label_requires_no_other_nearby_amount():
    horizontal_and_below = local_fragmented_employment_insurance(
        PositionedText("12,000", 1, 30, 30, 50, 10, 95),
    )
    far = local_fragmented_employment_insurance(value_x=400)
    for tokens in (horizontal_and_below, far):
        items = parse_positioned_items(tokens, ocr=True)
        assert not any(item.standard_item_candidate == "employment_insurance"
                       and not item.needs_review for item in items)


def test_low_confidence_exact_ocr_label_rejects_two_reconstructed_owners():
    tokens = (
        *local_fragmented_employment_insurance(value_x=220)[:-1],
        PositionedText("健康", 1, 100, 10, 20, 10, 86),
        PositionedText("保険", 1, 124, 10, 20, 10, 96),
        PositionedText("料", 1, 148, 10, 8, 10, 59),
        PositionedText("10,056", 1, 220, 10, 50, 10, 92),
    )
    items = parse_positioned_items(tokens, ocr=True)

    assert not any(item.value == 10056 and not item.needs_review for item in items)


def test_low_confidence_exact_ocr_label_requires_unambiguous_ownership():
    tokens = (
        PositionedText("雇用", 1, 10, 10, 20, 10, 86),
        PositionedText("保険", 1, 34, 10, 20, 10, 96),
        PositionedText("料", 1, 58, 10, 8, 10, 59),
        PositionedText("所得税", 1, 17, 10, 48, 10, 95),
        PositionedText("10,056", 1, 120, 10, 50, 10, 92),
    )
    items = parse_positioned_items(tokens, ocr=True)

    assert not any(item.value == 10056 and not item.needs_review for item in items)


def test_exact_ocr_label_override_does_not_change_other_paths():
    pdf_item = parse_positioned_items((
        PositionedText("雇用保険料", 1, 10, 10, 70, 10, 59),
        PositionedText("10,056", 1, 100, 10, 50, 10, 92),
    ))[0]
    low_ocr = parse_positioned_items((
        PositionedText("住民税", 1, 10, 10, 50, 10, 59),
        PositionedText("51,500", 1, 100, 10, 50, 10, 92),
    ), ocr=True)[0]
    regular_ocr = parse_positioned_items((
        token("健康保険料", 10, 10), token("67,669", 100, 10),
        token("厚生年金", 10, 40), token("137,250", 100, 40),
        token("住民税", 10, 70), token("51,500", 100, 70),
    ), ocr=True)

    assert pdf_item.value == 10056 and not pdf_item.needs_review
    assert low_ocr.value is None and low_ocr.needs_review
    assert [item.value for item in regular_ocr] == [67669, 137250, 51500]
    assert not any(item.needs_review for item in regular_ocr)


def test_split_high_confidence_label_keeps_normal_ocr_pairing_path():
    items = parse_positioned_items(
        local_fragmented_employment_insurance(confidences=(86, 96, 60)), ocr=True)

    assert [(item.raw_item_name, item.value, item.needs_review) for item in items] == [
        ("雇用保険料", 10056, False),
    ]


def test_positioned_ytd_block_is_classified_without_changing_current_income_tax():
    items = parse_positioned_items((
        token("所得税", 20, 40), token("16,840", 100, 40),
        token("本年累計", 300, 10),
        token("課税支給額", 300, 30), token("2,061,730", 390, 30),
        token("社会保険料", 302, 50), token("234,765", 390, 50),
        token("所得税", 301, 70), token("69,310", 390, 70),
    ))

    current_tax = next(item for item in items if item.value == 16840)
    assert (current_tax.standard_item_candidate, current_tax.section) == (
        "income_tax", "deduction")

    expected = {
        2061730: ("ytd_taxable_amount", "reference"),
        234765: ("ytd_social_insurance", "reference"),
        69310: ("ytd_income_tax", "reference"),
    }
    assert {
        item.value: (item.standard_item_candidate, item.section)
        for item in items if item.value in expected
    } == expected


def test_positioned_ytd_heading_without_complete_group_does_not_reclassify():
    items = parse_positioned_items((
        token("本年累計", 300, 10),
        token("課税支給額", 300, 30), token("2,061,730", 390, 30),
        token("所得税", 301, 50), token("69,310", 390, 50),
    ))
    by_value = {item.value: item for item in items}

    assert by_value[2061730].standard_item_candidate is None
    assert by_value[2061730].section == "unknown"
    assert (by_value[69310].standard_item_candidate, by_value[69310].section) == (
        "income_tax", "deduction")


def test_positioned_income_tax_outside_ytd_column_remains_deduction():
    items = parse_positioned_items((
        token("本年累計", 300, 10),
        token("課税支給額", 300, 30), token("2,061,730", 390, 30),
        token("社会保険料", 300, 50), token("234,765", 390, 50),
        token("所得税", 300, 70), token("69,310", 390, 70),
        token("所得税", 20, 70), token("16,840", 100, 70),
    ))
    outside = next(item for item in items if item.value == 16840)

    assert (outside.standard_item_candidate, outside.section) == (
        "income_tax", "deduction")


def test_pdf_near_identical_label_bbox_with_same_value_is_deduplicated():
    items = parse_positioned_items((
        token("支給合計", 10, 10), token("支給合計", 10.5, 10.2),
        token("300,000", 100, 10),
    ))

    assert [(item.raw_item_name, item.value) for item in items] == [
        ("支給合計", 300000),
    ]


def test_pdf_same_label_at_distant_bboxes_is_preserved():
    items = parse_positioned_items((
        token("支給合計", 10, 10), token("300,000", 100, 10),
        token("支給合計", 250, 10), token("400,000", 340, 10),
    ))

    assert [(item.raw_item_name, item.value) for item in items] == [
        ("支給合計", 300000), ("支給合計", 400000),
    ]


def test_pdf_near_identical_labels_with_different_value_regions_are_preserved():
    labels = (
        PositionedText("支給合計", 1, 10, 10, 50, 10, 100),
        PositionedText("支給合計", 1, 10, 9, 50, 10, 100),
    )
    items = parse_positioned_items((
        *labels,
        PositionedText("300,000", 1, 100, 16, 50, 10, 100),
        PositionedText("400,000", 1, 100, 3, 50, 10, 100),
    ))

    assert sorted(item.value for item in items) == [300000, 400000]


def test_duplicate_ytd_heading_tokens_are_not_items_and_keep_ytd_detection():
    items = parse_positioned_items((
        token("本年累計", 300, 10), token("本年累計", 300.5, 10.2),
        token("課税支給額", 300, 30), token("2,061,730", 390, 30),
        token("社会保険料", 300, 50), token("234,765", 390, 50),
        token("所得税", 300, 70), token("69,310", 390, 70),
    ))

    assert all(item.raw_item_name != "本年累計" for item in items)
    assert {item.standard_item_candidate for item in items} == {
        "ytd_taxable_amount", "ytd_social_insurance", "ytd_income_tax",
    }


def test_pdf_distant_other_tax_cells_are_both_preserved():
    items = parse_positioned_items((
        token("他課税", 633.69, 425.44), token("10,000", 710, 425.44),
        token("他課税", 411.16, 553.67), token("20,000", 490, 553.67),
    ))

    assert sorted(item.value for item in items) == [10000, 20000]
