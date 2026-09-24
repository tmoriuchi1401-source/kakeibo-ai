from __future__ import annotations
import base64
import json
from hashlib import sha256
from google import genai
from pydantic import ValidationError
from .models import ReceiptResult, ProductClassificationBatch
from .medical_receipt_privacy import Classification
from .receipt_privacy_gate import ReceiptPrivacyBlocked, require_receipt_ai_permission

class GeminiAI:
    def __init__(self, api_key: str, model: str = "gemini-3.6-flash", *, request_attempts: int | None = None):
        # Use the stable v1 Interactions API.
        options = {"api_version": "v1"}
        if request_attempts is not None:
            options['retry_options'] = {'attempts': request_attempts}
        self.client = genai.Client(api_key=api_key, http_options=options)
        self.model = model

    @staticmethod
    def category_text(categories):
        return "\n".join(f"- {a} / {b}" for a, b in categories)

    def analyze_receipt(self, image_bytes: bytes, mime_type: str,
                        categories: list[tuple[str, str]], *,
                        known_source_classification: Classification | None = None) -> ReceiptResult:
        # Authorize the exact immutable bytes immediately before transport.
        if not isinstance(image_bytes, bytes):
            raise ReceiptPrivacyBlocked()
        fingerprint = sha256(image_bytes).digest()
        if fingerprint in getattr(self, "_blocked_receipts", ()):
            raise ReceiptPrivacyBlocked()
        try:
            require_receipt_ai_permission(
                image_bytes,
                mime_type,
                known_source_classification=known_source_classification,
            )
        except ReceiptPrivacyBlocked:
            if not hasattr(self, "_blocked_receipts"):
                self._blocked_receipts = set()
            self._blocked_receipts.add(fingerprint)
            raise
        prompt = f"""あなたは日本の家計簿レシート解析器です。画像またはPDFから取引情報を抽出してください。
カテゴリは必ず次の一覧からのみ選び、新カテゴリを作らないでください。
{self.category_text(categories)}

ルール:
- transaction_kind は利用者が店に支払った購入ならpurchase、店が利用者から品物を買い取り代金を支払った場合はbuyback、判別できなければunknown。
- 「買取」票の品目や現金受取額を購入支出として扱わない。店の買取代金の合計を正のtotalで返す。
- 広告中の「買取」やポイント付与だけを根拠にbuybackとしない。商品返品・返金もbuybackとしない。
- 商品ごとに税込の明細金額を抽出。数量が読めれば数量も。
- 値引きが特定商品に対応すると読める場合はその商品のamountへ反映。
- 全体値引き・クーポンは、印字された金額を負の明細として記録。二重に値引きしない。
- 税抜商品が並ぶ場合は、印字された外税を独立明細にしてもよい。内税を加算しない。
- 数量×単価と明細金額を区別し、全商品・値引き・税を上から下まで読み取る。
- 合計と明細の差を埋める架空の「調整額」を作らない。合計自体を明細合計で置き換えない。
- 商品名はレシート表記を基礎に、人が理解できる程度に正規化。
- 判断不能な商品は「その他 / 未分類」。
- 店舗全体の合計totalを必ず抽出。
- 支払方法が読める場合のみ記録。推測しない。
- 日付はYYYY-MM-DD。読めない場合は空文字。
- 店舗名も読めない場合は空文字。ファイル名・現在日付から推測しない。
- 習い事の種類（ピアノ、ダンス、体操、スイミング等）が商品/サービス名から分かる場合、
  カテゴリは教育/習い事、種類はnoteに記録。
"""
        encoded = base64.b64encode(image_bytes).decode("ascii")
        media_type = "document" if mime_type == "application/pdf" else "image"
        from .receipt_validation import validate_receipt_result
        correction='';last=None;previous=None;unresolved=None
        for attempt in range(3):
            # The same immutable original is reread, never a guessed repair or
            # an unverified crop. API/transport errors are not replayed here.
            interaction = self.client.interactions.create(
                model=self.model,
                input=[{'type':'text','text':prompt+correction},
                       {'type':media_type,'mime_type':mime_type,'data':encoded}],
                response_format={'type':'text','mime_type':'application/json',
                                 'schema':ReceiptResult.model_json_schema()},
            )
            try:
                result=ReceiptResult.model_validate_json(interaction.output_text)
            except ValidationError:
                correction='\n前回の応答は指定JSON形式ではありませんでした。原本から指定形式で再読取してください。'
                continue
            last=result
            ok,issues=validate_receipt_result(result,categories)
            changed=previous is not None and any(a and a!=b for a,b in (
                (previous.date,result.date),(previous.merchant,result.merchant),(previous.total,result.total)))
            if ok and not changed:return result
            if not ok:unresolved=result
            if changed:
                issues=issues+['前回と日付・店舗・合計が変化。原本の印字で再確認']
            previous=result
            correction=('\n検証で次の問題が見つかりました: '+'; '.join(issues)+
                '\n前回結果は誤りを含む参考データです。原本を拡大して見直し、読み落とした商品・数量・値引き・外税を確認してください。'
                '印字を確認できない項目は推測せず未確定のまま返してください。\n前回結果:\n'+
                json.dumps(result.model_dump(),ensure_ascii=False))
        if last is None:raise RuntimeError('receipt_response_invalid')
        # A changed total must not make an omitted item look balanced. If the
        # bounded reread could not corroborate it, preserve the invalid reading
        # for review rather than posting the uncorroborated replacement.
        return unresolved or last

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
