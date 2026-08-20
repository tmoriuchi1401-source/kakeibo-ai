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
- baseline後のAmazon差分商品を固定支出IDで「支出明細」へ商品単位計上
- au PAY利用通知メールを伝票番号で重複なく「取込データ」へ登録
- レシートとau PAY／カード利用を金額・日付・店舗で安全側に照合
- GitHub Actionsで3時間ごと＋手動実行
- 処理済み、要確認、既取込のレシート画像を `receipt_processed` へ退避

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

iPhoneへGoogle Driveアプリを入れてログインする。Driveアプリの右下にある追加ボタンから
「スキャン」（カメラアイコン）を選び、レシートを撮影する。撮影後の保存先として
`receipt_inbox` を指定する。Driveアプリが作成するPDFはそのまま取込対象になる。

ファイル名の変更は必須ではない。GitHub Actionsが3時間ごとにDriveを確認し、正常取込、
要確認、既取込の画像またはPDFを `receipt_processed` へ移す。解析に失敗して記録も
できなかったファイルは `receipt_inbox` に残るため、原因を確認して再処理できる。

初回テストでは1枚撮影し、Driveアプリで `receipt_inbox` への保存を確認してから、
GitHub Actionsの `Process receipt inbox` を手動実行する。完了後、ファイルが
`receipt_processed` に移り、Sheetsの「レシート」「取込データ」と、正常時は
「支出一覧」に反映されることを確認する。

参考: [Google: iPhoneで書類をスキャンする](https://support.google.com/drive/answer/3145835?co=GENIE.Platform%3DiOS&hl=ja)

## 8. GitHub Actions
Secretsに以下を登録:
- GEMINI_API_KEY
- SPREADSHEET_ID
- GOOGLE_SERVICE_ACCOUNT_JSON（JSON全文）
- RECEIPT_DRIVE_FOLDER_ID
- PROCESSED_DRIVE_FOLDER_ID（推奨。`receipt_processed` のフォルダURLまたはID）
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

## 取込データの統合・重複排除

書き込まずに候補件数だけ確認する:

```bash
python -m app.cli reconcile-preview
```

判定結果を「取込データ」へ反映する:

```bash
python -m app.cli reconcile
```

解析済みレシートを正本とし、au PAYまたは通常カード利用が金額・日付・店舗で
一意に一致した場合だけ決済側を `matched_receipt` にする。候補が複数ある場合や、
同じレシートを複数決済が参照する場合は `needs_review_duplicate` とし、自動統合しない。
au PAY残高オートチャージとAmazon照合済みカードは対象外とする。

## スマホ用「要確認」シート

未分類のPayPay・au PAY・au PAYカードの明確な支払いは、先に書き込みなしで
件数を確認できる:

```bash
python -m app.cli auto-expense-preview
```

確認後に `python -m app.cli auto-expense` を実行すると、明確な支払いを
`その他 / 未分類` または限定的な高信頼ルールのカテゴリで支出化する。
Amazon分割払い、返金・取消、資金移動候補、既存レシート候補は自動計上しない。
後から一意に一致するレシートが取り込まれた場合は `reconcile` がレシートを
正本とし、自動支出行を `duplicate_excluded` にする。

Google Sheetsアプリでは「要確認」シートだけを開けば、対応が必要な取引を確認できる。
このシートは自動生成専用で、3時間ごとのGitHub Actions実行時に最新状態へ更新される。
日付列はスマホ表示を含め `yyyy/mm/dd` 形式に統一する。

- 高: レシート解析エラー、曖昧な重複候補
- 中: レシート未照合のau PAY、未分類カード利用
- 表示しない: オートチャージ、Amazon照合済み、正常統合済み

書き込まずに件数を確認する:

```bash
python -m app.cli review-preview
```

手動でシートを更新する:

```bash
python -m app.cli review-refresh
```

「要確認」シートの自動生成列（A〜I）は編集しない。スマホから対応する場合は
J〜N列だけを入力する。

- ユーザー判断: `支出として計上` / `重複として除外` / `レシートと統合` / `保留`
- 統合先取込ID: `レシートと統合` の場合に入力
- 大カテゴリ・小カテゴリ: `支出として計上` の場合にカテゴリマスタから選択
- ユーザー備考: 任意
- 反映結果: システムが結果またはエラーを記録

判断とカテゴリはシート上のドロップダウンから選択する。大・小カテゴリの組合せが
カテゴリマスタに存在しない場合は反映しない。入力した判断は次の3時間ごとの
GitHub Actionsで反映される。すぐ反映したい場合はActionsを手動実行する。

手動で判断だけを反映する:

```bash
python -m app.cli review-apply
```

## スマホ用「支出一覧」シート

「支出明細」は監査用として除外行も保持する。「支出一覧」は `計上状態=active` の
支出だけを新しい日付順に表示する最終画面で、`duplicate_excluded` は表示しない。
Google Sheetsアプリでは通常このシートを見れば、二重計上を除いた最終支出を確認できる。
日付表示は `yyyy/mm/dd` に統一し、3時間ごとのActionsで自動更新する。

書き込まずに最終件数・合計を確認する:

```bash
python -m app.cli expenses-preview
```

手動で「支出一覧」を更新する:

```bash
python -m app.cli expenses-refresh
```

## 次の実装
1. 店舗名マスタと照合ルールを拡充
2. 実運用データを使って要確認判定を調整

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

baselineの行は過去データの基準点として保持し、支出明細へ一括計上しない。
baseline後に検出した新規・変更商品だけを、Amazonキーから作る固定支出IDで
「支出明細」へ追加・更新する。同時に注文単位の合計を「取込データ」へ
`canonical_amazon` として記録し、レシートとの重複判定に使用する。

Amazon注文合計とAmazonレシートが金額・日付・店舗で一意に一致した場合は、
商品単位のAmazon明細を正本とし、レシート由来支出の `計上状態` を
`duplicate_excluded` にする。元のレシート・支出行は監査用に削除しない。


## v3.4: Sheets書き込みクォータ対策
Amazon取込は1行ずつSheets APIへ書き込まず、新規行をまとめて1回のappend、
変更行をまとめて1回のbatchUpdateで反映します。
途中で429になった旧版から再実行しても、すでに登録済みのAmazonキーは検出されるため二重登録しません。
