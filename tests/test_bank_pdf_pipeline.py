import ast
import sys

import pytest

from app.bank_pdf_pipeline import (
    BankPdfError,
    BankPdfPipeline,
    PageGeometry,
    PositionedWord,
    parse_jibun_bank_pages,
)
from app.cli import main
from app.transaction_plan import resolve_transaction_identities


BOUNDARIES = (80.0, 300.0, 390.0, 480.0)


def word(text, x0, top, width=None):
    width = width if width is not None else max(8.0, len(text) * 7.0)
    return PositionedWord(text, x0, x0 + width, top, top + 9.0)


def header(top=100.0, x_offset=0.0):
    return (
        word("取引日付", 20 + x_offset, top, 45),
        word("取", 160 + x_offset, top), word("引", 170 + x_offset, top),
        word("内", 180 + x_offset, top), word("容", 190 + x_offset, top),
        word("出", 330 + x_offset, top), word("金", 345 + x_offset, top),
        word("入", 420 + x_offset, top), word("金", 435 + x_offset, top),
        word("残", 510 + x_offset, top), word("高", 525 + x_offset, top),
    )


def transaction_row(
    date, description, *, debit=None, credit=None, balance="1,000", top=120.0,
    x_offset=0.0,
):
    words = [
        word(date, 15 + x_offset, top, 55),
        word(description, 90 + x_offset, top, 120),
    ]
    if debit is not None:
        words.append(word(str(debit), 330 + x_offset, top, 50))
    if credit is not None:
        words.append(word(str(credit), 420 + x_offset, top, 50))
    if balance is not None:
        words.append(word(str(balance), 510 + x_offset, top, 60))
    return tuple(words)


def page(number, *rows, include_header=True, header_top=100.0, x_offset=0.0):
    words = header(header_top, x_offset) if include_header else ()
    return PageGeometry(number, 595, 842, words + tuple(
        item for row in rows for item in row
    ), tuple(boundary + x_offset for boundary in BOUNDARIES))


def test_withdrawal_deposit_comma_and_empty_opposite_cells():
    result = parse_jibun_bank_pages([page(
        1,
        transaction_row("2026/09/02", "匿名支払", debit="1,200", balance="3,800", top=120),
        transaction_row("2026/09/01", "匿名入金", credit="2,000", balance="5,000", top=140),
    )])

    assert [item.signed_amount for item in result.transactions] == [-1200, 2000]
    assert [item.transaction_kind for item in result.transactions] == ["withdrawal", "deposit"]
    assert result.balance_consistency_failures == 0
    assert result.summary()["withdrawal_count"] == 1
    assert result.summary()["deposit_count"] == 1


def test_same_day_same_amount_remain_distinct_by_stable_row_identity():
    geometry = page(
        1,
        transaction_row("2026/09/02", "匿名A", debit="500", balance="1,000", top=120),
        transaction_row("2026/09/02", "匿名B", debit="500", balance="1,500", top=140),
    )
    first = parse_jibun_bank_pages([geometry])
    second = parse_jibun_bank_pages([geometry])

    assert len({item.source_row_identity for item in first.transactions}) == 2
    assert [item.source_row_identity for item in first.transactions] == [
        item.source_row_identity for item in second.transactions
    ]


def identity_for(result, description):
    return next(
        item.source_row_identity for item in result.transactions
        if item.description == description
    )


def test_identity_survives_page_row_and_header_position_changes():
    original = parse_jibun_bank_pages([
        page(1, transaction_row(
            "2026/09/02", "対象取引", debit="500", balance="1,000", top=120,
        )),
    ])
    regenerated = parse_jibun_bank_pages([
        page(1, transaction_row(
            "2026/09/03", "追加取引", credit="500", balance="500", top=155,
        ), header_top=130),
        page(2, transaction_row(
            "2026/09/02", "対象取引", debit="500", balance="1,000", top=210,
            x_offset=10,
        ), header_top=170, x_offset=10),
        page(3, transaction_row(
            "2026/09/01", "過去取引", credit="500", balance="1,500", top=260,
        ), header_top=220),
    ])

    assert identity_for(original, "対象取引") == identity_for(regenerated, "対象取引")


def test_balance_discriminates_same_date_description_and_amount():
    result = parse_jibun_bank_pages([page(
        1,
        transaction_row("2026/09/02", "同一摘要", debit="500", balance="1,000", top=120),
        transaction_row("2026/09/02", "同一摘要", debit="500", balance="1,500", top=140),
    )])
    identities = [item.source_row_identity for item in result.transactions]

    assert len(set(identities)) == 2
    assert all(len(identity.split(":")) == 4 for identity in identities)


def test_occurrence_suffix_is_only_fallback_for_identical_balance_material():
    result = parse_jibun_bank_pages([page(
        1,
        transaction_row("2026/09/02", "同一摘要", debit="500", balance="1,000", top=120),
        transaction_row("2026/09/02", "同一摘要", debit="500", balance="1,000", top=140),
    )])
    identities = [item.source_row_identity for item in result.transactions]

    assert identities[0].endswith(":001")
    assert identities[1].endswith(":002")
    assert len(set(identities)) == 2


def test_balance_is_not_exposed_on_normalized_or_canonical_transaction():
    result = parse_jibun_bank_pages([
        page(1, transaction_row("2026/09/01", "匿名", debit="100")),
    ])

    assert not hasattr(result.transactions[0], "balance")
    assert not hasattr(result.canonical_transactions[0], "balance")


def test_multi_page_repeated_headers_are_parsed_and_balances_cross_page():
    result = parse_jibun_bank_pages([
        page(1, transaction_row("2026/09/03", "匿名A", debit="100", balance="900")),
        page(2, transaction_row("2026/09/02", "匿名B", debit="100", balance="1,000")),
    ])

    assert result.pages == 2
    assert result.candidate_rows == 2
    assert len(result.transactions) == 2
    assert result.balance_consistency_failures == 0


@pytest.mark.parametrize(
    ("row", "reason"),
    [
        (transaction_row("2026/09/01", "匿名", debit="100", credit="100"), "both_debit_and_credit"),
        (transaction_row("2026/09/01", "匿名"), "amount_missing"),
        (transaction_row("2026/09/01", "匿名", debit="bad"), "amount_invalid"),
        (transaction_row("2026/09/01", "", debit="100"), "description_missing"),
        (transaction_row("2026/09/01", "匿名", debit="100", balance=None), "balance_missing"),
    ],
)
def test_malformed_rows_fail_closed(row, reason):
    result = parse_jibun_bank_pages([page(1, row)])

    assert result.candidate_rows == 1
    assert not result.transactions
    assert [issue.reason for issue in result.issues] == [reason]


def test_balance_check_does_not_bridge_over_an_unresolved_row():
    result = parse_jibun_bank_pages([page(
        1,
        transaction_row("2026/09/03", "匿名A", debit="100", balance="900", top=120),
        transaction_row("2026/09/02", "匿名B", top=140),
        transaction_row("2026/09/01", "匿名C", debit="500", balance="1,500", top=160),
    )])

    assert result.balance_consistency_failures == 0
    assert result.summary()["rejected_unresolved_count"] == 1


def test_header_or_column_geometry_failure_is_unresolved():
    missing_header = page(
        1, transaction_row("2026/09/01", "匿名", debit="100"),
        include_header=False,
    )
    result = parse_jibun_bank_pages([missing_header])

    assert result.summary()["unresolved_reasons"] == {"header_geometry_unresolved": 1}


def test_replay_is_duplicate_in_existing_canonical_identity_resolver():
    parsed = parse_jibun_bank_pages([
        page(1, transaction_row("2026/09/01", "匿名", debit="100")),
    ])
    canonical = list(parsed.canonical_transactions)
    replay = resolve_transaction_identities(canonical + canonical)

    assert len(replay.unique) == 1
    assert replay.duplicate_count == 1
    assert not replay.collision_groups


def test_existing_identity_is_withheld_from_canonical_candidates():
    initial = parse_jibun_bank_pages([
        page(1, transaction_row("2026/09/01", "匿名", debit="100")),
    ])
    identity = initial.transactions[0].source_row_identity
    replay = parse_jibun_bank_pages([
        page(1, transaction_row("2026/09/01", "匿名", debit="100")),
    ], existing_identities={identity})

    assert replay.duplicate_candidates == 1
    assert not replay.canonical_transactions


def test_account_alias_must_be_non_sensitive_slug():
    with pytest.raises(BankPdfError, match="account_alias_invalid"):
        parse_jibun_bank_pages([], account_alias="123 456 789")


def test_cli_preview_is_local_and_read_only(monkeypatch, capsys):
    expected = {
        "source": "auじぶん銀行PDF",
        "extraction_method": "native_pdf_words",
        "pages": 1,
    }
    monkeypatch.setattr(BankPdfPipeline, "preview", lambda *args, **kwargs: expected)
    monkeypatch.setattr(sys, "argv", ["app.cli", "bank-pdf-preview", "statement.pdf"])

    main()

    assert ast.literal_eval(capsys.readouterr().out.strip()) == expected
