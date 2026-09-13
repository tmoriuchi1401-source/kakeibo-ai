import pytest

from app.bank_pdf_pipeline import (
    DEFAULT_DOCOMO_SMTB_ACCOUNT_ALIAS,
    DOCOMO_SMTB_SOURCE,
    BankPdfError,
    BankPdfPipeline,
    PageGeometry,
    PositionedWord,
    detect_bank_pdf_adapter,
    parse_docomo_smtb_bank_pages,
)
from app.bank_reconciliation import (
    build_bank_shadow_result,
    classify_bank_transaction,
)


BOUNDARIES = (89.3, 290.3, 384.1, 478.3)


def word(text, x0, top, width=None):
    width = width if width is not None else max(8.0, len(text) * 6.0)
    return PositionedWord(text, x0, x0 + width, top, top + 9.0)


def docomo_header(top=255.4, x_offset=0.0):
    return (
        word("日付", 43.2 + x_offset, top, 20),
        word("内容", 180.0 + x_offset, top, 20),
        word("出金金額", 316.8 + x_offset, top, 40),
        word("入金金額", 408.6 + x_offset, top, 40),
        word("残高", 513.4 + x_offset, top, 20),
    )


def transaction_row(
    date,
    description,
    *,
    withdrawal,
    deposit,
    balance="1,000",
    top=267.0,
    x_offset=0.0,
):
    return (
        word(date, 20 + x_offset, top - 0.1, 68),
        word(description, 100 + x_offset, top, 150),
        word(str(withdrawal), 320 + x_offset, top + 0.9, 48),
        word(str(deposit), 412 + x_offset, top + 0.9, 48),
        word(str(balance), 500 + x_offset, top + 0.9, 60),
    )


def page(
    number,
    *rows,
    header_top=255.4,
    x_offset=0.0,
    marker=True,
):
    marker_words = (
        word("株式会社ドコモSMTBネット銀行", 403.2, 66.6, 154),
    ) if marker else ()
    return PageGeometry(
        number,
        595,
        842,
        marker_words + docomo_header(header_top, x_offset) + tuple(
            item for row in rows for item in row
        ),
        tuple(boundary + x_offset for boundary in BOUNDARIES),
    )


def identity_for(result, description):
    return next(
        item.source_row_identity for item in result.transactions
        if item.description == description
    )


def test_zero_filled_withdrawal_and_deposit_have_correct_signs():
    result = parse_docomo_smtb_bank_pages([page(
        1,
        transaction_row(
            "2026年09月02日", "匿名出金", withdrawal="200", deposit="0",
            balance="800", top=267,
        ),
        transaction_row(
            "2026年09月01日", "匿名入金", withdrawal="0", deposit="100",
            balance="1,000", top=279,
        ),
    )])

    assert [item.signed_amount for item in result.transactions] == [-200, 100]
    assert [item.transaction_kind for item in result.transactions] == [
        "withdrawal", "deposit",
    ]
    assert result.balance_consistency_failures == 0


@pytest.mark.parametrize(
    ("withdrawal", "deposit", "reason"),
    [
        ("100", "200", "both_debit_and_credit_positive"),
        ("0", "0", "both_amounts_zero"),
        ("100", None, "amount_missing"),
        ("bad", "0", "amount_invalid"),
    ],
)
def test_invalid_amount_pairs_fail_closed(withdrawal, deposit, reason):
    row = list(transaction_row(
        "2026年09月01日", "匿名", withdrawal=withdrawal or "0",
        deposit=deposit or "0",
    ))
    if withdrawal is None:
        row.pop(2)
    if deposit is None:
        row.pop(3 if withdrawal is not None else 2)
    result = parse_docomo_smtb_bank_pages([page(1, tuple(row))])

    assert result.candidate_rows == 1
    assert not result.transactions
    assert [issue.reason for issue in result.issues] == [reason]


def test_multi_page_repeated_header_and_balance_chain():
    result = parse_docomo_smtb_bank_pages([
        page(1, transaction_row(
            "2026年09月02日", "匿名A", withdrawal="100", deposit="0",
            balance="900",
        )),
        page(2, transaction_row(
            "2026年09月01日", "匿名B", withdrawal="0", deposit="100",
            balance="1,000",
        ), marker=False),
    ])

    assert result.pages == 2
    assert result.candidate_rows == 2
    assert len(result.transactions) == 2
    assert result.balance_consistency_failures == 0


def test_row_geometry_and_page_relocation_do_not_change_identity():
    original = parse_docomo_smtb_bank_pages([page(
        1,
        transaction_row(
            "2026年09月02日", "対象取引", withdrawal="500", deposit="0",
            balance="1,000", top=267,
        ),
    )])
    relocated = parse_docomo_smtb_bank_pages([
        page(1, transaction_row(
            "2026年09月03日", "追加取引", withdrawal="0", deposit="100",
            balance="500", top=300,
        )),
        page(2, transaction_row(
            "2026年09月02日", "対象取引", withdrawal="500", deposit="0",
            balance="1,000", top=330, x_offset=8,
        ), header_top=290, x_offset=8, marker=False),
    ])

    assert identity_for(original, "対象取引") == identity_for(relocated, "対象取引")
    assert identity_for(original, "対象取引").startswith(
        "bankpdf:docomo-smtb:docomo-smtb-primary:"
    )


def test_balance_inconsistency_is_counted_without_exposing_balance():
    result = parse_docomo_smtb_bank_pages([page(
        1,
        transaction_row(
            "2026年09月02日", "匿名A", withdrawal="100", deposit="0",
            balance="700", top=267,
        ),
        transaction_row(
            "2026年09月01日", "匿名B", withdrawal="0", deposit="100",
            balance="1,000", top=279,
        ),
    )])

    assert result.balance_consistency_failures == 1
    assert not hasattr(result.transactions[0], "balance")
    assert not hasattr(result.canonical_transactions[0], "balance")


def test_document_detection_selects_docomo_adapter_and_private_default_alias(monkeypatch):
    geometry = page(1, transaction_row(
        "2026年09月01日", "匿名", withdrawal="100", deposit="0",
    ))
    monkeypatch.setattr(
        "app.bank_pdf_pipeline.materialize_native_pdf", lambda path: [geometry],
    )

    assert detect_bank_pdf_adapter([geometry]).key == "docomo-smtb"
    result = BankPdfPipeline().parse("statement.pdf")
    assert result.source == DOCOMO_SMTB_SOURCE
    assert result.adapter_key == "docomo-smtb"
    assert result.transactions[0].account_alias == DEFAULT_DOCOMO_SMTB_ACCOUNT_ALIAS


def test_unrecognized_document_is_rejected():
    geometry = PageGeometry(
        1, 595, 842, (word("unknown", 10, 10),), BOUNDARIES,
    )
    with pytest.raises(BankPdfError, match="bank_document_unrecognized"):
        detect_bank_pdf_adapter([geometry])


def test_existing_jibun_header_still_selects_jibun_adapter():
    geometry = PageGeometry(
        1,
        595,
        842,
        (
            word("取引日付", 20, 100, 45),
            word("取引内容", 160, 100, 45),
            word("出金", 330, 100, 30),
            word("入金", 420, 100, 30),
            word("残高", 510, 100, 30),
        ),
        (80, 300, 390, 480),
    )

    assert detect_bank_pdf_adapter([geometry]).key == "au-jibun"


def test_generic_classification_and_docomo_semantic_holds():
    parsed = parse_docomo_smtb_bank_pages([page(
        1,
        transaction_row(
            "2026年09月05日", "給与", withdrawal="0", deposit="100",
            balance="500", top=267,
        ),
        transaction_row(
            "2026年09月04日", "口座振替 公共", withdrawal="100", deposit="0",
            balance="400", top=279,
        ),
        transaction_row(
            "2026年09月03日", "ATM 現金", withdrawal="100", deposit="0",
            balance="500", top=291,
        ),
        transaction_row(
            "2026年09月02日", "定額自動入金", withdrawal="0", deposit="100",
            balance="600", top=303,
        ),
        transaction_row(
            "2026年09月01日", "SBIハイブリッド預金", withdrawal="100", deposit="0",
            balance="500", top=315,
        ),
    )])
    decisions = build_bank_shadow_result(parsed, []).decisions

    assert [item.classification.classification for item in decisions] == [
        "income", "expense", "cash_withdrawal", "needs_review", "needs_review",
    ]
    assert decisions[3].classification.reason == "docomo_fixed_auto_deposit"
    assert decisions[4].classification.reason == "docomo_sbi_hybrid_deposit"


def test_exact_private_ownership_authority_is_account_scoped():
    transaction = parse_docomo_smtb_bank_pages([page(
        1,
        transaction_row(
            "2026年09月01日", "定額自動入金", withdrawal="0", deposit="100",
        ),
    )]).transactions[0]
    description = "定額自動入金"

    wrong_account = classify_bank_transaction(
        transaction,
        confirmed_internal_transfers=frozenset({
            (description, "incoming", "jibun-primary"),
        }),
    )
    exact_account = classify_bank_transaction(
        transaction,
        confirmed_internal_transfers=frozenset({
            (description, "incoming", DEFAULT_DOCOMO_SMTB_ACCOUNT_ALIAS),
        }),
    )

    assert wrong_account.classification == "needs_review"
    assert exact_account.classification == "transfer"
