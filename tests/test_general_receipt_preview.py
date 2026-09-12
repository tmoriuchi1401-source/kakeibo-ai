from __future__ import annotations

import pytest

from app.general_receipt_preview import (
    GeneralReceiptPreviewPipeline,
    extract_total_candidates,
    preview_general_receipt_bytes,
    preview_general_receipt_text,
)
from app.receipt_text_extraction import _ReceiptTextExtraction
from app.medical_ocr_observation_shadow import make_observation
from app.medical_receipt_privacy import _StructuredOcrToken, classify_receipt_text


# Eight deliberately small, human-readable receipt layouts.  Ground truth is
# visible alongside each sample so failures remain cheap to adjudicate.
LAYOUTS = [
    (
        "スーパー青空 大宮店\n2026/09/01 18:32\n小計 980円\n消費税 98円\n合計 1,078円\nお預り 2,000円\nお釣り 922円",
        ("2026-09-01", "スーパー青空 大宮店", 1078),
    ),
    (
        "株式会社みどりストア\n毎度ありがとうございます\n取引日時 2026-09-02 09:10\nお買上金額 ￥2,480\nポイント利用 100円",
        ("2026-09-02", "株式会社みどりストア", 2480),
    ),
    (
        "コンビニさくら駅前店\nTEL 00-0000-0000\n26-09-03 07:05\n税抜合計 500円\n合計金額 550円\n現金 550円",
        ("2026-09-03", "コンビニさくら駅前店", 550),
    ),
    (
        "青葉マーケット\nレシート\n購入日: 2026年9月4日\n小計 3,000\nクーポン値引 200円\nお支払金額 2,800円",
        ("2026-09-04", "青葉マーケット", 2800),
    ),
    (
        "有限会社 北町商店\n発行日 2026.09.05\n内税 73円\n現計 880円\nお預り 1,000円\n釣銭 120円",
        ("2026-09-05", "有限会社 北町商店", 880),
    ),
    (
        "DAILY FOODS\n中央店\n日時 2026/09/06 12:00\n総合計\n¥1,234-\nポイント残高 999",
        ("2026-09-06", "DAILY FOODS", 1234),
    ),
    (
        "ひかりマート本店\nR8年9月7日 13:45\n小計 4,000円\n税込合計 4,400円",
        ("2026-09-07", "ひかりマート本店", 4400),
    ),
    (
        "花まるストア\n2026/09/08\n商品A 300円\n商品B 450円\n合計 750円\n合計 750円",
        ("2026-09-08", "花まるストア", 750),
    ),
]


@pytest.mark.parametrize(
    "label", ("合計", "合計金額", "お買上金額", "お支払金額", "現計", "総合計"),
)
def test_required_total_labels_are_normal_receipt_transaction_signals(label):
    decision = classify_receipt_text(f"レシート\n商品A\n{label} 500円")

    assert decision.classification == "normal"


@pytest.mark.parametrize(("text", "ground_truth"), LAYOUTS)
def test_eight_layout_harness_extracts_minimum_transaction(text, ground_truth):
    result = preview_general_receipt_text(text, source_id="sample")

    assert result.status == "ready"
    assert (result.purchase_date, result.merchant, result.total) == ground_truth
    assert result.transaction_row is not None
    assert result.transaction_row[2] == "receipt"
    assert result.transaction_row[4:7] == ground_truth
    assert result.write_plan_rows == 1
    assert result.write_authorized is False
    assert result.external_ai_used is False


@pytest.mark.parametrize(
    "line",
    [
        "小計 1,000円", "税抜合計 1,000円", "消費税 100円", "お預り 2,000円",
        "釣銭 900円", "ポイント合計 500", "クーポン合計 300円", "値引合計 100円",
    ],
)
def test_non_total_monetary_context_never_becomes_total(line):
    assert extract_total_candidates(line, "a" * 64) == ()


def test_real_ocr_tax_total_is_excluded_and_currency_grouping_is_bounded():
    text = "合計/ 11点 ¥3. 788\n(税合計 ¥313)"

    candidates = extract_total_candidates(text, "a" * 64)

    assert tuple(candidate.value for candidate in candidates) == (3788,)


def test_invalid_grouping_is_review_instead_of_partial_total():
    assert extract_total_candidates(r"合計 \5,4065", "a" * 64) == ()


def test_real_ocr_deposit_total_is_not_final_total():
    text = "入金額合計 6,367\n合計 ¥6, 365"

    candidates = extract_total_candidates(text, "a" * 64)

    assert tuple(candidate.value for candidate in candidates) == (6365,)


def test_payment_detail_and_customer_copy_are_normal_receipt_evidence():
    assert classify_receipt_text("決済利用明細\n総合計 530円").classification == "normal"
    assert classify_receipt_text("お客様控え\nコード決済支払\n金額 394円").classification == "normal"


def test_explicit_corporate_merchant_can_be_below_header():
    result = preview_general_receipt_text(
        "読取ノイズ\n2026/08/29\n合計 6,365円\n株式会社 ベルク",
        source_id="corporate",
    )

    assert result.merchant == "株式会社 ベルク"


def test_trailing_ocr_punctuation_is_removed_from_merchant():
    result = preview_general_receipt_text(
        "STARBUCKS”\n決済利用明細\n2026/09/10\n総合計 206円",
        source_id="punctuation",
    )

    assert result.merchant == "STARBUCKS"


def test_conflicting_total_candidates_require_review_and_withhold_total():
    result = preview_general_receipt_text(
        "青空ストア\n2026/09/01\n合計 1,000円\nお支払金額 900円",
        source_id="ambiguous",
    )

    assert result.status == "needs_review"
    assert result.total is None
    assert result.issues == ("total_ambiguous",)
    assert result.write_plan_rows == 0


def test_low_confidence_merchant_ocr_requires_review():
    tokens = (
        _StructuredOcrToken("青空", 1, 10, 10, 80, 20, 42, (1, 1, 1, 5)),
        _StructuredOcrToken("ストア", 1, 95, 10, 100, 20, 92, (1, 1, 1, 5)),
    )
    result = preview_general_receipt_text(
        "青空ストア\nレシート\n2026/09/01\n合計 500円",
        source_id="low-merchant", ocr_tokens=tokens,
    )

    assert result.status == "needs_review"
    assert "merchant_ocr_low_confidence" in result.issues
    assert result.write_plan_rows == 0


def test_expiry_date_is_excluded_and_competing_purchase_dates_require_review():
    expiry = preview_general_receipt_text(
        "青空ストア\n購入日 2026/09/01\n有効期限 2026/10/01\n合計 500円",
        source_id="expiry",
    )
    conflicting = preview_general_receipt_text(
        "青空ストア\n2026/09/01\n2026/09/02\n合計 500円",
        source_id="dates",
    )

    assert expiry.purchase_date == "2026-09-01" and expiry.status == "ready"
    assert conflicting.purchase_date is None
    assert conflicting.status == "needs_review"
    assert "date_ambiguous" in conflicting.issues


def _row(import_id, source, day, merchant, amount, status="解析済", digest=""):
    return [import_id, "", source, import_id, day, merchant, amount, "", status, "", digest, ""]


def test_exact_and_semantic_duplicates_stop_the_write_plan():
    text = "青空ストア\n2026/09/01\n合計 500円"
    digest = "b" * 64
    exact = preview_general_receipt_text(
        text, source_id="r1", source_sha256=digest,
        existing_rows=[_row("receipt:old", "receipt", "2026-01-01", "別店舗", 1, digest=digest)],
    )
    possible = preview_general_receipt_text(
        text, source_id="r2", source_sha256="c" * 64,
        existing_rows=[_row("receipt:old", "receipt", "2026-09-01", "青空ストア本店", 500)],
    )

    assert exact.status == "duplicate"
    assert exact.duplicate_state == "exact"
    assert exact.write_plan_rows == 0
    assert possible.status == "needs_review"
    assert possible.duplicate_state == "possible"
    assert possible.write_plan_rows == 0


def test_hypothetical_transaction_connects_to_existing_reconciliation():
    result = preview_general_receipt_text(
        "青空ストア\n2026/09/01\n合計 500円", source_id="r1",
        existing_rows=[
            _row("paypay:1", "PayPay", "2026-09-01", "青空ストア", 500, "unclassified_paypay")
        ],
    )

    assert result.status == "ready"
    assert result.reconciliation_status == "matched_receipt"
    assert result.reconciliation_candidate_ids == ("paypay:1",)


def test_bytes_path_blocks_medical_without_external_ai(monkeypatch):
    import app.general_receipt_preview as module

    extracted = _ReceiptTextExtraction(
        "extracted", "image_ocr", "青空病院\n診療費\n患者負担額 3,250円", (), True,
    )
    monkeypatch.setattr(module, "_extract_receipt_text", lambda content, mime: extracted)

    result = preview_general_receipt_bytes(b"private", "image/png", source_id="medical")

    assert result.status == "privacy_blocked"
    assert result.classification == "medical"
    assert result.transaction_row is None
    assert result.external_ai_used is False


def test_bytes_path_uses_one_private_extraction_and_no_writer(monkeypatch):
    import app.general_receipt_preview as module

    calls = []
    extracted = _ReceiptTextExtraction(
        "extracted", "pdf_text", "青空ストア\nレシート\n2026/09/01\n合計 500円", (), True,
    )
    monkeypatch.setattr(
        module, "_extract_receipt_text", lambda content, mime: calls.append((content, mime)) or extracted,
    )

    class ReadOnlyDB:
        def get(self, value):
            assert value == "取込データ!A2:L"
            return []

    result = GeneralReceiptPreviewPipeline(ReadOnlyDB()).preview_bytes(
        b"pdf", "application/pdf", source_id="one",
    )

    assert calls == [(b"pdf", "application/pdf")]
    assert result.status == "ready"
    assert result.duplicate_state == "none"
    assert result.write_plan_rows == 1
    assert result.write_authorized is False


def test_pipeline_can_reuse_bound_offline_rapidocr_observation():
    class FakeRapidOcr:
        def observe(self, image):
            rows = ["青空ストア", "レシート", "2026/09/01", "合計 500円"]
            regions = [
                {
                    "text": text,
                    "confidence": 0.98,
                    "detection_confidence": 0.99,
                    "polygon": [[10, 10 + index * 30], [300, 10 + index * 30],
                                [300, 30 + index * 30], [10, 30 + index * 30]],
                }
                for index, text in enumerate(rows)
            ]
            return make_observation(image, "rapidocr/3.9.2", ("a" * 64,), 400, 800, regions)

    result = GeneralReceiptPreviewPipeline(rapidocr_adapter=FakeRapidOcr()).preview_bytes(
        b"immutable-image", "image/jpeg", source_id="rapid-one",
    )

    assert result.status == "ready"
    assert result.extraction_method == "rapidocr"
    assert (result.purchase_date, result.merchant, result.total) == (
        "2026-09-01", "青空ストア", 500,
    )


def test_rapidocr_binding_or_completeness_failure_stops_parsing():
    class WrongBinding:
        def observe(self, image):
            return make_observation(image, "rapidocr/3.9.2", (), 400, 800, [],
                                    ("observation_incomplete",))

    result = GeneralReceiptPreviewPipeline(rapidocr_adapter=WrongBinding()).preview_bytes(
        b"immutable-image", "image/jpeg", source_id="rapid-one",
    )

    assert result.status == "extraction_failed"
    assert result.issues == ("observation_incomplete",)
    assert result.transaction_row is None


def test_arbitrary_normal_document_without_receipt_total_is_not_receipt():
    result = preview_general_receipt_text(
        "青空株式会社\n会議資料\n2026/09/01", source_id="document",
    )

    assert result.status == "not_receipt"
    assert result.transaction_row is None


def test_cli_is_local_read_only_by_default(monkeypatch, tmp_path, capsys):
    import json
    import sys
    import app.general_receipt_preview as module
    from app.cli import main

    path = tmp_path / "receipt.pdf"
    path.write_bytes(b"pdf")
    extracted = _ReceiptTextExtraction(
        "extracted", "pdf_text", "青空ストア\nレシート\n2026/09/01\n合計 500円", (), True,
    )
    monkeypatch.setattr(module, "_extract_receipt_text", lambda content, mime: extracted)
    monkeypatch.setattr(sys, "argv", ["kakeibo", "general-receipt-preview", str(path)])

    main()

    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "ready"
    assert output["write_plan_rows"] == 1
    assert output["duplicate"]["state"] == "not_checked"
    assert output["external_ai_used"] is False
    assert output["write_authorized"] is False
