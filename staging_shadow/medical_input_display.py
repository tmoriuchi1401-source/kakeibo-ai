"""Presentation of an existing medical validation failure; no admission rules.

Only field names leave this function. It cannot save, validate, grant or post.
The caller has already saved and re-read the exact partial draft.
"""

PREFIX = '医療・完全手入力\n'
STOPPED = '自動処理は停止中。入力修正だけでは登録検証・記帳されません。'
PARTIAL = '必須項目または明細別入力が不足しています。部分入力は保存済みです。'
RECEIVED = 'この原本の登録検証は受付済みです。保存結果を照会してください。'


def partial_reason(error, fields):
    # Names mirror validate_proposal's existing required fields. This is a
    # description of blanks in its rejected snapshot, never a readiness test.
    if str(error) not in {'draft_v2_text', 'draft_v2_integer', 'draft_v2_complete_manual_only'}:
        return '入力不足または入力不正。部分入力は保存済みです。'
    names = [('date', '日付'), ('merchant', '施設名'),
             ('amount', '実支払額'), ('category', 'カテゴリ')]
    missing = [label for key, label in names
               if fields[key] == '' or type(fields[key]) is str and not fields[key].strip()]
    if missing:
        return '入力不足・未入力：'+'、'.join(missing)+'\n部分入力は保存済みです。'
    return '入力不正または入力条件未成立。部分入力は保存済みです。'


def terminal_reason(status, reason):
    if status == 'CONFLICT':
        reason = '入力を採用していません。再登録せず、案件・原本・保存結果を照合してください。'
    elif status == 'HELD' and reason == RECEIVED:
        reason = '受付済み・未記帳。再登録せず保存結果を照会してください。'
    return PREFIX+reason+'\n'+STOPPED
