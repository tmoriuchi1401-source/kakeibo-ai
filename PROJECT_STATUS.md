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
- **main:** `a01f7c3453a90e7214438ea073d43d3115c1e169`
- **確認したもの:** GitHub main / 銀行3adapter統合 / 銀行実PDF smoke / 取込済み銀行行185件の最終反映preview / 銀行住宅ローン1件のproduction canary / 3銀行10件のproduction bounded batch / 支出backfill追加25件のwrite・read-back・replay / 直近Actions / Google Sheets「家計簿AI」
- **銀行最終反映checkpoint:** `agent/bank-final-reflection`。full pytest `1084 passed`、compileall成功、diff-check成功。合計36件の本番支出反映済み。87件backfillは25件成功後、追加実行の承認境界で停止
- **今回未確認:** Windowsの現在worktree、未コミット差分、Task Scheduler最新履歴、ローカルMedical review store

### 現在の最重要判断

KakeiboAIは、主要な入力sourceを新しく増やす段階より、**既に取り込めているデータを最終的な家計簿表示までつなぎ、既存production経路を安定運用する段階**に入っている。

**次の主作業:** 銀行の「取込済み → 収支反映」

**並行する小作業:** 一般レシートproductionのPDF前処理 / AI呼出し契約の保守

---

## 2. 全体ステータス

| 機能 | Level | 完成範囲 | 現在の残課題 | 次の1作業 |
|---|---:|---|---|---|
| **PayPay** | **L4** | 通常`支払い`CSV → Drive → dedupe → 取込 → 支出計上 → processed → replay安全性 | 返金などparser対象外の例外は別扱い | **保守。通常支払いの再開発をしない** |
| **Amazon通常購入** | **L4** | Gmail新着通常購入のbounded recurring production | 返品・返金・取消、CSV商品明細は別経路 | **保守。対象範囲を混同しない** |
| **au PAYカード** | **L4** | Gmail incremental recurring production。実write確認済み | review項目の意味と最終処理状況 | **reviewだけ確認** |
| **au PAY残高** | **L4相当** | Gmail通知取込・既存dedupe・共通後続処理 | 大きなblockingなし | **保守** |
| **一般レシート** | **L4相当 / 保守課題あり** | `receipt_inbox` → privacy gate → normalのみGemini → structured明細 → Sheets → processed。実シートに解析済み21件 | 直近PDF 2件がAI前の`pdf_ocr_failed`で保留。`analyze` CLIと`GeminiAI.analyze_receipt`の引数契約不整合 | **production前処理とAI接続を最小修正** |
| **銀行PDF（auじぶん / ドコモSMTB / 千葉）** | **L3 backfill部分反映済み** | 住宅ローンcanary 1件、3銀行代表10件、追加backfill 25件の合計36件で本番write / 各件read-back / replayを確認 | 残り93件（支出62 / non-expense 12 / review 19）。支出backfillは追加実行の承認境界で停止 | **追加承認なしでは残り62支出へ触れない** |
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

### 既存シートで確認済みの銀行行（2026-09-14最終反映preview時点）

| status | 件数 | 現在の意味 |
|---|---:|---|
| `bank_expense` | **93** | 銀行支出として取込済み。残り62件はwrite-eligible、12件はnon-expense、19件はreview |
| `bank_loan_repayment` | **0** | 住宅ローン返済4件はすべて支出反映済み |
| `bank_income` | **48** | 銀行収入として取込済み。支出にしないことをpreviewで確認 |
| `auto_expense` | **44** | 既反映8件と、銀行最終反映経路で追加した36件 |
| **合計** | **185** | 現在のproduction sheet観測値。parser smoke件数とは別物 |

**重要:** parser smokeの 92 / 150 / 53 はPDFを解析できた件数であり、Sheetsへ新規writeした件数ではない。既存185行の棚卸し値と混同しない。

### 取込済み銀行行の最終反映preview

`agent/bank-final-reflection` の共通read-only previewで、現在の185行を次のとおり分類した。

| 最終分類 | 件数 | 扱い |
|---|---:|---|
| 新規支出 | **62** | 残りの`bank_expense` 62件。stable `M-...` IDで支出明細へupsert予定 |
| 新規収入 | **48** | `bank_income`を維持し、支出明細へ書かない |
| excluded / link | **0** | 現在の実データに一意な既存支出link候補なし |
| non-expense | **12** | exactなcard settlement。支出を作らない |
| review | **19** | 所有・用途が曖昧な金融機関相手の振替。自動支出化しない |
| duplicate | **44** | 既存`auto_expense`と本番反映済み36件のstable targetを確認。再追加しない |

production入口は通常workflowへ未接続。applyはexact 1 identity、承認済みSpreadsheet ID、expected Git HEADの完全一致を要求し、`取込データ`と`支出明細`をread-backする。2026-09-14に住宅ローンcanary 1件、auじぶん4件 / ドコモSMTB 3件 / 千葉3件のbounded batch 10件、追加backfill 25件を同じ1件単位経路で反映した。追加25件は全件read-back一致、replayはduplicate 25 / 予定write 0件。87件backfillの第6組開始前に追加実行が承認境界で拒否されたため、残り62件へ触れず停止した。

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
- 129件の未反映出金行（`bank_expense` 125 + `bank_loan_repayment` 4）を、支出98 / non-expense 12 / review 19に分離し、支出36件を反映済み。残りは支出62 / non-expense 12 / review 19の93件
- 48件の`bank_income`を支出と混同しない → **preview確認済み**
- 既存決済sourceとの二重計上を作らない → exact link候補0、card settlement 12を非支出、追加backfill後は既反映44をduplicateとして確認
- 必要なコード変更は最小限
- 最終反映についてcanary → 3銀行bounded batch → 追加25件backfill → 各件read-back → replay安全性を確認 → **36件で確認済み**

### やらないこと

- 4つ目以降の銀行adapterを、既存3銀行の最終反映より先に追加する
- 全銀行を共通化するための大規模framework再設計
- 残り62件を一括で無条件に支出追加
- 銀行の摘要だけを頼りに危険な自動分類を増やす

---

## 4. 一般レシート: 本流はAI解析。offline MVPではない

### Production authority

`receipt_inbox` → `ReceiptPipeline` → local privacy判定 → **normalのみGemini** → structured output → category / total / date検査 → Sheets → `receipt_processed`

- Medical / payroll / `sensitive_unknown` は外部AIへ送らない
- `agent/general-receipt-import` のoffline OCR previewは診断用であり、本番parserではない
- 本番の一般レシートは**全体画像/PDFをAI解析に回す方式**

### 実績

- 実シートに**解析済み21件**
- これはproduction利用実績であり、「21/21を人手照合して完全正解」という意味ではない

### 現在の保守課題

2026-09-14の保守worktreeで、以下の最小修正を準備済み。**production canaryは未実施。**

1. 最新の定期run #202（HEAD `7d0e60d`）ではPDF 3件が `pdf_ocr_failed` → `sensitive_unknown` → Gemini禁止で保留。PDFはembedded textが空でOCR fallbackへ進んでおり、runner workflowにTesseract本体と日本語言語データのsetupがなかった
2. workflowで `tesseract-ocr` / `tesseract-ocr-jpn` を導入し、`jpn` / `eng` availabilityを実行前に検査する修正を準備
3. mergeで脱落していた `GeminiAI.analyze_receipt(..., known_source_classification=...)` とadapter直前のprivacy再検査を復元
4. `取込データ`をreceipt materializationのcommit markerとして最後にwriteし、レシートID / 支出IDでpartial retry時の重複を抑止
5. synthetic通常画像 / scan PDFの実Tesseract smoke成功、OCR runtime欠落時のfail-closed再現成功。full pytest **1067 passed**、compileall / diff-check成功

残課題は、明示承認後に通常PDF canaryを行い、read-back / Drive move / replayをproductionで確認すること。

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

### Priority 2 — 一般レシートproduction保守

PDF前処理とAI呼出し契約だけを最小修正する。

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

- 一般レシート: 解析済み21件
- PayPay: 取込52行 / 支出明細52行
- 銀行PDF（追加backfill 25件後の最終反映preview）: 取込185行 / 新規支出62 / 収入48 / non-expense 12 / review 19 / duplicate 44 / link 0。予定支出write 62 / 予定取込更新93
- 銀行3adapter統合後の実PDF smoke: auじぶん92 / ドコモSMTB150 / 千葉53
- 銀行統合main: `30b277ac7f2f8e9807053bf9c0a769a0737e055e`、full pytest `1056 passed`
- Payroll: header1 / item18 / employer1
- 直近共通workflow: PDF前処理保留2件、reconcile updated0、auto-expense candidates0
- au PAYカード recurring: 新規3件write成功
- Amazon recurring: 正常no-op

---

## 13. 履歴

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

### 2026-09-14 銀行最終反映preview

- 取込済み銀行行185件を3銀行共通のread-only経路で最終分類
- 新規支出98 / 収入48 / non-expense 12 / review 19 / duplicate 8 / link 0
- incomeを支出化せず、card settlementと曖昧な金融相手行を自動支出から除外
- stable expense ID、read-back、部分失敗後のreplay修復を実装
- production applyはexact 1 identity / approved target / expected HEADへ限定
- 住宅ローン1件のcanaryを承認済みproduction targetへ反映
- `取込データ`は`auto_expense` / target `M-619abd838394af6ab069af7b`、`支出明細`は103,950円 / 住まい / 住宅ローン / activeとしてread-back成功
- 同一identityのreplay previewはduplicate `already_finalized`、予定write 0件。全体残件は支出97 / 収入48 / non-expense 12 / review 19 / duplicate 9
- 明示承認後、auじぶん4件 / ドコモSMTB 3件 / 千葉3件のwrite-eligible支出だけを、既存の1 identity単位production経路でbounded batch反映
- 全10件で支出1件作成 / 取込1件更新 / read-back一致。needs_review / withheld / ambiguous / 既存duplicateは選択・変更していない
- 同一10 identitiesのreplay previewはduplicate `already_finalized` 10件、予定支出write 0 / 予定取込更新0
- `支出明細`は10行増加し、`取込データ`は対象10行だけstable target付き`auto_expense`へ更新。API再読込とGoogle Sheets表示で既存レイアウト破損なし
- 全体残件は支出87 / non-expense 12 / review 19の118件。追加反映はこのbounded batch承認に含めず、別途明示承認を必要とする
- 続く87件backfillでは、直前previewの87件一致後、各identityを直前preview → 1件apply → read-backの順で処理
- 追加25件（auじぶん14 / ドコモSMTB11）は全件支出作成・取込更新・read-back一致。同一25 identitiesのreplayはduplicate `already_finalized` 25件、予定write 0
- 第6組の開始前に追加production writeが承認境界で拒否されたため停止。第6組は未開始で、残り62件には触れていない
- 停止後previewは支出62 / 収入48 / non-expense 12 / review 19 / duplicate 44。保護対象の件数は不変
