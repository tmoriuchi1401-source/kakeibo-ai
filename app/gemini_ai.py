from __future__ import annotations
import base64
from hashlib import sha256
from google import genai
from .models import ReceiptResult, ProductClassificationBatch
from .medical_receipt_privacy import Classification
from .receipt_privacy_gate import ReceiptPrivacyBlocked, require_receipt_ai_permission

class GeminiAI:
    def __init__(self, api_key: str, model: str = "gemini-3.6-flash"):
        # Use the stable v1 Interactions API.
        self.client = genai.Client(api_key=api_key, http_options={"api_version": "v1"})
        self.model = model

    @staticmethod
    def category_text(categories):
        return "\n".join(f"- {a} / {b}" for a, b in categories)

    def analyze_receipt(self, image_bytes: bytes, mime_type: str,
                        categories: list[tuple[str, str]], *,
                        known_source_classification: Classification | None = None) -> ReceiptResult:
        # Only immutable bytes may be checked and then submitted; reject mutable
        # buffers rather than authorizing one image and encoding another.
        if not isinstance(image_bytes, bytes):
            raise ReceiptPrivacyBlocked()
        # Denials are monotonic for these exact bytes during this adapter's
        # lifetime. No raw bytes, source identifiers, or successful permits are
        # cached. Across instances/runs the caller must carry source provenance.
        fingerprint = sha256(image_bytes).digest()
        if fingerprint in getattr(self, "_blocked_receipts", ()):
            raise ReceiptPrivacyBlocked()
        try:
            require_receipt_ai_permission(
                image_bytes, mime_type, known_source_classification=known_source_classification,
            )
        except ReceiptPrivacyBlocked:
            if not hasattr(self, "_blocked_receipts"):
                self._blocked_receipts = set()
            self._blocked_receipts.add(fingerprint)
            raise
        prompt = f"""あなたは日本の家計簿レシート解析器です。画像またはPDFから購入情報を抽出してください。
カテゴリは必ず次の一覧からのみ選び、新カテゴリを作らないでください。
{self.category_text(categories)}

ルール:
- 商品ごとに税込の明細金額を抽出。数量が読めれば数量も。
- 値引きが特定商品に対応すると読める場合はその商品のamountへ反映。
- 商品名はレシート表記を基礎に、人が理解できる程度に正規化。
- 判断不能な商品は「その他 / 未分類」。
- 店舗全体の合計totalを必ず抽出。
- 支払方法が読める場合のみ記録。推測しない。
- 日付はYYYY-MM-DD。読めない場合は空文字。
- 習い事の種類（ピアノ、ダンス、体操、スイミング等）が商品/サービス名から分かる場合、
  カテゴリは教育/習い事、種類はnoteに記録。
"""
        encoded = base64.b64encode(image_bytes).decode("ascii")
        media_type = "document" if mime_type == "application/pdf" else "image"
        interaction = self.client.interactions.create(
            model=self.model,
            input=[
                {"type": "text", "text": prompt},
                {"type": media_type, "mime_type": mime_type, "data": encoded},
            ],
            response_format={
                "type": "text",
                "mime_type": "application/json",
                "schema": ReceiptResult.model_json_schema(),
            },
        )
        return ReceiptResult.model_validate_json(interaction.output_text)

    def classify_products(self, products: list[dict],
                          categories: list[tuple[str, str]]) -> ProductClassificationBatch:
        prompt = f"""Amazon購入商品を家計簿カテゴリへ分類してください。カテゴリは必ず一覧から選んでください。
{self.category_text(categories)}

商品:
""" + "\n".join(f"ASIN={p['asin']} | {p['name']}" for p in products) + """
ルール:
- 商品名から用途を判断。
- 教育目的が明白な教材・参考書は教育/教材・参考書。
- ペット用商品は日用品/ペット用品。
- 判断できなければ その他/未分類。
- ASINは入力値をそのまま返す。
"""
        interaction = self.client.interactions.create(
            model=self.model,
            input=prompt,
            response_format={
                "type": "text",
                "mime_type": "application/json",
                "schema": ProductClassificationBatch.model_json_schema(),
            },
        )
        return ProductClassificationBatch.model_validate_json(interaction.output_text)
