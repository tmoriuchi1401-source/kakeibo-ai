from __future__ import annotations

import re
from datetime import date

from .payroll_models import PayrollItem
from .payroll_ocr import PositionedText


STANDARD_NAMES = {
    "基本給": "basic_pay", "早出残業": "overtime_pay", "時間外労働": "overtime_pay",
    "時間外手当": "overtime_pay", "通勤手当": "commuting_allowance",
    "健康保険": "health_insurance", "健康保険料": "health_insurance",
    "介護保険": "nursing_care_insurance", "厚生年金": "employees_pension",
    "厚生年金保険": "employees_pension", "雇用保険": "employment_insurance",
    "雇用保険料": "employment_insurance", "所得税": "income_tax",
    "源泉所得額": "income_tax", "住民税": "resident_tax",
    "支給合計": "gross_pay", "総支給額": "gross_pay", "支給額計": "gross_pay",
    "控除合計": "total_deductions", "控除額計": "total_deductions",
    "差引支給額": "net_pay", "差引不足額": "net_pay",
}


def compact(value: str) -> str:
    return re.sub(r"[\s　]+", "", value).replace("，", ",")


def amounts(value: str) -> list[int]:
    return [int(x.replace(",", "")) for x in re.findall(r"(?<![\d.])\d{1,3}(?:,\d{3})+(?!\d)", value)]


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
    if c in {"gross_pay", "total_deductions", "net_pay"}: return "summary"
    if c in {"health_insurance", "nursing_care_insurance", "employees_pension",
             "employment_insurance", "income_tax", "resident_tax"}: return "deductions"
    if c in {"basic_pay", "overtime_pay", "commuting_allowance"}: return "earnings"
    if any(k in name for k in ("日数", "時間", "残業ｈ", "勤務ｈ")): return "attendance"
    return current


def _is_non_item_heading(name: str) -> bool:
    normalized = compact(name).strip("()（）:：<>＜＞")
    return (
        normalized in {"給与明細書", "給与支給明細書", "課税処理", "年次有給休暇"}
        or normalized.startswith("支給日")
    )


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


def parse_positioned_items(tokens: tuple[PositionedText, ...], *, ocr: bool = False) -> list[PayrollItem]:
    """Pair labels only with geometrically adjacent values; ambiguity becomes review."""
    money = [(token, amounts(token.text)) for token in tokens if amounts(token.text)]
    labels = _ocr_label_tokens(tokens) if ocr else [
        token for token in tokens if not amounts(token.text)
    ]
    result = []
    sensitive = ("殿", "社員番号", "株式会社", "銀行", "支店", "本部", "センタ")
    ignored = ("お知らせ", "年月分")
    short_ocr_fragments = {"給与", "出勤", "手当", "保険", "控除", "支給"}
    item_terms = ("給", "手当", "保険", "年金", "税", "控除", "日数", "時間", "残業", "勤務",
                  "休", "積立", "貯蓄", "費", "合計", "対象", "月額", "累計", "調整", "販売",
                  "教育", "組合", "送金", "課税", "基準", "持株", "財形", "社宅", "預金",
                  "出勤", "欠勤", "支給", "報酬")
    for label in labels:
        name = label.text.strip()
        if (len(name) < 2 or re.fullmatch(r"[\d\W]+", name) or
                any(term in name for term in sensitive + ignored) or
                _is_non_item_heading(name) or
                (ocr and compact(name) in short_ocr_fragments and candidate(name) is None)):
            continue
        same_page = [(number, vals) for number, vals in money if number.page == label.page]
        horizontal = [(number, vals, number.x - (label.x + label.width))
                      for number, vals in same_page
                      if abs(number.y - label.y) <= max(label.height, number.height) * .65
                      and number.x >= label.x + label.width - 3]
        below = [(number, vals, number.y - (label.y + label.height))
                 for number, vals in same_page
                 if 0 <= number.y - (label.y + label.height) <= label.height * 2.2
                 and label.x - label.width * .35 <= number.x <= label.x + label.width * 1.35]
        above = [(number, vals, label.y - (number.y + number.height))
                 for number, vals in same_page
                 if 0 <= label.y - (number.y + number.height) <= label.height * 1.2
                 and label.x - label.width * .35 <= number.x <= label.x + label.width * 1.35]
        candidates = (sorted(horizontal, key=lambda item: item[2])
                      or sorted(below, key=lambda item: item[2])
                      or (sorted(above, key=lambda item: item[2]) if ocr else []))
        chosen = candidates[0] if candidates else None
        plausible_name = candidate(name) or section_for(name) == "attendance" or any(term in name for term in item_terms)
        if not plausible_name: continue
        ambiguous = len(candidates) > 1 and abs(candidates[1][2] - candidates[0][2]) < max(label.width, 12)
        low_confidence = ocr and (label.confidence < 60 or
                                  (chosen is not None and chosen[0].confidence < 60))
        confirmed = chosen is not None and not ambiguous and not low_confidence
        number = chosen[0] if chosen else None
        raw_value = number.text if confirmed else None
        value = chosen[1][0] if confirmed and len(chosen[1]) == 1 else None
        result.append(PayrollItem(
            raw_item_name=name, section=section_for(name), value=value, raw_value=raw_value,
            standard_item_candidate=candidate(name), page=label.page, x=label.x, y=label.y,
            confidence=min(label.confidence, number.confidence) if number else label.confidence,
            needs_review=not confirmed,
        ))
    # Stable row/column indexes are derived per page, without template coordinates.
    for page in {item.page for item in result}:
        page_items = [item for item in result if item.page == page]
        ys = sorted({round(item.y or 0, 0) for item in page_items})
        xs = sorted({round(item.x or 0, 0) for item in page_items})
        for item in page_items:
            item.row = min(range(len(ys)), key=lambda i: abs(ys[i] - (item.y or 0)))
            item.column = min(range(len(xs)), key=lambda i: abs(xs[i] - (item.x or 0)))
    unique = {}
    for item in result:
        key = (item.page, item.raw_item_name, round(item.x or 0), round(item.y or 0))
        unique[key] = item
    items = list(unique.values())
    if not ocr:
        return items
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
