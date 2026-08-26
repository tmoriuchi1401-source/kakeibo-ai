from app.amazon_cancellation_return_preview import (
    _build_review_plan,
    cancellation_review_event_row,
)
from app.amazon_review import AMAZON_REVIEW_HEADERS
from app.sheets import HEADERS


EXISTING_REVIEW_HEADERS = [
    "確認ID", "優先度", "日付", "データ元", "店舗", "金額", "状態", "推奨対応", "備考",
    "ユーザー判断", "統合先取込ID", "カテゴリ（大｜小）", "小カテゴリ（従来）", "ユーザー備考",
    "反映結果", "Amazon候補", "Amazon候補数", "Amazon注文候補選択", "Amazon候補ID", "Amazon選択状態",
]


def _planned_row(*, source_hash="a" * 64, created_at="2026-08-26 12:34:56"):
    plan = _build_review_plan(
        b"private cancellation message",
        source_hash=source_hash,
        event_date="2026-08-24",
        matching={"candidate_count_2plus": 1},
        cancellation_scope="partial_likely",
    )
    assert plan is not None
    return cancellation_review_event_row(plan, created_at=created_at)


def test_amazon_review_schema_has_fixed_fourteen_column_order():
    assert AMAZON_REVIEW_HEADERS == [
        "Review ID", "確認状態", "イベント種別", "イベント日", "確認理由", "対象範囲",
        "候補数区分", "候補選択", "選択候補ID", "選択状態", "解決Order ID", "反映結果",
        "作成日時", "解決日時",
    ]
    assert len(AMAZON_REVIEW_HEADERS) == 14


def test_cancellation_plan_converts_to_initial_review_row():
    review = _planned_row()

    assert review.review_status == "未確認"
    assert review.event_type == "cancellation"
    assert review.event_date == "2026-08-24"
    assert review.scope == "partial_likely"
    assert review.reasons == ("missing_order_id", "multiple_candidates")
    assert review.candidate_count_class == "2plus"
    assert review.candidate_selection == ""
    assert review.selected_candidate_id == ""
    assert review.selection_status == ""
    assert review.resolved_order_id == ""
    assert review.created_at == "2026-08-26 12:34:56"
    assert len(review.to_sheet_row()) == 14


def test_review_id_is_stable_and_created_at_is_explicitly_injected():
    first = _planned_row(created_at="fixed-one")
    second = _planned_row(created_at="fixed-two")

    assert first.review_id == second.review_id
    assert first.created_at == "fixed-one"
    assert second.created_at == "fixed-two"


def test_review_representation_does_not_expose_identity_or_source_data():
    private_source = "private-source-hash"
    review = _planned_row(source_hash=private_source)
    rendered = repr(review)

    assert review.review_id not in rendered
    assert private_source not in rendered


def test_existing_review_schema_is_unchanged_and_new_schema_is_not_installed():
    assert HEADERS["要確認"] == EXISTING_REVIEW_HEADERS
    assert "Amazon要確認" not in HEADERS
