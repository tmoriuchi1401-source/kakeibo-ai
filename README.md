# 家計簿AI — 無料運用を前提にした初期実装

## 現在実装済み
- Google Sheets 7シートのヘッダー/カテゴリマスタ初期化
- レシート画像 → Gemini structured output → カテゴリ検証 → Sheets登録
- レシート明細合計と総額が合わない場合は「要確認」にして支出へ自動計上しない
- Google Drive のレシート受信フォルダをPythonで巡回
- Amazon Order History.csv の全履歴CSVから差分抽出
- Amazonは `Order ID + ASIN` を商品行キーに使用し、quantity=0の取消/調整行を除外
- Amazon商品マスタ（ASIN）を再利用し、未知商品だけGemini分類
- 同じAmazonキーで内容が変わった場合だけ更新
- au PAY利用通知メールを伝票番号で重複なく「取込データ」へ登録
- GitHub Actionsで1時間ごと＋手動実行

## シート名
支出明細 / レシート / カテゴリ / 店舗 / 取込データ / Amazon注文 / 商品マスタ

## 1. Google Cloud
1. Google Cloudでプロジェクトを作成。
2. Google Sheets API と Google Drive API を有効化。
3. サービスアカウントを作成しJSONキーを発行。
4. 作成済みの家計簿スプレッドシートをサービスアカウントのメールアドレスへ「編集者」で共有。
5. Google Driveに `receipt_inbox` と `receipt_processed` フォルダを作り、同じサービスアカウントへ編集権限を付与。

## 2. Google AI Studio
Gemini APIキーを発行。`.env` の `GEMINI_API_KEY` に設定。APIキーはGitHubへコミットしない。

## 3. ローカル/Codespaces設定
```bash
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\\Scripts\\activate
pip install -r requirements.txt
cp .env.example .env
```
`.env` に `SPREADSHEET_ID` 等を設定し、サービスアカウントJSONを `service-account.json` として置く。

## 4. Sheetsを初期化
```bash
python -m app.cli init
```
既存シートは維持し、必要なヘッダーと確定カテゴリを書き込む。

## 5. レシート1枚でテスト
```bash
python -m app.cli receipt /path/to/receipt.jpg
```
合計整合性が取れれば「支出明細」に商品行が入る。不整合なら「レシート」「取込データ」に要確認として残り、支出には入らない。

## 6. Amazon履歴を初回投入
```bash
python -m app.cli amazon "/path/to/Order History.csv"
```
毎回フル履歴CSVでもよい。同じ `Order ID + ASIN` は再追加されず、内容変更だけ更新される。

### 今回の実CSVで確認した点
- 880行
- `Order ID + ASIN` は879通り
- 重複1組は quantity=4 の購入行と quantity=0 の調整行
- quantity=0を除外すると836行すべて `Order ID + ASIN` が一意
- 688注文ID、複数商品注文127件、最大6商品/注文

## 7. iPhone運用
iPhoneショートカットで「写真を撮る → Google Driveのreceipt_inboxへ保存」を作る。
GitHub Actionsは1時間ごとにDriveを確認するため、PCは不要。
必要ならGitHub Mobile/ブラウザから workflow_dispatch で即時実行もできる。

## 8. GitHub Actions
Secretsに以下を登録:
- GEMINI_API_KEY
- SPREADSHEET_ID
- GOOGLE_SERVICE_ACCOUNT_JSON（JSON全文）
- RECEIPT_DRIVE_FOLDER_ID
- PROCESSED_DRIVE_FOLDER_ID（任意）
- GOOGLE_GMAIL_TOKEN_JSON（任意。Gmail読み取り専用OAuthのauthorized-user JSON）

`GOOGLE_GMAIL_TOKEN_JSON` が未登録なら、定期処理は従来どおりレシートだけを処理し、
Gmail取込ステップはスキップする。Gmail用トークンは個人Gmailを読めないサービス
アカウントJSONとは別にする。権限は `gmail.readonly` のみに限定する。

au PAY通知の検索条件を絞る場合は、GitHub Actions Variables の
`AUPAY_GMAIL_QUERY` に Gmail 検索式を設定する。未設定時は直近30日の
`info@wallet.auone.jp` から届いたメールコードP1002の利用通知だけを検索し、
種別が「支払」で必須項目を抽出できる通知だけを登録する。

ローカルに保存した実メール（`.eml`）で、GmailやSheetsへ書き込まずパーサーだけを
確認する場合:

```bash
python -c "from app.aupay_mail_pipeline import parse_eml; print(parse_eml('notice.eml'))"
```

Sheetsへ1件登録する場合:

```bash
python -m app.cli aupay-eml notice.eml
```

Gmailから取り込む場合:

```bash
python -m app.cli aupay-gmail --max-results 100
```

Gmail OAuthの初回認証は、OAuthクライアントJSONを保存したPC上で実行する。
どちらのJSONもリポジトリ外へ置き、内容をターミナルへ表示しない。

```bash
python -m app.cli gmail-authorize "/path/to/client_secret.json" "/path/to/gmail-token.json"
```

ブラウザで自分のGmailアカウントを選び、読み取り専用アクセスを許可すると、
指定先にGitHub Actions用のauthorized-user JSONが生成される。

件名が「【ご利用詳細】au PAY カード」のメールは、au PAY残高決済通知とは形式が
異なり、1通に複数のカード利用が含まれる。ローカルの `.eml` を取り込む場合は:

```bash
python -m app.cli card-eml-import "【ご利用詳細】au PAY カード.eml"
```

この形式には伝票番号がないため、RFC Message-IDのハッシュとメール内の明細番号を
一意キーにする。オートチャージ、Amazon、その他利用の判定はカードCSVと同じ
ルールを使用する。

伝票番号を `aupay:<伝票番号>` という取込IDにするため、同じメールを何度検索しても
二重登録されない。必須項目が欠けるメールは自動登録せず `needs_review` として集計する。

## 次の実装
1. au PAY利用通知の実メールで表記揺れを確認しparserを調整
2. receipt / Amazon / au PAY / au PAYカード間の決済照合を追加
3. 確信度の低い行だけスマホ確認できる「要確認」ビューを追加

## 安全設計
- APIキー、サービスアカウントJSONはGitにコミットしない。
- AIがカテゴリを新設できないよう、Sheetsのカテゴリマスタとの一致をPythonで検証。
- 合計不一致は自動計上しない。
- 生データ側のID/ハッシュを保持して再取込による二重計上を防ぐ。


## 5a. 書き込まずに解析テスト
```bash
python -m app.cli analyze receipt_test.jpg
```
Sheetsへ書き込まず、Gemini解析結果だけを表示する。


## Amazon初回基準点（推奨）
AmazonのデータリクエストCSVは毎回全期間になるため、初回だけ次を実行します。

```bash
python -m app.cli amazon-baseline "Order History.csv"
```

このコマンドは過去の有効行を `Amazon注文` に基準点として記録しますが、Gemini分類は行いません。
次回以降は新規・変更行だけを検出し、未知ASINだけGeminiで分類します。

```bash
python -m app.cli amazon "Order History.csv"
```


## v3.4: Sheets書き込みクォータ対策
Amazon取込は1行ずつSheets APIへ書き込まず、新規行をまとめて1回のappend、
変更行をまとめて1回のbatchUpdateで反映します。
途中で429になった旧版から再実行しても、すでに登録済みのAmazonキーは検出されるため二重登録しません。
