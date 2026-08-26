from __future__ import annotations

from dataclasses import dataclass


AMAZON_REVIEW_SHEET = "Amazon要確認"
AMAZON_REVIEW_HEADERS = [
    "Review ID",
    "確認状態",
    "イベント種別",
    "イベント日",
    "確認理由",
    "対象範囲",
    "候補数区分",
    "候補選択",
    "選択候補ID",
    "選択状態",
    "解決Order ID",
    "反映結果",
    "作成日時",
    "解決日時",
]


@dataclass(frozen=True, repr=False)
class AmazonReviewEventRow:
    review_id: str
    review_status: str
    event_type: str
    event_date: str
    reasons: tuple[str, ...]
    scope: str
    candidate_count_class: str
    candidate_selection: str = ""
    selected_candidate_id: str = ""
    selection_status: str = ""
    resolved_order_id: str = ""
    apply_result: str = "未反映"
    created_at: str = ""
    resolved_at: str = ""

    def __repr__(self) -> str:
        return (
            "AmazonReviewEventRow(review_id=<redacted>, "
            f"review_status={self.review_status!r}, event_type={self.event_type!r})"
        )

    def to_sheet_row(self) -> list[str]:
        """Return the fixed 14-column representation without writing it anywhere."""

        return [
            self.review_id,
            self.review_status,
            self.event_type,
            self.event_date,
            ";".join(self.reasons),
            self.scope,
            self.candidate_count_class,
            self.candidate_selection,
            self.selected_candidate_id,
            self.selection_status,
            self.resolved_order_id,
            self.apply_result,
            self.created_at,
            self.resolved_at,
        ]
