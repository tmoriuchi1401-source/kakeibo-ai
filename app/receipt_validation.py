"""Accounting checks shared by extraction, correction and posting."""
from datetime import date


def validate_receipt_result(result, categories):
    notes=[]
    if any((x.major_category,x.minor_category) not in set(categories) for x in result.items):
        notes.append('カテゴリ不正')
    item_sum=sum(x.amount for x in result.items)
    # Yen are integers. A tolerance used to silently admit missing small
    # discounts/taxes. Never manufacture a balancing item to pass this check.
    if item_sum!=result.total:notes.append(f'明細合計{item_sum}≠レシート合計{result.total}')
    if not result.date:notes.append('日付不明')
    else:
        try:
            if date.fromisoformat(result.date).isoformat()!=result.date:raise ValueError
        except (ValueError,TypeError):notes.append('日付不正')
    if not result.merchant.strip():notes.append('店舗名不明')
    if not result.items:notes.append('明細なし')
    if result.total<=0:notes.append('合計金額不正')
    if any(not x.name.strip() or x.quantity<=0 for x in result.items):notes.append('明細不正')
    return not notes,notes
