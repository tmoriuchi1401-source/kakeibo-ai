# KakeiboAI PROJECT STATUS

> **役割:** KakeiboAI開発の「現在地の正本」。新しいCodex / Work / Goalを開始するときは、過去チャットや古いbranchより先にこのファイルを読む。
>
> **重要:** このファイルは状況整理であり、本番write・apply・権限拡張・外部データ変更の承認ではない。

## 0. このファイルの使い方

- 大きなPhaseが終わったときだけ更新する。細かな診断ごとには更新しない。
- 各機能を **L0〜L4** で管理する。
  - **L0 未着手**: 本番用途の実装なし
  - **L1 解析可能**: 入力を安全に読み、previewできる
  - **L2 取込可能**: stable identity / dedupe付きで取込データへ登録できる
  - **L3 家計簿反映可能**: 支出・収入・除外・要確認まで到達できる
  - **L4 定期運用確認済み**: 実データで自動運用・再実行安全性を確認済み
- `A/B/C` 判定だけを書かない。「何についてAか」を必ず書く。
- `main SHA` や実行件数は **Last verified** として扱い、再開時に必要なら確認する。
- 過去の実験branchの成績で、本番機能を未完成へ戻さない。
- 個人開発方針として、完全性・網羅性・過剰な安全証明より **実用性・開発時間・保守負担・削減作業量** を優先する。重大事故を防ぐ安全境界は維持し、低頻度・低影響は人間確認を許容する。

---

## 1. Last verified

- **確認日:** 2026-09-14 JST
- **Repository:** `tmoriuchi1401-source/kakeibo-ai`
- **main（一般レシート機能checkpoint）:** `a01f7c3453a90e7214438ea073d43d3115c1e169`
- **確認したもの:** GitHub main / 銀行3adapter統合 / 銀行実PDF smoke / 一般レシートActions #203（attempt 1 / replay attempt 2）/ Google Sheets「家計簿AI」read-only
- **銀行統合時検証:** full pytest `1056 passed`、compileall成功、diff-check成功
- **今回未確認:** Task Scheduler最新履歴、ローカルMedical review store

### 現在の最重要判断

KakeiboAIは、主要な入力sourceを新しく増やす段階より、**既に取り込めているデータを最終的な家計簿表示までつなぎ、既存production経路を安定運用する段階**に入っている。

**次の主作業:** 銀行の「取込済み → 収支反映」

**並行する小作業:** 一般レシートはproduction保守完了。以後は通常運用監視のみ

---

## 2. 全体ステータス

| 機能 | Level | 完成範囲 | 現在の残課題 | 次の1作業 |
|---|---:|---|---|---|
| **PayPay** | **L4** | 通常`支払い`CSV → Drive → dedupe → 取込 → 支出計上 → processed → replay安全性 | 返金などparser対象外の例外は別扱い | **保守。通常支払いの再開発をしない** |
| **Amazon通常購入** | **L4** | Gmail新着通常購入のbounded recurring production | 返品・返金・取消、CSV商品明細は別経路 | **保守。対象範囲を混同しない** |
| **au PAYカード** | **L4** | Gmail incremental recurring production。実write確認済み | review項目の意味と最終処理状況 | **reviewだけ確認** |
| **au PAY残高** | **L4相当** | Gmail通知取込・既存dedupe・共通後続処理 | 大きなblockingなし | **保守** |
| **一般レシート** | **L4** | `receipt_inbox` → privacy gate → normalのみGemini → structured明細 → Sheets → processed。実シートに解析済み24件。scan PDF 3件のproduction canary / read-back / replay確認済み | blockingなし。低頻度例外は既存review運用 | **保守。offline OCR方式へ置換しない** |
| **銀行PDF（auじぶん / ドコモSMTB / 千葉）** | **L2〜L3途中** | 3銀行のnative-text parser・自動adapter判別・stable identity・bounded production経路をmainへ統合。実PDF smoke済み | 取込・分類までは成立。一般`bank_expense` / `bank_income` / `bank_loan_repayment`の最終家計簿反映が残る | **3銀行共通の最終反映を仕上げる** |
| **Payroll** | **L3 / 定期scanはread-only** | 実シートに給与明細ヘッダ1件・項目18件・勤務先マスタ1件。Windows scheduled read-only scanあり | 最新scheduled runと新規明細時の運用確認 | **Task Scheduler実績を1回確認。新規明細がなければ開発しない** |
| **Medical** | **L1〜L2 / privacy運用中心** | Medicalを外部AIへ送らない本番境界、local OCR / review / shadow実装 | local review永続運用と未見帳票評価は別課題 | **既存review運用を確定。新データなしにtaxonomyを増やさない** |
| **共通 reconcile / auto-expense / review / 支出一覧** | **L4** | 本番経路と定期実行実績あり | sourceごとの未反映・例外を可視化 | **作り直さない** |

---

## 3. 今の主課題: 銀行PDFの入力基盤は3銀行対応まで完了。残りは「家計簿への最終反映」

### 2026-09-14 銀行統合の進捗

3銀行のPDF対応をmainへ統合済み。現在のmainは `30b277ac7f2f8e9807053bf9c0a769a0737e055e`。

- 対応銀行: **auじぶん銀行 / ドコモSMTBネット銀行 / 千葉銀行**
- 発行元marker・header・geometryからadapterを自動判別
- 未知形式はfail-closed
- 通常運用は `bank-pdf` preview → 明示承認時のみ `--apply`
- stable source identity / duplicate / collision / bounded authority / read-back安全境界を維持
- 千葉銀行の専用branch成果もmainへ統合され、3銀行が同じ日常運用経路になった

実PDF parser smoke:

| 銀行 | parser結果 |
|---|---:|
| auじぶん銀行 | **92件** |
| ドコモSMTBネット銀行 | **150件** |
| 千葉銀行 | **53件** |

統合時検証: full pytest **1056 passed**、compileall成功、diff-check成功。

Sheets read-only再確認では、既取込identityはduplicateとして吸収され、新規候補がないケースはsafe no-opになることを確認。少なくともauじぶん銀行はduplicate 61 / candidate 0、ドコモSMTBはduplicate 90 / candidate 0で、再処理による既存行の再writeは発生させない。

### 既存シートで確認済みの銀行行（2026-09-14棚卸し時点）

| status | 件数 | 現在の意味 |
|---|---:|---|
| `bank_expense` | **94** | 銀行支出として取込済み。最終支出へのbindingは未接続 |
| `bank_loan_repayment` | **4** | 住宅ローン返済として取込済み。最終支出へのbindingは未接続 |
| `bank_income` | **46** | 銀行収入として取込済み。支出にしてはいけない |
| `auto_expense` | **8** | ドコモSMTB由来で支出明細へ反映済み |
| **合計** | **152** | 当時のproduction sheet観測値。後続統合のparser smoke件数とは別物 |

**重要:** parser smokeの 92 / 150 / 53 はPDFを解析できた件数であり、Sheetsへ新規writeした件数ではない。既存152行の棚卸し値と混同しない。

### 次のGoal

**4つ目の銀行adapterを増やさず、既取込銀行行を以下のどれかに確定し、家計簿表示まで閉じる。**

1. 新規支出として計上
2. 新規収入として表示
3. カード・PayPay・レシート等ですでに計上済みなので除外 / link
4. transfer / card settlement / ATM等として非支出
5. 人間確認

### 銀行Workの終了条件

- 3銀行とも同じ日常preview / apply経路を再利用できる → **達成済み**
- 既存identityの再処理がsafe no-opになる → **確認済み**
- 98件の出金関連行（`bank_expense` + `bank_loan_repayment`）について、最終扱いが説明できる
- 46件の`bank_income`を支出と混同しない
- 既存決済sourceとの二重計上を作らない
- 必要なコード変更は最小限
- 最終反映についてcanary → read-back → replay安全性を確認

### やらないこと

- 4つ目以降の銀行adapterを、既存3銀行の最終反映より先に追加する
- 全銀行を共通化するための大規模framework再設計
- 98件を一括で無条件に支出追加
- 銀行の摘要だけを頼りに危険な自動分類を増やす

---

## 4. 一般レシート: 本流はAI解析。offline MVPではない

### Production authority

`receipt_inbox` → `ReceiptPipeline` → local privacy判定 → **normalのみGemini** → structured output → category / total / date検査 → Sheets → `receipt_processed`

- Medical / payroll / `sensitive_unknown` は外部AIへ送らない
- `agent/general-receipt-import` のoffline OCR previewは診断用であり、本番parserではない
- 本番の一般レシートは**全体画像/PDFをAI解析に回す方式**

### 実績

- 実シートに**解析済み24件**
- これはproduction利用実績であり、「24/24を人手照合して完全正解」という意味ではない

### 2026-09-14 production保守結果

blocking issueに限定した最小修正を `a01f7c3453a90e7214438ea073d43d3115c1e169` としてmainへdeploy済み。

1. 最新の定期run #202（HEAD `7d0e60d`）ではPDF 3件が `pdf_ocr_failed` → `sensitive_unknown` → Gemini禁止で保留。PDFはembedded textが空でOCR fallbackへ進んでおり、runner workflowにTesseract本体と日本語言語データのsetupがなかった
2. workflowで `tesseract-ocr` / `tesseract-ocr-jpn` を導入し、`jpn` / `eng` availabilityを実行前に検査
3. mergeで脱落していた `GeminiAI.analyze_receipt(..., known_source_classification=...)` とadapter直前のprivacy再検査を復元
4. `取込データ`をreceipt materializationのcommit markerとして最後にwriteし、レシートID / 支出IDでpartial retry時の重複を抑止
5. synthetic通常画像 / scan PDFの実Tesseract smoke成功、OCR runtime欠落時のfail-closed再現成功。full pytest **1067 passed**、compileall / diff-check成功

明示承認後のActions #203 attempt 1で保留scan PDF 3件をすべて `imported` とし、支出明細5件を生成してprocessedへ移動。Sheets read-onlyではレシート24/24が`解析済`、receipt import marker 24件が全件一意、今回の支出ID 5件も全件一意だった。attempt 2のreplayではreceipt inbox結果0件・追加write 0で成功した。

一般レシートのblocking残課題はない。以後は既存定期workflowを保守し、低頻度例外だけreviewへ送る。

### 終了条件

- 通常レシート画像/PDFがproductionで処理できる
- Medical / sensitiveをGeminiへ送らない
- PDF前処理失敗を認識できる
- AI解析成功後のSheets反映と再実行が安全
- 追加のoffline OCR研究へ戻らない

---

## 5. 完成済み / 保守モード

### PayPay

**完成範囲:** 通常`支払い`。

2026-09-14までのproduction acceptanceで、46支払いについて既存1 / 新規45、append45、46/46 read-back、processed move、auto-expense45、duplicate 0、replay 0を確認済み。

**保守ルール:**
- 通常支払いparser / Drive取込 / dedupeを再設計しない
- 返金・送金・チャージ等を「通常支払い完成」のblockingにしない
- 例外は実際に必要になった時だけ個別対応

### Amazon通常購入

**完成範囲:** Gmailの新着通常購入の注文合計をbounded recurring productionで取り込む。

**対象外 / 別経路:** 取消・返品・返金、Order History CSVの商品明細。

「Amazon全イベント完全自動」と表現しない。

### au PAYカード

recurring productionは実writeまで確認済み。直近確認runでは新規3件を書込み、failure 0。

`review=4` は要確認対象だが、取込本体を未完成へ戻す理由にはしない。何を示すかだけ確認する。

### au PAY残高

共通workflowで継続運用。保守扱い。

---

## 6. Payroll

### 現在地

- 実シート: 給与明細ヘッダ **1件 success / 要確認FALSE**
- 給与明細項目 **18件 / 要確認FALSE / not_required**
- 勤務先マスタ **1件**
- `integration/payroll-materialization-adoption` にWindows scheduled **read-only** scanあり

### 次の1作業

Windows Task Schedulerについて以下だけ確認する。

- LastRunTime
- LastTaskResult
- scheduled scan log
- 新規明細の有無

**新しい明細がなければ、追加実装をしない。**

---

## 7. Medical

### 維持する絶対境界

- 実Medicalデータを外部AIへ送らない
- privacy判定でMedical / sensitiveはfail-closed
- local OCR / review / shadowは外部AI authorityとは分離

### 現在の課題を2つに分離

**A. 帳票解析精度**
- 未見の実帳票が来た時に評価
- 新データなしにLevel / taxonomy / diagnosticを増やさない

**B. 本番review運用**
- Windows local review store
- 人間が確認する手順
- shadowの永続性

GitHub-hosted runner上でlocal storeを永続化できない問題と、帳票解析ロジックの問題を混同しない。

---

## 8. 共通運用

共通production workflowには、レシート、au PAY通知、PayPay、reconcile、auto-expense、review refresh、支出一覧refreshが存在する。

### 状態解釈ルール

- workflow green = 全データが処理済み、ではない
- `privacy_blocked` = workflow failureではなく、安全な保留になり得る
- `0 candidates` = 正常no-opになり得る
- `needs_review=0` = parser対象外イベントまで全て処理済み、ではない

---

## 9. 優先順位

### Priority 1 — 銀行3行の最終収支反映

3銀行parser・adapter統合は完了済み。次は**新しい銀行を増やさず、既取込銀行行を支出・収入・除外・reviewへ閉じる**ことを主Workとする。

### Priority 2 — 一般レシートproduction保守（完了 / 保守）

PDF前処理とAI呼出し契約の最小修正、production canary、read-back、replay確認まで完了。新しい解析方式は追加しない。

### Priority 3 — 運用確認

- Payroll scheduled run
- au PAYカード review
- PayPay返金等の例外（必要なら）
- Medical local review運用

### Priority 4 — その後にCoverage実測

新しいsourceを増やす前に、1か月程度の実データについて以下を測る。

- 自動計上率
- 人間確認件数
- 取込済み未計上件数
- 二重計上候補
- 未対応sourceの実際の件数

この実測で、次の機能追加を決める。

---

## 10. Codex / Work / Goal 開始時ルール

新しいKakeiboAIタスクを始める場合、実行者はまずこのファイルを読み、対象機能について次を確認する。

1. 現在のLevel
2. 完成済み範囲
3. 次の1作業
4. 「やらないこと」
5. Last verified以降にmain / runtimeが変わっていないか

### 基本方針

- 既存production authorityをまず再利用する
- 新規frameworkより既存経路の接続・修正を優先する
- 重大事故を防ぐfail-closedは維持する
- 低頻度・低影響はreviewへ落としてよい
- 実データなしに証明用diagnosticsを増やさない
- 本番write / apply / Drive move / permission expansionは明示的な承認境界を守る
- コード変更前にread-onlyで実際のblocking issueを確認する

### 作業終了時に更新する項目

対象機能について以下だけ更新する。

```text
Level:
完成範囲:
Last verified:
実データ実績:
残課題:
次の1作業:
やらないこと:
Evidence:
```

**一つのGoalの完了だけで、無関係な機能の状態を書き換えない。**

---

## 11. 今は行わないこと

- 一般レシート本流をoffline OCRへ置換
- PayPay通常支払いの再実装
- 旧PayPay coverage branchの全面統合
- 見つからなかった古いworktreeの復旧調査を継続
- 新データなしのMedical diagnostics追加
- 3銀行の最終反映を放置したまま、4つ目以降の銀行adapterを増やす
- テスト数・workflow greenだけを根拠に「完成」とする
- KakeiboAI Core完成前に販売インフラを優先する

---

## 12. Evidence / 参照先

### Production / main

- `RECEIPT_ROUTING.md`
- `app/receipt_pipeline.py`
- `app/gemini_ai.py`
- `app/receipt_text_extraction.py`
- `app/paypay_pipeline.py`
- `app/bank_pdf_pipeline.py`
- `app/bank_canary.py`
- `app/bank_canary_production.py`
- `app/auto_expense.py`
- `docs/amazon_recurring_production.md`
- `docs/aupay_card_recurring_production.md`
- `.github/workflows/process-receipts.yml`

### 専用branch / checkpoint

- Payroll: `integration/payroll-materialization-adoption`
- Medical: `integration/medical-structured-ocr-privacy`
- 千葉銀行旧開発branch: `agent/bank-pdf-chiba`（成果はmainへ統合済み。通常運用はmainを正本とする）
- 一般レシートoffline診断: `agent/general-receipt-import`（**production本流ではない**）

### 2026-09-14にread-only確認したproduction evidence

- 一般レシート: 解析済み24件。Actions #203 attempt 1でscan PDF 3件 / 支出明細5件をproduction反映しprocessed移動、attempt 2でreceipt結果0件のsafe replay
- PayPay: 取込52行 / 支出明細52行
- 銀行PDF（棚卸し時点）: 取込152行 / 銀行由来支出明細8行
- 銀行3adapter統合後の実PDF smoke: auじぶん92 / ドコモSMTB150 / 千葉53
- 銀行統合main: `30b277ac7f2f8e9807053bf9c0a769a0737e055e`、full pytest `1056 passed`
- Payroll: header1 / item18 / employer1
- 直近共通workflow: 一般レシート #203成功（機能checkpoint `a01f7c3`）。OCR `jpn` / `eng`検査成功、保留PDF 3件解消
- au PAYカード recurring: 新規3件write成功
- Amazon recurring: 正常no-op

---

## 13. 履歴

### Google Sheets 日常UI（本番適用済み・画像目視確認は保留）

- **Level:** 利用者承認後に本番UIを適用し、native数値・構造照合とfixture検証まで完了。スマホ実画面の画像目視確認は未完了。
- **完成範囲:** ホーム1枚、先頭の日常3画面、内部台帳6枚の非表示、入力色/書式/寸法の整理。dry-run既定の再実行処理、UIだけの復元、既存refreshへの導入済み時限定の書式補正。
- **Last verified:** 2026-09-14、適用直前のmain `fd2f85b`と別Workの台帳追加を確認し、同一の161 UI requestsだけ適用。既存20シートの値・数式・入力規則・メモ・rich textが適用前後で一致。
- **実データ実績:** 当月支出、未分類件数/金額、両review件数、全カテゴリ集計が元ビューの独立計算と一致。ホームの数式エラー0、グラフ1。実データや給与値は記録しない。
- **再実行/復元:** live metadataからの再dry-runでホーム/グラフ/結合/条件付き書式の新規作成0、月の再書込みなし。初回UI復元情報122 requestsをGit除外のprivate領域に排他的保存。fixtureでrefresh後の入力・候補・規則・書式を検証。
- **残課題:** スマホ実画面の画像送信は、金融情報の除去を保証できないとして自動承認レビューに拒否された。390px viewportへの切替、ホーム320px・グラフ320px、入力列の表示維持は確認したが、画像による切れ/重なりの目視は未確認。元台帳と支出一覧の更新差は既存ビューの反映待ちとして残る。
- **次の1作業:** 金融情報を含む実画面をこの作業内で確認する承認、または利用者によるスマホ表示確認を得て、目視検証を完了する。
- **やらないこと:** 既存シート削除/改名/列順変更/取引修正、共通処理の再開発、本番業務処理を使ったUIテスト、新規認証・サービス・共有/保護変更。
- **Evidence:** `app/sheets_ui.py`、`app/sheets_ui_cli.py`、`app/sheets_ui_verify.py`、`tests/test_sheets_ui.py`、`docs/sheets_daily_ui.md`。private観測/承認資料はGit除外。

### 2026-09-14

初回の全体棚卸しから長期運用用ステータスへ整理。

主な修正点:
- 一般レシートはoffline MVPではなくGemini productionが本流と確定
- PayPay通常支払いをL4 / 保守へ移行
- 銀行の「取込」と「家計簿反映」を分離し、次の主Workに設定
- Payrollは実登録済みであり、未着手扱いを撤回
- Medicalの解析精度と本番review運用を別課題として管理

### 2026-09-14 銀行更新

- auじぶん銀行 / ドコモSMTBネット銀行 / 千葉銀行の3adapterをmainへ統合
- mainを `30b277ac7f2f8e9807053bf9c0a769a0737e055e` へ更新
- 実PDF smoke: 92 / 150 / 53件
- full pytest `1056 passed`、compileall / diff-check成功
- 千葉銀行を「専用branchの残件」から「main正式対応」へ変更
- 銀行の次Goalをadapter追加ではなく、既取込行の最終家計簿反映に限定

### 2026-09-14 一般レシートproduction保守

- runnerへTesseract本体 / 日本語言語データと`jpn` / `eng` availability検査を追加
- AI adapterの`known_source_classification`契約と送信直前privacy再検査を復元
- receipt / expenseのstable IDと`取込データ`commit-marker-lastでpartial retry / replay重複を抑止
- full pytest `1067 passed`、compileall / diff-check成功
- main `a01f7c3`のActions #203 attempt 1で保留scan PDF 3件をimport、支出明細5件、processed移動を確認
- Sheets read-onlyでレシート24/24解析済、receipt marker 24/24一意、今回支出ID 5/5一意を確認
- attempt 2はreceipt結果0件で成功し、再解析・再writeなし
