"""Review controls: formatting and validation only, never owner cell values."""
from copy import deepcopy
from datetime import date

from .receipt_confirmation import CHOICES, medical_categories
from .receipt_reimport import _date, _money


PAYMENTS = ['現金', 'クレジットカード', 'auPAY', 'PayPay', 'SmartCode', '電子マネー', '口座振替', '銀行振込']
NOTES = {
    3: '原本を開いて、F列の既存値とG列の候補を確認します。',
    5: 'すでに記帳されている内容。正しければM列で「既存値を維持」を選びます。',
    6: '読み取った未確定の候補。採用する場合だけM列で確定を選びます。',
    7: '医療のみ。日付をダブルクリックするとカレンダーが開きます。一般レシートは入力不要です。',
    8: '医療のみ。候補・過去の医療支出から選択できます。新しい施設は直接入力できます。',
    9: '医療のみ。本人が実際に支払った金額を円単位で入力します。',
    10: '医療のみ。既存の医療費カテゴリから選択します。',
    11: '医療のみ・任意。支払方法を選択できます。該当しなければ直接入力、分からなければ空欄で構いません。',
    12: '①F列の既存値とG列の候補を比較 → ②この列のプルダウンで判断。一般：既存が正しい＝既存値を維持／候補を採用＝候補明細で確定／迷う＝保留。医療：候補が正しい＝候補で医療費を確定（不足・修正はH～L列）。手入力した医療費＝医療費を確定。選択は次の取込処理で反映され、完了行は非表示になります。P列に反映結果が出ます。',
    13: '同日付近・同額の支出がある場合だけ選択します。各セルのメモで日付・店舗・金額を確認してください。候補は取込処理のたびに更新されます。',
    14: '任意の補足メモ。種類確認の行はプルダウンでも選べます（これだけで記帳は確定しません）。',
    15: '取込処理後の結果。入力不足などが表示された場合は内容を確認して修正してください。',
}


def duplicate_options(item, parsed, expenses):
    """Same eligibility as the posting validator; options grant no write authority."""
    if parsed is None or item.get('before', {}).get('expense_rows'):
        return []
    source = item['source']['source_id']
    result = []
    for row in expenses:
        r = list(row) + [''] * max(0, 13 - len(row))
        if r[12] != 'active' or r[9] == 'R-' + source or r[10] == 'receipt:' + source:
            continue
        day = _date(r[1])
        if day and abs((date.fromisoformat(day) - date.fromisoformat(parsed.date)).days) <= 7 and _money(r[4]) == parsed.total:
            result.append(r)
    return result


def dropdown_requests(review, sid, rows):
    """Build a non-destructive plan from a fresh review and accounting snapshot."""
    requests = []
    expenses = review.tables()['expense_rows']
    categories = ['｜'.join(c) for c in medical_categories(review.db.categories())]
    facilities = sorted({str(r[2]) for r in expenses if len(r) > 6 and r[5] == '医療・保険' and r[2]})
    payments = list(dict.fromkeys(PAYMENTS + [str(r[7]) for r in expenses if len(r) > 7 and r[7]]))

    def cell_range(n, col):
        return dict(sheetId=sid, startRowIndex=n-1, endRowIndex=n, startColumnIndex=col, endColumnIndex=col+1)

    def validation(n, col, choices, strict=True):
        if not choices:
            return
        requests.append({'setDataValidation': {'range': cell_range(n, col), 'rule': {
            'condition': {'type': 'ONE_OF_LIST', 'values': [{'userEnteredValue': str(x)} for x in dict.fromkeys(choices)]},
            'strict': strict, 'showCustomUi': True}}})

    active_medical = False
    show_duplicates = False
    for key, (n, row) in rows.items():
        item = review.items[key]
        # Archived rows retain their historical controls and notes.
        if not review.needs_attention(key):
            continue
        kind = item['kind']
        active_medical |= kind == 'medical' or any(row[7:12])
        existing = bool(item.get('before', {}).get('expense_rows'))
        choices = ['保留']
        if kind == 'normal':
            if existing:
                choices.append('既存値を維持')
            choices.append('候補明細で確定')
        elif kind == 'medical':
            choices.append('医療費を確定')
            candidate = item.get('medical_candidates', {})
            if candidate.get('source') == item['source'] and candidate.get('candidate_id'):
                choices.append('候補で医療費を確定')
        if kind != 'intake' and not existing:
            choices += ['既存支出と重複（紐付け）', '重複候補と別の支出として確定']
        # Preserve an in-progress/legacy owner decision, even if no longer offered.
        if row[12] in CHOICES and row[12] not in choices:
            choices.append(row[12])
        if not row[12] or row[12] in choices:
            validation(n, 12, choices)
        if kind == 'intake':
            validation(n, 14, ['一般の買物', '医療', '対象外', '再撮影が必要'], strict=False)
        if kind == 'medical':
            candidate = item.get('medical_candidates', {})
            issuer = candidate.get('issuer', '') if candidate.get('source') == item['source'] else ''
            validation(n, 8, [x for x in [row[8], issuer] + facilities if x], strict=False)
            validation(n, 10, categories)
            validation(n, 11, payments, strict=False)
            for col, condition in [(7, {'type': 'DATE_IS_VALID'}), (9, {'type': 'NUMBER_GREATER', 'values': [{'userEnteredValue': '0'}]})]:
                requests.append({'setDataValidation': {'range': cell_range(n, col), 'rule': {
                    'condition': condition, 'strict': True, 'showCustomUi': True}}})
            requests.append({'repeatCell': {'range': cell_range(n, 7), 'cell': {
                'userEnteredFormat': {'numberFormat': {'type': 'DATE', 'pattern': 'yyyy/mm/dd'}}},
                'fields': 'userEnteredFormat.numberFormat'}})
        parsed = None
        if kind != 'intake':
            live = deepcopy(item)
            live['inputs'] = list(row[7:15])
            if kind == 'medical' and '候補で医療費を確定' in choices:
                live['inputs'][5] = '候補で医療費を確定'
            try:
                parsed = review._parsed(live)
            except (ValueError, KeyError):
                pass  # Incomplete medical input cannot propose a matching expense yet.
        duplicates = duplicate_options(item, parsed, expenses)
        # N remains available for medical/manual input even before it is complete.
        show_duplicates |= bool(row[13]) or (kind != 'intake' and not existing)
        if duplicates:
            validation(n, 13, [r[0] for r in duplicates], strict=False)
            note = '\n'.join(f'{r[0]}：{_date(r[1])}／{r[2]}／{r[4]}円' for r in duplicates)
        else:
            requests.append({'setDataValidation': {'range': cell_range(n, 13)}})
            note = '現在の入力・候補に一致する統合先はありません。日付・金額を修正した場合、次の取込処理で候補を再確認します。'
        requests.append({'repeatCell': {'range': cell_range(n, 13), 'cell': {'note': note}, 'fields': 'note'}})
        for a, b, color in [(7, 12, {'red': 1, 'green': .98, 'blue': .88} if kind == 'medical' else {'red': .95, 'green': .95, 'blue': .95}),
                            (12, 13, {'red': 1, 'green': .95, 'blue': .72})]:
            requests.append({'repeatCell': {'range': dict(sheetId=sid, startRowIndex=n-1, endRowIndex=n, startColumnIndex=a, endColumnIndex=b),
                'cell': {'userEnteredFormat': {'backgroundColor': color}}, 'fields': 'userEnteredFormat.backgroundColor'}})
    for col, note in NOTES.items():
        requests.append({'repeatCell': {'range': cell_range(1, col), 'cell': {'note': note}, 'fields': 'note'}})
    for a, b, width in [(1, 2, 65), (2, 3, 105), (3, 4, 85), (4, 5, 210), (5, 7, 235), (7, 12, 155), (12, 13, 205), (13, 16, 230)]:
        requests.append({'updateDimensionProperties': {'range': dict(sheetId=sid, dimension='COLUMNS', startIndex=a, endIndex=b),
            'properties': {'pixelSize': width}, 'fields': 'pixelSize'}})
    for a, b, hide in [(7, 12, not active_medical), (13, 14, not show_duplicates)]:
        requests.append({'updateDimensionProperties': {'range': dict(sheetId=sid, dimension='COLUMNS', startIndex=a, endIndex=b),
            'properties': {'hiddenByUser': hide}, 'fields': 'hiddenByUser'}})
    return requests
