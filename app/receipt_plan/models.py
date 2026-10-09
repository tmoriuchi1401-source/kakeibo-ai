"""Plan-only subset of PR #91 (1ac98bd); no live authority/writer transport."""
from pydantic import BaseModel,Field,field_validator
from typing import Literal,Optional
class ReceiptItem(BaseModel):
    name: str
    quantity: Optional[float] = 1
    amount: int = Field(description="税込の明細合計金額。値引き反映後が読める場合は反映")
    major_category: str
    minor_category: str
    note: str = ""
    confidence: float = Field(default=0.8, ge=0, le=1)

class ReceiptResult(BaseModel):
    merchant: str = ''
    transaction_kind: Literal["purchase", "buyback", "unknown"] = "purchase"
    date: str = Field(description="YYYY-MM-DD。読めない場合は空文字")
    total: int
    payment_method: str = ""
    items: list[ReceiptItem]
    note: str = ""

    @field_validator('merchant', 'payment_method', mode='before')
    @classmethod
    def optional_text(cls, value):
        return '' if value is None else value
