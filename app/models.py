from __future__ import annotations
from pydantic import BaseModel, Field
from typing import Optional

class ReceiptItem(BaseModel):
    name: str
    quantity: float = 1
    amount: int = Field(description="税込の明細合計金額。値引き反映後が読める場合は反映")
    major_category: str
    minor_category: str
    note: str = ""
    confidence: float = Field(default=0.8, ge=0, le=1)

class ReceiptResult(BaseModel):
    merchant: str
    date: str = Field(description="YYYY-MM-DD。読めない場合は空文字")
    total: int
    payment_method: str = ""
    items: list[ReceiptItem]
    note: str = ""

class ProductClassification(BaseModel):
    asin: str
    major_category: str
    minor_category: str
    note: str = ""
    confidence: float = Field(default=0.8, ge=0, le=1)

class ProductClassificationBatch(BaseModel):
    products: list[ProductClassification]
