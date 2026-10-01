"""Bank review as the optional fourth block of the category operation surface.

Only A:F are interactive. A second row holds source bank/alias fields so mobile
operators need not reveal hidden technical columns or enter account numbers.
"""
from copy import deepcopy
import re

from .bank_income import INCOME_CATEGORIES
from .bank_review_groups import encoded, safe_alias, validate_snapshot

BANK_MARKER = "■ 4. 銀行取引をまとめて確認"
BANK_UI_HEADERS = ["条件・代表取引", "対象", "用途", "収入分類", "今後の自動判定", "反映",
                   "固定group key", "確認group ID", "行種別", "代表原本リンク",
                   "固定snapshot digest", "固定snapshot"]
PURPOSES = {"未選択": "", "支出": "expense", "収入": "income", "自己口座間振替": "transfer",
            "返金": "reimbursement", "対象外": "other_nonwrite", "個別確認": "needs_review"}
FUTURE_CHOICES = ("未選択", "登録する", "今回のみ")
BANK_LABELS = {"au-jibun": "auじぶん銀行", "docomo-smtb": "ドコモ銀行", "chiba": "千葉銀行"}


def checked(value):
    return value is True or isinstance(value, str) and value.upper() == "TRUE"


def representative_link(group):
    representative = group["members"][0]
    file_id = representative.get("file_id", "")
    page = representative.get("page", "")
    if not re.fullmatch(r"[A-Za-z0-9_-]{10,200}", file_id) or type(page) is not int or page < 1:
        raise ValueError("bank_review_representative_missing")
    return f"https://drive.google.com/file/d/{file_id}/view#page={page}"


def build_rows(groups, prior_rows=()):
    """Retain edits only for exactly unchanged conditions AND membership proof."""
    prior = {str(row[6]): row for row in prior_rows if len(row) == 12 and row[6]}
    output = []
    for group in groups:
        link = representative_link(group)
        first = group["members"][0]
        direction = "入金" if group["direction"] == "incoming" else "出金"
        label = (f"{group['id']}｜{BANK_LABELS.get(group['bank'], group['bank'])}｜{direction}\n"
                 f"{group['description']}\n代表原本を見る → p{first['page']} 行{first['row']}")
        target = (f"{group['count']}件 / {group['total_amount']:,}円\n"
                  f"{group['start_date']}～{group['end_date']}\n用途未確定")
        if group["amount_partition"] is not None:
            target += f"\n金額条件 {group['amount_partition']:,}円"
        primary = [label, target, "未選択", "", "未選択", False, group["key"], group["id"],
                   "group", link, group["membership_digest"], encoded(group["snapshot"])]
        if any(isinstance(value, str) and len(value.encode("utf-16-le")) // 2 > 45000
               for value in primary):
            raise ValueError("bank_review_snapshot_cell_bound_exceeded")
        sender = ["自己口座間振替の場合のみ", "送金元銀行／口座alias →", "", "", "", "",
                  group["key"] + ":sender", group["id"], "sender", "",
                  group["membership_digest"], ""]
        old = prior.get(group["key"])
        if old and old[8] == "group" and old[10:12] == primary[10:12]:
            primary[2:6] = deepcopy(old[2:6])
            if "\n処理結果：" in str(old[1]):
                primary[1] += "\n処理結果：" + str(old[1]).split("\n処理結果：", 1)[1]
            old_sender = prior.get(sender[6])
            if old_sender and old_sender[8] == "sender" and old_sender[10] == sender[10]:
                sender[2:4] = deepcopy(old_sender[2:4])
        elif old:
            primary[1] += "\n対象変更・以前の入力は未反映"
        output.extend((primary, sender))
    return output


def validate_rows(rows):
    """Reject ambiguous shape and hidden-proof tampering before any writer."""
    if len(rows) % 2:
        raise ValueError("bank_review_rows_invalid")
    seen = set()
    for index in range(0, len(rows), 2):
        group, sender = rows[index:index + 2]
        if (len(group) != 12 or len(sender) != 12 or group[8] != "group" or sender[8] != "sender"
                or group[6] in seen or sender[6] != group[6] + ":sender"
                or group[7] != sender[7] or group[10] != sender[10] or sender[11]
                or any(sender[column] for column in (4, 5, 9))):
            raise ValueError("bank_review_rows_invalid")
        seen.add(group[6])
        proof = validate_snapshot(group[11], group[6], group[10])
        expected_link = representative_link({"members": proof["members"]})
        if group[9] != expected_link:
            raise ValueError("bank_review_representative_changed")
    return rows


def submitted_answers(rows, *, accounts):
    """Only checked groups are requests; no implied meaning or future consent.

    accounts is a trusted bank/alias registry, not a name extracted from an
    amount match. The original fixed snapshot stays attached to every answer.
    """
    validate_rows(rows)
    answers = []
    for index in range(0, len(rows), 2):
        group, sender = rows[index:index + 2]
        if not checked(group[5]):
            continue
        proof = validate_snapshot(group[11], group[6], group[10])
        choice = PURPOSES.get(group[2])
        if not choice or group[4] not in FUTURE_CHOICES[1:]:
            raise ValueError("bank_review_choice_missing")
        direction = proof["condition"]["direction"]
        if direction == "incoming" and choice == "expense" or direction == "outgoing" and choice == "income":
            raise ValueError("bank_review_direction_choice_invalid")
        category = group[3]
        if choice == "income" and category not in INCOME_CATEGORIES:
            raise ValueError("bank_review_income_category_missing")
        if choice != "income" and category:
            raise ValueError("bank_review_income_category_not_applicable")
        source_bank, source_alias = sender[2:4]
        source_bank = {label: bank for bank, label in BANK_LABELS.items()}.get(source_bank, source_bank)
        if choice == "transfer":
            if not safe_alias(source_alias) or (source_bank, source_alias) not in accounts:
                raise ValueError("bank_review_source_account_unconfirmed")
            if (source_bank, source_alias) == (proof["condition"]["bank"], proof["condition"]["account_alias"]):
                raise ValueError("bank_review_source_account_same")
        elif source_bank or source_alias:
            raise ValueError("bank_review_source_account_not_applicable")
        answers.append({"group_key": group[6], "group_id": group[7], "snapshot": proof,
            "snapshot_digest": group[10], "choice": choice, "income_category": category,
            "future": group[4] == "登録する", "source_bank": source_bank,
            "source_alias": source_alias})
    return answers


def controls(sheet_id, start_row, rows):
    """Native dropdowns/checkboxes, scoped to the bank block's rows only."""
    requests = []
    def validation(row, col, choices):
        return {"setDataValidation": {"range": {"sheetId": sheet_id,
            "startRowIndex": row - 1, "endRowIndex": row,
            "startColumnIndex": col, "endColumnIndex": col + 1}, "rule": {
                "condition": {"type": "ONE_OF_LIST", "values": [
                    {"userEnteredValue": choice} for choice in choices]},
                "strict": True, "showCustomUi": True}}}
    for number, row in enumerate(rows, start_row):
        if row[8] == "group":
            requests.extend([validation(number, 2, tuple(PURPOSES)),
                validation(number, 3, tuple(sorted(INCOME_CATEGORIES))),
                validation(number, 4, FUTURE_CHOICES),
                {"setDataValidation": {"range": {"sheetId": sheet_id,
                    "startRowIndex": number - 1, "endRowIndex": number,
                    "startColumnIndex": 5, "endColumnIndex": 6}, "rule": {
                        "condition": {"type": "BOOLEAN"}, "strict": True}}},
                {"updateCells": {"start": {"sheetId": sheet_id, "rowIndex": number - 1,
                    "columnIndex": 0}, "rows": [{"values": [{"textFormatRuns": [{
                        "startIndex": 0, "format": {"link": {"uri": row[9]}}}]}]}],
                    "fields": "textFormatRuns"}}])
        elif row[8] == "sender":
            requests.append(validation(number, 2, tuple(BANK_LABELS.values())))
            requests.append({"updateCells": {"start": {"sheetId": sheet_id,
                "rowIndex": number - 1, "columnIndex": 3}, "rows": [{"values": [{
                    "note": "自己口座間振替の場合だけ、確認済みの相手口座aliasを入力。口座番号は入力しないでください。"}]}],
                "fields": "note"}})
    return requests
