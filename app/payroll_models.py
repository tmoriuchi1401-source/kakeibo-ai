from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class PayrollItem(BaseModel):
    raw_item_name: str
    section: Literal["earnings", "deductions", "attendance", "summary", "unknown"]
    value: int | float | str | None = None
    raw_value: str | None = None
    standard_item_candidate: str | None = None
    page: int | None = None
    x: float | None = None
    y: float | None = None
    row: int | None = None
    column: int | None = None
    confidence: float | None = None
    needs_review: bool = False


class PayrollPreview(BaseModel):
    file_type: Literal["pdf", "image"]
    extraction_method: Literal["pdf_text", "ocr"]
    pay_period: str | None = None
    pay_date: str | None = None
    gross_pay: int | None = None
    total_deductions: int | None = None
    net_pay: int | None = None
    items: list[PayrollItem] = Field(default_factory=list)
    parse_status: Literal["success", "partial", "failed"] = "failed"
