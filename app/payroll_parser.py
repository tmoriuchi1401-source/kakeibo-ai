from __future__ import annotations

import re
from datetime import date

from .payroll_models import PayrollItem
from .payroll_ocr import PositionedText


STANDARD_NAMES = {
    "基本給": "basic_pay", "早出残業": "overtime_pay", "時間外労働": "overtime_pay",
    "時間外手当": "overtime_pay", "法定内残業手当": "overtime_pay",
    "通勤手当": "commuting_allowance",
    "健康保険": "health_insurance", "健康保険料": "health_insurance",
    "介護保険": "nursing_care_insurance", "介護保険料": "nursing_care_insurance",
    "厚生年金": "employees_pension",
    "厚生年金保険": "employees_pension", "雇用保険": "employment_insurance",
    "雇用保険料": "employment_insurance", "所得税": "income_tax",
    "源泉所得額": "income_tax", "住民税": "resident_tax",
    "支給合計": "gross_pay", "総支給額": "gross_pay", "支給額計": "gross_pay",
    "控除合計": "total_deductions", "控除額計": "total_deductions",
    "差引支給額": "net_pay", "差引不足額": "net_pay",
    "一斉預金": "collective_savings", "深夜勤務": "night_work_pay",
    "標準報酬月額": "standard_monthly_remuneration",
}

ITEM_TERMS = (
    "給", "手当", "保険", "年金", "税", "控除", "日数", "時間", "残業", "勤務",
    "休", "積立", "貯蓄", "費", "合計", "対象", "月額", "累計", "調整", "販売",
    "教育", "組合", "送金", "課税", "基準", "持株", "財形", "社宅", "預金",
    "出勤", "欠勤", "支給", "報酬",
)

LOGICAL_ROW_MIN_Y_GAP = 10
LOGICAL_ROW_MAX_Y_GAP = 15
LOGICAL_ROW_MAX_X_GAP = 25
LOGICAL_ROW_MIN_X_MARGIN = 8

YTD_ITEM_CANDIDATES = {
    "課税支給額": "ytd_taxable_amount",
    "社会保険料": "ytd_social_insurance",
    "所得税": "ytd_income_tax",
}

SUMMARY_LABELS = {
    "支給合計", "総支給額", "支給額計", "控除合計", "控除額計", "差引支給額",
}

OCR_OWNERSHIP_FRAGMENTS = {
    "給与", "出勤", "手当", "保険", "控除", "支給", "合計", "対象", "累計",
}


def compact(value: str) -> str:
    return re.sub(r"[\s　]+", "", value).replace("，", ",")


def amounts(value: str) -> list[int]:
    return [int(x.replace(",", "")) for x in re.findall(r"(?<![\d.])\d{1,3}(?:,\d{3})+(?!\d)", value)]


def _ocr_dot_amounts(token: PositionedText) -> list[int]:
    """Treat only a complete, confident OCR thousands-group token as money."""
    if token.confidence < 60:
        return []
    value = token.text.strip()
    if re.fullmatch(r"\d{1,3}(?:\.\d{3})+", value) is None:
        return []
    return [int(value.replace(".", ""))]


def candidate(name: str) -> str | None:
    normalized = compact(name).replace("（非", "")
    for label, standard in STANDARD_NAMES.items():
        if compact(label) == normalized:
            return standard
    # OCR/PDF cells may truncate the suffix while retaining an unambiguous label.
    reference_suffixes = ("対象額", "対象支給額", "累計", "月額")
    for label in ("通勤手当", "健康保険", "厚生年金", "雇用保険"):
        if (normalized.startswith(compact(label))
                and not normalized.endswith(reference_suffixes)):
            return STANDARD_NAMES[label]
    return None


def section_for(name: str, current: str = "unknown") -> str:
    c = candidate(name)
    if c in {"gross_pay", "total_deductions", "net_pay",
             "standard_monthly_remuneration"}: return "reference"
    if c in {"health_insurance", "nursing_care_insurance", "employees_pension",
             "employment_insurance", "income_tax", "resident_tax",
             "collective_savings"}: return "deduction"
    if c in {"basic_pay", "overtime_pay", "commuting_allowance",
             "night_work_pay"}: return "earning"
    if any(k in name for k in ("日数", "時間", "残業ｈ", "勤務ｈ")): return "attendance"
    return current


def _attendance_value_type(name: str) -> str | None:
    normalized = compact(name).lower()
    if "日数" in normalized:
        return "days"
    if "時間" in normalized or any(term in normalized for term in ("残業h", "勤務h", "残業ｈ", "勤務ｈ")):
        return "hours"
    return None


def _explicit_attendance_value(value: str, value_type: str | None) -> int | float | None:
    unit = "日" if value_type == "days" else "時間" if value_type == "hours" else None
    if unit is None:
        return None
    match = re.fullmatch(rf"\s*([+-]?\d+(?:\.\d+)?)\s*{unit}\s*", value)
    if not match:
        return None
    number = float(match.group(1))
    return int(number) if number.is_integer() else number


def _attendance_value_conflicts(name: str, raw_value: str) -> bool:
    """Reject an untyped money cell for a label that expects days or hours."""
    value_type = _attendance_value_type(name)
    if value_type is None or _explicit_attendance_value(raw_value, value_type) is not None:
        return False
    return re.fullmatch(r"\s*[+-]?\d{1,3}(?:,\d{3})+(?:円)?\s*", raw_value) is not None


def _is_explicit_attendance_quantity(value: str) -> bool:
    return any(_explicit_attendance_value(value, value_type) is not None
               for value_type in ("days", "hours"))


def _is_non_item_heading(name: str) -> bool:
    normalized = compact(name).strip("()（）:：<>＜＞")
    return (
        normalized in {"給与明細書", "給与支給明細書", "課税処理", "年次有給休暇", "本年累計"}
        or normalized.startswith("支給日")
    )


def _is_plausible_item_label(name: str) -> bool:
    return bool(
        candidate(name)
        or section_for(name) == "attendance"
        or any(term in name for term in ITEM_TERMS)
    )


def _same_ocr_line(left: PositionedText, right: PositionedText) -> bool:
    return (
        left.page == right.page
        and abs(left.y - right.y) <= max(left.height, right.height) * .65
    )


def _crosses_ocr_vertical_rule(
    label: PositionedText,
    number: PositionedText,
    tokens: tuple[PositionedText, ...],
) -> bool:
    left = label.x + label.width
    top = min(label.y, number.y)
    bottom = max(label.y + label.height, number.y + number.height)
    return any(
        token.page == label.page
        and re.fullmatch(r"[|｜]+", token.text.strip()) is not None
        and left <= token.x <= number.x
        and token.y <= bottom
        and token.y + token.height >= top
        for token in tokens
    )


def _owns_ocr_horizontal_value(
    label: PositionedText,
    number: PositionedText,
    labels: list[PositionedText],
    tokens: tuple[PositionedText, ...],
) -> bool:
    """Assign an OCR value to at most one unambiguous label in its table cell."""
    owners = []
    for other in labels:
        if (compact(other.text) in OCR_OWNERSHIP_FRAGMENTS
                or not _is_plausible_item_label(other.text)
                or not _same_ocr_line(other, number)
                or number.x < other.x + other.width - 3
                or _crosses_ocr_vertical_rule(other, number, tokens)):
            continue
        owners.append((number.x - (other.x + other.width), other))
    owners.sort(key=lambda entry: entry[0])
    if not owners or owners[0][1] is not label:
        return False
    if len(owners) > 1:
        margin = owners[1][0] - owners[0][0]
        minimum_margin = max(8, max(label.height, owners[1][1].height) * .4)
        if margin < minimum_margin:
            return False
    return True


def _mark_ytd_block(
    tokens: tuple[PositionedText, ...],
    items: list[PayrollItem],
) -> None:
    """Mark only a complete, compact three-row block below an exact YTD heading."""
    headings = [token for token in tokens if compact(token.text) == "本年累計"]
    required_names = set(YTD_ITEM_CANDIDATES)
    for heading in headings:
        page_label_tokens = [
            token for token in tokens
            if token.page == heading.page and compact(token.text) in required_names
        ]
        row_scale = max(
            [heading.height, *(token.height for token in page_label_tokens)],
        )
        column_scale = max(
            [heading.width, *(token.width for token in page_label_tokens)],
        )
        nearby = [
            item for item in items
            if item.page == heading.page
            and compact(item.raw_item_name) in required_names
            and item.x is not None and item.y is not None
            and heading.y <= item.y
            and item.y - (heading.y + heading.height) <= row_scale * 8
            and heading.x - column_scale <= item.x <= heading.x + column_scale
        ]
        grouped = {
            name: [item for item in nearby if compact(item.raw_item_name) == name]
            for name in required_names
        }
        # Missing or duplicate labels make the block boundary ambiguous.
        if any(len(matches) != 1 for matches in grouped.values()):
            continue
        block = [grouped[name][0] for name in YTD_ITEM_CANDIDATES]
        if any(item.needs_review or item.value is None for item in block):
            continue
        xs = [item.x or 0 for item in block]
        ys = [item.y or 0 for item in block]
        ordered_rows = ys == sorted(ys) and len(set(ys)) == len(ys)
        same_column = max(xs) - min(xs) <= column_scale * .5
        compact_rows = max(
            right - left for left, right in zip(sorted(ys), sorted(ys)[1:])
        ) <= row_scale * 3
        if not (ordered_rows and same_column and compact_rows):
            continue
        for item in block:
            item.standard_item_candidate = YTD_ITEM_CANDIDATES[compact(item.raw_item_name)]
            item.section = "reference"


def _nearly_same_bbox(left: PositionedText, right: PositionedText) -> bool:
    """Compare text boxes using strict, size-relative tolerances."""
    if left.page != right.page:
        return False
    max_width = max(left.width, right.width)
    max_height = max(left.height, right.height)
    if max_width <= 0 or max_height <= 0:
        return False
    if (abs(left.width - right.width) > max_width * .1
            or abs(left.height - right.height) > max_height * .1):
        return False

    overlap_width = max(0.0, min(left.x + left.width, right.x + right.width)
                        - max(left.x, right.x))
    overlap_height = max(0.0, min(left.y + left.height, right.y + right.height)
                         - max(left.y, right.y))
    intersection = overlap_width * overlap_height
    union = left.width * left.height + right.width * right.height - intersection
    iou = intersection / union if union > 0 else 0.0
    center_x_gap = abs((left.x + left.width / 2) - (right.x + right.width / 2))
    center_y_gap = abs((left.y + left.height / 2) - (right.y + right.height / 2))
    close_centers = (
        center_x_gap <= min(left.width, right.width) * .1
        and center_y_gap <= min(left.height, right.height) * .1
    )
    return iou >= .8 or close_centers


def _deduplicate_pdf_labels(entries):
    """Drop only near-identical PDF labels with the same resolved value source."""
    deduplicated = []
    for item, label, number, ambiguous in entries:
        duplicate = next((existing for existing in deduplicated
                          if existing[0].page == item.page
                          and compact(existing[0].raw_item_name) == compact(item.raw_item_name)
                          and _nearly_same_bbox(existing[1], label)
                          and existing[2] == number
                          and not existing[3] and not ambiguous
                          and existing[0].raw_value == item.raw_value), None)
        if duplicate is None:
            deduplicated.append((item, label, number, ambiguous))
    return deduplicated


def _pair_pdf_summary_values_below(entries, tokens: tuple[PositionedText, ...]):
    """Recover only unique, column-aligned PDF summary values below labels."""
    used_numbers = {id(number) for item, _label, number, _ambiguous in entries
                    if number is not None and item.value is not None}
    proposals = []
    labels = [entry[1] for entry in entries]
    for entry in entries:
        item, label, _number, _ambiguous = entry
        if compact(item.raw_item_name) not in SUMMARY_LABELS or item.value is not None:
            continue
        matches = []
        for number in tokens:
            values = amounts(number.text)
            vertical_gap = number.y - (label.y + label.height)
            overlap = max(0.0, min(label.x + label.width, number.x + number.width)
                          - max(label.x, number.x))
            if (number.page == label.page and len(values) == 1
                    and id(number) not in used_numbers
                    and 0 <= vertical_gap <= label.height * 1.5
                    and overlap >= min(label.width, number.width) * .5):
                # A value is unsafe when another label in its column is at least as close.
                competing = any(
                    other is not label and other.page == label.page
                    and 0 <= number.y - (other.y + other.height) <= vertical_gap
                    and max(0.0, min(other.x + other.width, number.x + number.width)
                            - max(other.x, number.x)) >= min(other.width, number.width) * .5
                    for other in labels
                )
                if not competing:
                    matches.append((number, values[0]))
        if len(matches) == 1:
            proposals.append((entry, matches[0][0], matches[0][1]))

    # Neither a value token nor one summary label may resolve to conflicting values.
    value_counts = {id(number): sum(candidate is number for _entry, candidate, _value in proposals)
                    for _entry, number, _value in proposals}
    summary_values = {}
    for entry, _number, value in proposals:
        summary_values.setdefault(entry[0].standard_item_candidate, set()).add(value)
    accepted = [(entry, number, value) for entry, number, value in proposals
                if value_counts[id(number)] == 1
                and len(summary_values[entry[0].standard_item_candidate]) == 1]

    by_standard = {}
    for entry, number, value in accepted:
        by_standard.setdefault(entry[0].standard_item_candidate, []).append((entry, number, value))
    totals = {standard: matches[0][2] for standard, matches in by_standard.items()
              if len(matches) == 1}
    if set(totals) == {"gross_pay", "total_deductions", "net_pay"}:
        if totals["gross_pay"] - totals["total_deductions"] != totals["net_pay"]:
            return entries

    for entry, number, value in accepted:
        item = entry[0]
        item.value = value
        item.raw_value = number.text
        item.confidence = min(item.confidence, number.confidence)
        item.needs_review = False
    return entries


def _ocr_label_tokens(tokens: tuple[PositionedText, ...]) -> list[PositionedText]:
    """Join adjacent OCR words on one line without inventing distant text."""
    # A token containing digits or table borders is a value/cell fragment, not a
    # safe component of a reconstructed label (plain OCR digits may lack commas).
    labels = [token for token in tokens
              if not amounts(token.text)
              and not re.search(r"\d", token.text)
              and not any(border in token.text for border in "|｜")]
    ordered = sorted(labels, key=lambda token: (token.page, token.y, token.x))
    groups: list[list[PositionedText]] = []
    for token in ordered:
        if not groups:
            groups.append([token])
            continue
        previous = groups[-1][-1]
        same_line = (
            token.page == previous.page
            and abs(token.y - previous.y) <= max(token.height, previous.height) * .55
        )
        gap = token.x - (previous.x + previous.width)
        close = -max(token.height, previous.height) <= gap <= max(
            24, max(token.height, previous.height) * 2,
        )
        if same_line and close:
            groups[-1].append(token)
        else:
            groups.append([token])

    merged = []
    for group in groups:
        text = "".join(token.text.strip() for token in group)
        first = group[0]
        right = max(token.x + token.width for token in group)
        merged.append(PositionedText(
            text=text, page=first.page, x=first.x, y=min(token.y for token in group),
            width=right - first.x, height=max(token.height for token in group),
            confidence=min(token.confidence for token in group),
        ))
    return merged


def parse_period_and_date(text: str) -> tuple[str | None, str | None]:
    normalized = compact(text)
    dates = re.findall(r"(20\d{2})年(\d{1,2})月(\d{1,2})日", normalized)
    pay_date = None
    if dates:
        y, m, d = dates[-1]
        pay_date = date(int(y), int(m), int(d)).isoformat()
    period_match = re.search(r"(20\d{2})年?(\d{1,2})月(?:度|分)", normalized)
    if not period_match and "給与明細" in normalized:
        period_match = re.search(r"(20\d{2})\s*[\u5e74]?\s*(\d{1,2})(?:\s*[月]?\s*分)?", text)
    period = f"{period_match.group(1)}-{int(period_match.group(2)):02d}" if period_match else None
    return period, pay_date


def parse_items(text: str) -> list[PayrollItem]:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    result: list[PayrollItem] = []
    current = "unknown"
    for index, line in enumerate(lines):
        names = re.findall(r"[^\s\d,]+(?:\([^)]*\))?", line)
        vals = amounts(line)
        if not vals and index + 1 < len(lines):
            vals = amounts(lines[index + 1])
        known_line = any(candidate(name) or section_for(name) == "attendance" for name in names)
        payroll_row = known_line or any(word in line for word in (
            "他基準内", "財形貯蓄", "社宅立替", "組合(", "割増賃金計",
        ))
        if not names or not payroll_row:
            continue
        if any(marker in line for marker in ("殿", "社員番号", "株式会社", "銀行", "支店",
                                             "本部", "センタ", "技術G")):
            names = [name for name in names if candidate(name) or section_for(name) == "attendance"]
        # PDF tables omit empty cells. Preserve every named field; align the total to
        # the final column and otherwise retain values conservatively from the left.
        mapped: list[int | None] = [None] * len(names)
        if not vals:
            mapped = [None] * len(names)
        elif len(vals) == len(names):
            mapped = vals
        elif vals:
            for pos, value in enumerate(vals[:max(0, len(vals) - 1)]): mapped[pos] = value
            mapped[-1] = vals[-1]
        for name, value in zip(names, mapped):
            if len(name) < 2 or name == "年月分" or _is_non_item_heading(name): continue
            current = section_for(name, current)
            result.append(PayrollItem(raw_item_name=name, section=current,
                                      value=value, standard_item_candidate=candidate(name)))
    # Deduplicate extraction artifacts without discarding distinct raw names.
    unique: dict[tuple[str, str, object], PayrollItem] = {}
    for item in result: unique[(item.raw_item_name, item.section, item.value)] = item
    return list(unique.values())


def _logical_pdf_value_pairs(
    tokens: tuple[PositionedText, ...],
) -> dict[tuple[int, str, float, float], PositionedText]:
    """Pair sparse PDF value rows only after repeated legacy-table detection."""
    rows: list[list[PositionedText]] = []
    for token in sorted(tokens, key=lambda item: (item.page, item.y, item.x)):
        if (rows and token.page == rows[-1][0].page
                and abs(token.y - rows[-1][0].y) <= 1.5):
            rows[-1].append(token)
        else:
            rows.append([token])

    page_pairs: dict[int, list[tuple[list[PositionedText], list[PositionedText]]]] = {}
    page_evidence: dict[int, int] = {}
    for index, row in enumerate(rows[:-1]):
        labels = [token for token in row if not amounts(token.text)
                  and (candidate(token.text)
                       or section_for(token.text) == "attendance"
                       or any(term in token.text for term in ITEM_TERMS))]
        if not labels or any(amounts(token.text) for token in row):
            continue
        following = rows[index + 1]
        if following[0].page != row[0].page:
            continue
        gap = following[0].y - row[0].y
        values = [token for token in following if amounts(token.text)]
        if not (LOGICAL_ROW_MIN_Y_GAP <= gap <= LOGICAL_ROW_MAX_Y_GAP and values):
            continue
        near_column = any(
            min(abs(value.x - label.x) for label in labels) <= LOGICAL_ROW_MAX_X_GAP
            for value in values
        )
        if near_column:
            page_pairs.setdefault(row[0].page, []).append((labels, values))
            ordered_x = sorted(label.x for label in labels)
            gaps = [right - left for left, right in zip(ordered_x, ordered_x[1:])]
            regular_columns = sum(35 <= gap <= 70 for gap in gaps)
            if (len(labels) >= 4
                    and regular_columns >= max(2, len(gaps) // 2)):
                page_evidence[row[0].page] = page_evidence.get(row[0].page, 0) + 1

    result: dict[tuple[int, str, float, float], PositionedText] = {}
    for page, pairs in page_pairs.items():
        # One matching row can be accidental. Require a repeated page structure.
        if page_evidence.get(page, 0) < 2:
            continue
        for labels, values in pairs:
            proposed: dict[int, list[PositionedText]] = {}
            for value in values:
                ranked = sorted(
                    ((abs(value.x - label.x), index) for index, label in enumerate(labels)),
                    key=lambda item: item[0],
                )
                distance, label_index = ranked[0]
                margin = ranked[1][0] - distance if len(ranked) > 1 else float("inf")
                if (distance <= LOGICAL_ROW_MAX_X_GAP
                        and margin >= LOGICAL_ROW_MIN_X_MARGIN):
                    proposed.setdefault(label_index, []).append(value)
            for label_index, matched_values in proposed.items():
                # A label and a value may each participate in at most one pairing.
                if len(matched_values) != 1:
                    continue
                label = labels[label_index]
                result[(label.page, label.text.strip(), label.x, label.y)] = matched_values[0]
    return result


def parse_positioned_items(tokens: tuple[PositionedText, ...], *, ocr: bool = False) -> list[PayrollItem]:
    """Pair labels only with geometrically adjacent values; ambiguity becomes review."""
    money = [(token, amounts(token.text)) for token in tokens if amounts(token.text)]
    ocr_dot_money = [
        (token, values) for token in tokens
        if ocr and (values := _ocr_dot_amounts(token))
    ]
    labels = _ocr_label_tokens(tokens) if ocr else [
        token for token in tokens
        if not amounts(token.text) and not _is_explicit_attendance_quantity(token.text)
    ]
    logical_values = {} if ocr else _logical_pdf_value_pairs(tokens)
    entries = []
    sensitive = ("殿", "社員番号", "株式会社", "銀行", "支店", "本部", "センタ")
    ignored = ("お知らせ", "年月分")
    short_ocr_fragments = {"給与", "出勤", "手当", "保険", "控除", "支給"}
    for label in labels:
        name = label.text.strip()
        if (len(name) < 2 or re.fullmatch(r"[\d\W]+", name) or
                any(term in name for term in sensitive + ignored) or
                _is_non_item_heading(name) or
                (ocr and compact(name) in short_ocr_fragments and candidate(name) is None)):
            continue
        attendance_type = _attendance_value_type(name) if section_for(name) == "attendance" else None
        same_page = [(number, vals) for number, vals in money if number.page == label.page]
        if attendance_type is None:
            same_page.extend(
                (number, vals) for number, vals in ocr_dot_money
                if number.page == label.page
            )
        if attendance_type is not None:
            same_page.extend(
                (number, [value]) for number in tokens
                if number.page == label.page
                and (value := _explicit_attendance_value(number.text, attendance_type)) is not None
            )
        horizontal = [(number, vals, number.x - (label.x + label.width))
                      for number, vals in same_page
                      if abs(number.y - label.y) <= max(label.height, number.height) * .65
                      and number.x >= label.x + label.width - 3
                      and (not ocr or _owns_ocr_horizontal_value(
                          label, number, labels, tokens,
                      ))]
        below = [(number, vals, number.y - (label.y + label.height))
                 for number, vals in same_page
                 if 0 <= number.y - (label.y + label.height) <= label.height * 2.2
                 and label.x - label.width * .35 <= number.x <= label.x + label.width * 1.35]
        above = [(number, vals, label.y - (number.y + number.height))
                 for number, vals in same_page
                 if 0 <= label.y - (number.y + number.height) <= label.height * 1.2
                 and label.x - label.width * .35 <= number.x <= label.x + label.width * 1.35]
        logical = logical_values.get((label.page, name, label.x, label.y))
        logical_candidate = ((logical, amounts(logical.text), 0),) if logical else ()
        candidates = (sorted(horizontal, key=lambda item: item[2])
                      or (sorted(below, key=lambda item: item[2]) if ocr else [])
                      or (sorted(above, key=lambda item: item[2]) if ocr else [])
                      or logical_candidate)
        chosen = candidates[0] if candidates else None
        plausible_name = _is_plausible_item_label(name)
        if not plausible_name: continue
        ambiguous = len(candidates) > 1 and abs(candidates[1][2] - candidates[0][2]) < max(label.width, 12)
        low_confidence = ocr and (label.confidence < 60 or
                                  (chosen is not None and chosen[0].confidence < 60))
        confirmed = chosen is not None and not ambiguous and not low_confidence
        number = chosen[0] if chosen else None
        if confirmed and number is not None and _attendance_value_conflicts(name, number.text):
            confirmed = False
        raw_value = number.text if confirmed else None
        value = chosen[1][0] if confirmed and len(chosen[1]) == 1 else None
        item = PayrollItem(
            raw_item_name=name, section=section_for(name), value=value, raw_value=raw_value,
            standard_item_candidate=candidate(name), page=label.page, x=label.x, y=label.y,
            confidence=min(label.confidence, number.confidence) if number else label.confidence,
            needs_review=not confirmed,
        )
        entries.append((item, label, number, ambiguous))
    if not ocr:
        entries = _deduplicate_pdf_labels(entries)
        entries = _pair_pdf_summary_values_below(entries, tokens)
    result = [entry[0] for entry in entries]
    # Stable row/column indexes are derived per page, without template coordinates.
    for page in {item.page for item in result}:
        page_items = [item for item in result if item.page == page]
        ys = sorted({round(item.y or 0, 0) for item in page_items})
        xs = sorted({round(item.x or 0, 0) for item in page_items})
        for item in page_items:
            item.row = min(range(len(ys)), key=lambda i: abs(ys[i] - (item.y or 0)))
            item.column = min(range(len(xs)), key=lambda i: abs(xs[i] - (item.x or 0)))
    _mark_ytd_block(tokens, result)
    if not ocr:
        return result
    unique = {}
    for item in result:
        key = (item.page, item.raw_item_name, round(item.x or 0), round(item.y or 0))
        unique[key] = item
    items = list(unique.values())
    # OCR may emit the same short label twice at nearly identical coordinates.
    deduplicated = []
    for item in items:
        duplicate = next((existing for existing in deduplicated
                          if existing.page == item.page
                          and compact(existing.raw_item_name) == compact(item.raw_item_name)
                          and abs((existing.x or 0) - (item.x or 0)) <= 12
                          and abs((existing.y or 0) - (item.y or 0)) <= 12), None)
        if duplicate is None:
            deduplicated.append(item)
    return deduplicated
