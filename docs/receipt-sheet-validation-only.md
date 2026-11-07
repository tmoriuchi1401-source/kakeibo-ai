# Sheets一般レシート検証専用経路（完全合成・本番未接続）

2026-11-07。今回の上限は **検証済み・未記帳**。このブランチはmainから独立し、#195〜#199をmergeせず最小の純粋コードだけを再利用する。実Google Sheets/iPhoneで動作する完成版ではない。Cloud Run `hga-auth-synthetic` の手動0を変更・照会しない。実機の操作性は未検証。

## 実装と再利用

| 部分 | 実装／根拠 | 今回の境界 |
|---|---|---|
| 商品修正・分類・税値引き・金額検証 | #196 `93e0fe7330e4d4aaa27144f92148f2503f159127` の `receipt_item_correction_plan.py` と `receipt_item_correction_review.py` をそのまま再利用 | 新経路は `review` と純粋 `registration_plan` のみ呼ぶ。旧writerの合成ハーネスは取り込まない |
| 原本・候補・private anchor独立読取 | #199 `1841438cc2ec17cc807b089f200576098fb52395` のreader照合を移植 | OIDC/Flask/Firestore/AuthRequestStoreをimportしない。ACL、原本bytes hash/version、候補/取込digest、前後読取を維持し、policy digestと下書き内部identityも照合 |
| UUID受付 | category-submit/manual-entryの先行保存→UUIDだけ送信→UNKNOWN時非再送パターン | `Code.gs`は依存注入する合成core。installer、trigger、PAT保管、UrlFetchApp、ライブSheet書込みなし |
| 結果journal | #199の専用scope・CAS/read-back・非TTL・状態遷移の考え方 | actor認証を流用しない専用schema。強ETag、server-side CAS、毎回のprivate ACL preflight、読戻しが必須。既存金融stateやhidden Sheetに接続しない |
| 同案件結果投影 | #199のcase/source/UUID/snapshot照合を移植 | 純粋関数が結果欄だけ変更したコピーを返す。原本リンク・入力・メモを保持。ライブ更新は未実装 |
| Actions | 既存 `synthetic-tests.yml` にNodeテスト1 step追加 | 既存のSecretsなし合成CIを再利用。Pythonは既存pytest discoveryで実行。workflow_dispatch/schedule/Secrets/Variables/環境承認/本番資格を追加せず、本番workflowは一切変更しない |

対象は#196が扱う**一般の買物画像・金額不一致で保留された案件**。修正後の合計が一致しても、カテゴリ競合、数量の行合計確認、税/値引きの帰属、候補と原本の整合を検証する。一般PDFや元から一致した別種類の案件への拡張は今回行わない。医療・給与・unknownは一般処理を拒否する。AI送信・原本移動なし。

## 合成「確認」UI差分（未展開）

既存「確認」1タブ、1行1案件、案件ID/見出し固定、原本リンク・既存入力・メモは維持する。以下は**新しい実ブックを作らないローカルの論理モデル**であり、本番や承認済み合成ブックの列追加・再生成をしていない。

| 追加を試作した欄 | 意味／既存UIへの影響 |
|---|---|
| 商品修正下書き | 同じ行の1セルに改行形式で必要な商品変更だけ保存。案件ID、原本ID/hash/version、基準digestの3行を同じセル内に持たせ、列だけのsortで別案件へ移った入力を拒否する |
| 修正内容を登録する | 1回の明示チェック操作を模したevent。普通のセル編集ではdispatchしない。予約済み/不明要求の再チェックは拒否 |
| 登録要求 | UUIDとsnapshot digestを同案件に結ぶ内部論理field。下書きと別。現行の具体的な列対応は未確定 |
| 検証結果 | 受付済み／検証中／修正が必要／保留／検証済み・未記帳／結果不明・照合待ち。後から入力が変わった場合は「未登録の変更あり」 |

商品操作行は `操作｜明細ID｜商品名｜数量｜行金額｜種類｜大カテゴリ｜小カテゴリ｜帰属ID｜原本根拠` の10項目。変更時の空欄は元値維持、除外時はIDと根拠だけ、追加時は新ID・名称・数量・金額・種類・カテゴリを必須とする。種類は `product/tax/discount`、数量は正数で小数3桁まで、金額は整数円の**行合計**（数量を再乗算しない）。区切り文字/改行を値に含める入力は未対応で拒否する。商品名等に数式風文字列があってもAPIへの書込みは存在しない。将来保存する場合はRAWが必要。

これは合成入力形式の試作であり、iPhoneでの長いID・区切り文字の編集負担を解消したUIではない。フォームやサイドバーへ勝手に切り替えない。UI展開前に実際の追加列・操作負担・既存入力保持をレビューし、明示承認と実機評価を必要とする。

## 想定するiPhone操作（将来接続後）

1. 「確認」で案件検索→既存原本リンクから画像を確認し同じ案件へ戻る。
2. 必要な商品行だけ変更・追加・除外・数量/カテゴリ/税/値引きを修正し下書き保存。総額と明細合計・差額は別の値として確認する。原因不明なら保留する。
3. 「修正内容を登録する」を1回チェック。UUID付き固定要求をprivateへ先行保存し、GitHubにはUUID以外を送らない。Actionsの待ち時間は保証しない。
4. 同じ案件で結果確認。「結果不明・照合待ち」は再チェックせず独立照合を待つ。最終状態は「検証済み・未記帳」。後日のwrite flag変更でもこの要求は記帳されない。

この手順は実iPhoneで完走した記録ではない。ローカルのNode capture→Python reader/validator→結果投影の合成テストが成立した範囲のみ。

## 信頼境界・失敗時の扱い

- Sheet、下書き、event.user、trigger実行者、PAT owner、GitHub actorは本人承認の証拠ではない。新purposeは検証専用で、actor/authorityを作成しない。
- 商品の原本根拠は入力者の説明であり、画像にその商品が印刷されていることの独立した人間認証ではない。原本bytes/hash/version・保管済み候補・private分類の整合は独立検証するが、新しいOCR/AIや本人認証を追加しない。既存総額確認はprivate anchorからだけ読む。Sheet編集から「本人確認済み」を作らない。
- private anchorのdigestは秘密の署名や本人証明ではない。誤結合・古い候補/ルール・総額確認の版を検出するcontext token。Sheets内のtokenを信用せずprivate原本から再構築して比較する。
- 要求には用途、案件、原本identity、操作、context digest、snapshot digestを固定。要求private readerは不変の要求だけを返す必要がある。readerは毎回独立読取し、二回のreview snapshotを比較する。後の編集は登録対象へ自動追従しない。
- CAS journal内でUUIDとcase/sourceを同時に検査・予約。同一UUIDと同一snapshot別UUIDを拒否。進行中/UNKNOWNの同案件・原本は別UUIDでも拒否。同原本の別案件への転用も拒否。完了済みの修正必要案件は、新しい下書きの別UUIDによる再検証だけを許す。
- CAS失敗、書込み応答喪失、読戻し失敗ではUNKNOWNを返し、自動再送・無条件上書き・rollback・回復writeを行わない。途中runが独立にcompletedと確認された場合だけ、read-only queryで受付済み/検証中をUNKNOWNとして投影できる。時間経過だけで処理を再開しない。受理CASに到達する前の要求はworker完了を推測しない。送信側の不明予約を解除せず、運用照合が必要。
- 結果journalの最終状態は不変。writer、authority factory、posting flag読取、銀行/カード/Amazon/PayPay処理への呼出し口なし。
- 並べ替えは案件IDで再探索し、結果投影時はcase/source/UUID/snapshotを比較。後の入力・メモ・リンクを保持する。**通常のSheets values.batchUpdateは行identityの原子的CASではないためライブadapterを提供しない**。

## 合成検証

ローカルWindows Python 3.14.7 / Nodeの実行。ネットワーク接続と既存金融SheetsDBの構築をテストでtrapし、新経路からの呼出し0を確認。全データ・ID・原本bytes・owner/serviceメールは合成。

```powershell
python -m pytest -q tests/test_receipt_sheet_validation.py tests/test_receipt_item_correction_plan.py tests/test_receipt_review_snapshot.py tests/test_category_rules.py tests/test_receipt_confirmation_ui.py tests/test_receipt_source_identity.py
node --test apps-script/receipt-validation/Code.test.cjs apps-script/category-submit/Code.test.cjs apps-script/manual-entry/Code.test.cjs
```

- Python **109成功・0失敗**（新経路50＋#196再利用12＋既存回帰47）。JavaScript **15成功・0失敗**（新8＋既存7）。初回の58件成功後、UNKNOWN/投影/医療の4件を加えて関連回帰まで実行した最終結果。
- 1,368−1,230=138、967−939=28、修正後一致、追加/除外/数量/名称/カテゴリ、内税二重加算・値引き符号・税配賦拒否、商品ルール競合、架空調整拒否。
- 原本hash/version、解析候補/取込/ルール改変、総額確認欠落、medical拒否、行sort/列sort/同時編集/欠落/重複case、同UUID/別UUID重複、競合worker。
- 受付前/後の通信断、終端保存後応答喪失、強CAS不足、終端CAS競合、読戻し失敗、UNKNOWNのread-only照合、後のwrite flag有効化でも再実行・記帳なし。
- JS実captureの合成requestをPythonへ渡し、同じdraftのsnapshot/hashが一致し、独立検証まで動くことを3ケース確認。
- CIは既存のSecretsなし合成テストを再利用。CIとローカル結果、実接続・iPhone結果を区別する。

初回head `da4866ad471e4638f9653df2f278770a2c854b47` の[専用CI](https://github.com/tmoriuchi1401-source/kakeibo-ai/actions/runs/42366817614)は成功（Python62＋JavaScript15）。同headの既存全体CIは、`test_gemini_error_compat.py`の`import httpx`で両matrixが各1収集エラー。最新SDKの推移で直接依存の不足が表面化しており、#196の`requirements-test.txt`の2行だけも再利用して修正する。本番requirementsや既存テストの期待値・安全条件は変更しない。再実行の結果はPR本文へ記録する。

中間head `60b6520` の全体CIは **4917成功・1失敗**（一般4201成功/1失敗、機微合成716成功）。失敗は新設合成workflowを既存production workflow監査が拒否したもの。新workflowを削除し、既存合成CIへNodeテスト1 stepだけを追加して解消する。監査テストのallowlistや期待値は緩めない。専用CIの成功は履歴として残し、最終headは既存全体CIで検証する。

## 本番接続の停止点と次の最小作業

1. **最初にコードレビュー**：上記UI差分と入力方式、検証専用scope、要求予約/結果投影のCAS契約を査読する。この段階で実資源作成は不要。
2. 既存の許可済みprivate領域・ACL・具体的な列対応・実行資格・不変要求保存・強CASが使えるかを限定読取で確定する。今回その実資源や資格を調査・作成しない。#199の旧host/private4ファイル案を既定にしない。
3. 要求を不変に保存し、同案件へUUIDを原子的に結ぶ `reserve` と、結果だけを原子的identity照合して返すadapterを証明できるまで接続しない。Sheets座標のread→writeだけでは保証できない。保証できなければ停止を維持し、その操作だけを個別判断する。
4. 証明後に、**別途承認された対象だけ**へ列/trigger/資格/UUID受付workflow/結果連携を最小接続し、金融writeなしでiPhone実機を確認する。今回新trigger・PAT・Drive領域・本番workflow dispatchは0。新権限が必要なら直前で停止。実記帳は本PR対象外で別の権限証明・承認が必要。

特に、通常のcheckbox編集eventはrow全体の「タップ時snapshot」を含まない。handler開始前にsortされた座標から、本人が意図した案件を推測してはならない。合成portの `readCaseAtEvent/reserve` は観測した案件への原子的結合をモデル化しているだけで、実Apps Scriptでタップ意図まで証明したものではない。これもライブ接続前の未解決条件として保持する。

## 変更0の証拠と限界

新経路の実装には金融writer引数・呼出し、Cloud API、OAuth/Flask/Firestore依存、ライブAPI client生成、Secrets読取りがない。新テストではsocket接続と金融SheetsDB構築を禁止し、結果の `posting_authority=False / financial_write_count=0` を検査する。合成private journalへの状態writeは行うが、金融・本番writeではない。

この作業で行った外部操作は公開ソース/PRの読取、独立ブランチ/commit/Draft PR保存とSecretsなし合成CIだけ。本番/承認済み合成Sheets、Drive原本/台帳、IAM、Secrets、Variables、validated SHA、本番workflow、main、#195〜#199、旧Cloud Runには変更操作を行わない。これはこの作業の非変更証拠であり、他の実行主体や既存writerの停止・Cloud Runの現在の管理状態を監査したという意味ではない。
