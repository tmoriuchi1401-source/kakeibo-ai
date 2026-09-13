import pytest

from app.bank_pdf_pipeline import (
    CHIBA_BANK_SOURCE,
    DEFAULT_CHIBA_BANK_ACCOUNT_ALIAS,
    BankPdfError,
    BankPdfPipeline,
    PageGeometry,
    PositionedWord,
    detect_bank_pdf_adapter,
    parse_chiba_bank_pages,
)
from app.bank_reconciliation import (
    classify_bank_transaction,
    diagnose_cross_bank_transfer_groups,
    group_bank_description_diagnostics,
)
from app.reconciliation import ImportTransaction


def word(text, x0, top, width=None):
    width = width if width is not None else max(8.0, len(text) * 7.0)
    return PositionedWord(text, x0, x0 + width, top, top + 9.0)


def chiba_header(top=232.5, x_offset=0.0):
    return (
        word("年月日", 48.1 + x_offset, top, 27),
        word("お支払い金額", 131.3 + x_offset, top, 54),
        word("お預り金額", 246.9 + x_offset, top, 45),
        word("お取引き内容", 367.3 + x_offset, top, 54),
        word("差引残高", 501.2 + x_offset, top, 36),
    )


def transaction_row(
    date,
    description,
    *,
    withdrawal=None,
    deposit=None,
    balance="¥1,000",
    top=256.5,
    x_offset=0.0,
):
    words = [word(date, 44.0 + x_offset, top, 36)]
    if withdrawal is not None:
        words.append(word(str(withdrawal), 180.0 + x_offset, top, 30))
    if deposit is not None:
        words.append(word(str(deposit), 291.0 + x_offset, top, 30))
    words.extend(
        word(part, 331.4 + x_offset + index * 30, top, 28)
        for index, part in enumerate(description.split())
    )
    if balance is not None:
        words.append(word(str(balance), 540.0 + x_offset, top, 31))
    return tuple(words)


def chiba_page(
    number,
    *rows,
    marker=True,
    title=True,
    header_top=232.5,
    x_offset=0.0,
    statement_range="2025/09/01-2026/09/13",
):
    markers = ()
    if title:
        markers += (word("取引明細照会", 237.6, 56.3, 120),)
    if marker:
        markers += (word("千葉銀行", 527.3, 91.7, 48),)
    metadata = (word(statement_range, 200, 190, 140),)
    return PageGeometry(
        number,
        595,
        842,
        markers + metadata + chiba_header(header_top, x_offset) + tuple(
            item for row in rows for item in row
        ),
        (),
    )


def identity_for(result, description):
    return next(
        item.source_row_identity for item in result.transactions
        if item.description == description
    )


def test_blank_opposite_cells_and_yen_comma_amounts_have_correct_signs():
    result = parse_chiba_bank_pages([chiba_page(
        1,
        transaction_row(
            "2026/09/01", "匿名 支払", withdrawal="¥1,200",
            balance="¥3,800", top=256.5,
        ),
        transaction_row(
            "2026/09/02", "匿名 入金", deposit="￥2,000",
            balance="¥5,800", top=280.5,
        ),
    )])

    assert [item.signed_amount for item in result.transactions] == [-1200, 2000]
    assert [item.transaction_kind for item in result.transactions] == [
        "withdrawal", "deposit",
    ]
    assert result.balance_consistency_failures == 0


@pytest.mark.parametrize(
    ("withdrawal", "deposit", "reason"),
    [
        ("¥100", "¥200", "both_debit_and_credit"),
        (None, None, "amount_missing"),
        ("not-money", None, "amount_invalid"),
    ],
)
def test_ambiguous_or_invalid_amount_cells_fail_closed(
    withdrawal, deposit, reason,
):
    result = parse_chiba_bank_pages([chiba_page(
        1,
        transaction_row(
            "2026/09/01", "匿名", withdrawal=withdrawal, deposit=deposit,
        ),
    )])

    assert result.candidate_rows == 1
    assert not result.transactions
    assert [issue.reason for issue in result.issues] == [reason]


def test_repeated_headers_multi_page_and_ascending_balance_chain():
    result = parse_chiba_bank_pages([
        chiba_page(1, transaction_row(
            "2026/09/01", "匿名A", deposit="¥1,000", balance="¥1,000",
        )),
        chiba_page(2, transaction_row(
            "2026/09/02", "匿名B", withdrawal="¥100", balance="¥900",
        )),
    ])

    assert result.pages == 2
    assert result.candidate_rows == 2
    assert len(result.transactions) == 2
    assert result.balance_consistency_failures == 0


def test_coordinate_reconstruction_does_not_use_word_token_order():
    row = tuple(reversed(transaction_row(
        "2026/09/01", "匿名 支払", withdrawal="¥500", balance="¥1,000",
    )))
    result = parse_chiba_bank_pages([chiba_page(1, row)])

    assert result.transactions[0].description == "匿名 支払"
    assert result.transactions[0].signed_amount == -500


def test_page_relocation_and_statement_range_change_preserve_identity():
    original = parse_chiba_bank_pages([chiba_page(
        1,
        transaction_row(
            "2026/09/02", "対象取引", withdrawal="¥500",
            balance="¥1,000",
        ),
        statement_range="2026/09/01-2026/09/30",
    )])
    regenerated = parse_chiba_bank_pages([
        chiba_page(1, transaction_row(
            "2026/09/01", "追加取引", deposit="¥500", balance="¥1,500",
        )),
        chiba_page(
            2,
            transaction_row(
                "2026/09/02", "対象取引", withdrawal="¥500",
                balance="¥1,000", top=350, x_offset=8,
            ),
            header_top=300,
            x_offset=8,
            statement_range="2026/08/01-2026/10/31",
        ),
    ])

    assert identity_for(original, "対象取引") == identity_for(
        regenerated, "対象取引",
    )
    assert identity_for(original, "対象取引").startswith(
        "bankpdf:chiba:chiba-primary:",
    )


def test_balance_mismatch_is_diagnostic_and_does_not_flip_direction():
    result = parse_chiba_bank_pages([chiba_page(
        1,
        transaction_row(
            "2026/09/01", "匿名A", deposit="¥1,000", balance="¥1,000",
        ),
        transaction_row(
            "2026/09/02", "匿名B", withdrawal="¥100", balance="¥700",
            top=280.5,
        ),
    )])

    assert result.balance_consistency_failures == 1
    assert result.transactions[1].signed_amount == -100
    assert not hasattr(result.transactions[1], "balance")


def test_recurring_description_groups_are_exact_and_directional():
    parsed = parse_chiba_bank_pages([chiba_page(
        1,
        transaction_row(
            "2026/09/01", "反復取引", withdrawal="¥100", balance="¥1,000",
        ),
        transaction_row(
            "2026/09/02", "反復取引", withdrawal="¥100", balance="¥900",
            top=280.5,
        ),
        transaction_row(
            "2026/09/03", "反復取引", deposit="¥100", balance="¥1,000",
            top=304.5,
        ),
    )])
    groups = group_bank_description_diagnostics(parsed)

    assert [(group.direction, group.occurrence_count) for group in groups] == [
        ("outgoing", 2), ("incoming", 1),
    ]


def test_cross_bank_same_amount_is_diagnostic_only():
    parsed = parse_chiba_bank_pages([chiba_page(
        1,
        transaction_row(
            "2026/09/02", "振込候補", deposit="¥100", balance="¥1,000",
        ),
    )])
    existing = [ImportTransaction(
        2, "other-id", "auじぶん銀行PDF", "2026-09-01", "匿名",
        -100, "bank_expense", "", "", [],
    )]

    groups = diagnose_cross_bank_transfer_groups(parsed, existing)

    assert len(groups) == 1
    assert classify_bank_transaction(
        parsed.transactions[0],
    ).classification == "needs_review"


def test_document_detector_requires_issuer_title_and_exact_headers(monkeypatch):
    geometry = chiba_page(1, transaction_row(
        "2026/09/01", "匿名", withdrawal="¥100",
    ))
    monkeypatch.setattr(
        "app.bank_pdf_pipeline.materialize_native_pdf", lambda path: [geometry],
    )

    assert detect_bank_pdf_adapter([geometry]).key == "chiba"
    result = BankPdfPipeline().parse("statement.pdf")
    assert result.source == CHIBA_BANK_SOURCE
    assert result.transactions[0].account_alias == DEFAULT_CHIBA_BANK_ACCOUNT_ALIAS

    for partial in (
        chiba_page(1, marker=False),
        chiba_page(1, title=False),
        PageGeometry(1, 595, 842, (word("千葉銀行", 10, 10),), ()),
    ):
        with pytest.raises(BankPdfError, match="bank_document_unrecognized"):
            detect_bank_pdf_adapter([partial])


def test_existing_jibun_and_docomo_detectors_do_not_regress():
    jibun = PageGeometry(
        1, 595, 842,
        (
            word("取引日付", 20, 100, 45),
            word("取引内容", 160, 100, 45),
            word("出金", 330, 100, 30),
            word("入金", 420, 100, 30),
            word("残高", 510, 100, 30),
        ),
        (80, 300, 390, 480),
    )
    docomo = PageGeometry(
        1, 595, 842,
        (
            word("株式会社ドコモSMTBネット銀行", 400, 60, 150),
            word("日付", 43, 255, 20),
            word("内容", 180, 255, 20),
            word("出金金額", 316, 255, 40),
            word("入金金額", 408, 255, 40),
            word("残高", 513, 255, 20),
        ),
        (89.3, 290.3, 384.1, 478.3),
    )

    assert detect_bank_pdf_adapter([jibun]).key == "au-jibun"
    assert detect_bank_pdf_adapter([docomo]).key == "docomo-smtb"
