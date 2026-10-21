# 本番定期処理：validated SHA不一致と最小再開計画

2026-10-21 UTC。基点は#151固定head `fea7b91aa9ca681661914361af5a7e03fc8addad`。本書はread-only監査と資格情報なし検証・計画のみ。本番設定変更、dispatch/re-run、記帳、AI送信、原本移動、authority操作、pending解除、main/既存PR更新は行わない。

## 判定

**SHA差分に起因するコード変更の阻害は見つからない。旧validated→mainは文書のみ。ただし従来定期処理の再開は現時点NO-GO（本番状態・競合の確認待ち）。新R1本番有効化も独立にNO-GO。** コード一致の証明と、稼働データ/資格情報/排他成立の証明を区別する。R2-4やR1のmain統合・大規模再設計を、従来処理の復旧条件に追加しない。

最短の候補は、既存状態・排他・関連flagの確認後に**validated変数だけを、検証したその時点のmainへ更新**する案。いまは実行しない。3flagがtrueならSHA更新それ自体で次のscheduleがapply可能になる。安全な準備操作とは扱わない。

## 1. 観測と原因の確定範囲

| 項目 | 証拠と結果 |
|---|---|
| 調査時main | GitHub GETで `d62e1e254692e676f4e4a72dc593524a80fbea08` を再確認 |
| production/schedule/legacy flag | ユーザー確認時点で各 `true`。今回の既存公開Variables GETは401。新ログイン/権限追加なし |
| validated | ユーザー確認時点 `fffbe7568db3acd647e9a088280bd4fcbed6c5c9`。現在値として独立再取得できず |
| 最新scheduled | [39597785298](https://github.com/tmoriuchi1401-source/kakeibo-ai/actions/runs/39597785298)、2026-10-21T12:12:40Z、head d62、skipped |
| 最新job | 119214386038 production、skipped、stepなし。checkout/runner/writerは開始していない |
| 最後のsuccess | [38730949804](https://github.com/tmoriuchi1401-source/kakeibo-ai/actions/runs/38730949804)、2026-10-15T11:56:36Z、schedule、head fff |
| success job | 116408287057。checkout/main guard/source execution step成功。金融行数・全native完了を意味しない |
| 実checkout SHA | 最新skipはcheckoutなし。旧successはguard成功からfffとの一致を推論できるが、独立checkoutログは未取得 |
| 実行中 | 観測時点in_progress/queued/waiting/pending/requested各0。native pending 0ではない |
| workflow登録 | 公開GET200。production active。旧receipt/bank/aupay/Amazon等のdisabled状態とactive canaryはevidence参照 |

[現在メタデータ](production-sha-evidence/current-metadata.json)、[公開設定アクセスとworkflow状態](production-sha-evidence/public-settings-access.json)。last successは全event success直近100件（最古2026-10-11）とschedule successを照合した。最新より新しいproduction successはその範囲にない。runログ/入力/artifact本文は読んでいない。job timestampからcron scopeを逆算しない。

main workflowのjob-ifはrepository/main、production=true、legacy_disabled=true、scheduleならschedule_enabled=true、`github.sha == vars.KAKEIBO_VALIDATED_MAIN_SHA` をすべて要求する。提供された値とd62の組合せでは、他がtrueでもSHA比較がfalseとなり**job開始前skip**になる。これはソース条件とユーザー提供値による再現結果で、run時点のVariablesをログで確認した結果ではない。過去の変数更新履歴は不明で、過去すべてのskip原因を同一とは断定しない。

checkoutは`ref: main`。job gate通過後にもHEAD==validated、main==GITHUB_SHAをguardする。mainが待機中に動けばfail-closed。旧runをre-runしてもevent SHAと現在のmain checkoutを一致させられる保証はない。manual dispatchもSHA gateを迂回しない。

## 2. 全Git差分とA〜D分類

旧fffはd62の祖先。到達commit数4（実変更2、merge2）、first-parent2、変更ファイル2、追加169行/削除0。旧571tracked entryの全mode/type/blobはmainと一致し、文書2件だけ追加されている。app250、workflow24、tests216、Apps Script7はすべて一致。requirements/依存定義・lock等も全tree比較に含む。[Git比較・重点blob証拠](production-sha-evidence/git-comparison.json)。

| commit | 意図と差分 | 分類 |
|---|---|---|
| b1769752694ab49cdb9251e5c98093bf03f8d640 | 採用済iPhone Sheets UIと作業境界をAGENTS/継続仕様へ保存 | A |
| 6b1bfe4f26fd8a7ad83a32f4b4b3d810bcb4883c | #125 merge、first-parent差分は同じ文書2件 | A |
| 3cc93645ccb357d76093c5fc7bbb603fb74919c0 | 1タブ採用、入力保持、未完の本人確定/ソート安全/本番承認を明文化 | A |
| d62e1e254692e676f4e4a72dc593524a80fbea08 | #126 merge、first-parent差分は同じ文書2件 | A |

AGENTS.mdは23行追加、継続仕様は146行追加（端点比較）。mergeを別の金融変更として二重計上しない。runtimeによる両Markdownの読込みがないこともソース検索と全回帰で確認する。A=本番処理への差分なし。**B/C/Dに該当する変更は0**。既存実装のリスクや未確認本番状態を「C変更」と混同しない。テスト成功を理由にBとした項目はない。

重点対象：kakeibo-production、production_flow/run/source、CLI、receipt/privacy/duplicate、bank/PayPay/Amazon、支出/収入writer、native/pending/retry、Google clients/Sheets/Drive、category/manual/daily/ledger sort、Secrets/Variables参照・schedule/manual分岐は全てblob一致。mainと#151では隔離実装等の追加があるため、#151側のアプリ試験をmainの証拠に流用しない。

## 3. 実writerまでの再開影響

以下は**main d62（旧fffと同一）**の到達経路と論理保存先。実file ID/Sheet共有ACL/credential本文/行本文は読んでいない。SheetsはSPREADSHEET_ID、永続stateは既存の暗号化ID変数によるDrive JSON。新DBは不要。

| 処理 | cron UTC → JST / scope | 既存到達経路・保存先・再開前確認 |
|---|---|---|
| 通常receipt | `31 */3 * * *` → 3時間毎 :31 / drive_receipts | production_flow.invoke→receipt_confirmation_productionでscan/context/承認反映→production_source→drive_receipts.process_inbox→ReceiptPipeline。レシート/取込データ/支出明細、買取は収入明細、確認store、Drive archiveに届く。全page privacy・medical手入力・固定source/duplicateを維持。既存RECEIPT_CONFIRMATION_BINDING/scan plan必須。旧hold・途中複数sheet write・原本archive結果を確認し、旧案件を新R1へ移植しない |
| 銀行 | `47 21 * * *` → 06:47 JST / drive_bank + --bank-apply | CLI bank-pdf-recurring→既存recurring authority/監査鍵・固定window→bank writer/finalization。取込データ/支出明細、income flagと固有validator次第で収入明細、bank native state・archiveを更新。authority有効期間/対象/上限、income/review flag、内部振替設定、catch-up cursor/pendingを確認。PDF金融を新R1へ追加する作業はしないが、従来bank PDF処理は再開対象である |
| PayPay | `11 23 * * *` → 08:11 JST / drive_paypay | production_source→DrivePayPayPipeline.apply→PayPayPipeline.import_csv、固定import ID/既存duplicate照合→取込データ、Drive processed印/移動。CSV取込途中・移動のみ失敗を区別。将来coreの照合/支出化へ渡るため単独import成功だけで金融完了としない |
| Amazon等core | `17 9,21 * * *` → 18:17/翌06:17 JST / core | CLI amazon-gmail-recurring→既存authority/Sqlite checkpoint→Amazon注文/イベント/取込/支出、money mode次第で既存MoneyWriter。aupay card/balanceも同じcore。続いてreview-apply→reconcile→auto-expense→view refresh。他scopeのready/銀行catch-upを確認してから後続へ進む。取得間隔/最大window/件数/overlapを広げずbacklogを段階処理 |

coreの順序はproduction_run.DEPENDENCIESを維持。ReviewApprovalPipelineは支出/取込/要確認、ReconciliationPipelineは取込と除外支出、AutoExpensePipelineは支出/取込/適用ルールを更新する。classification flagと手動承認状態は既存のまま独立照合。manual `scope=all` にはdaily/category処理もあり、scheduleのcoreと同一ではない。銀行は独立scope、receipt/PayPayも独立source runであり、coreでそれらを再取込しない。

成功後、必要なscopeは既存ledger_order/投影/銀行review表示更新へ進む。ledger sortは支出明細/収入明細の日付順の既存処理で、IDを維持しprojection dirty証跡・read-backを使う。複数Sheet/Drive/nativeを単一transactionとは扱わない。再開監視はsource countだけでなく、対象ID・値・数式・無関係行を独立照合する。

## 4. 排他とSHA更新の波及

既存DriveStateTransportはread→write→read-backであり、**分散lockではない**。If-Matchによる原子的CASはない。ProductionLedgerもread一致確認後の通常writeで、競合writerに対する強CAS保証にはならない。これは今回導入された欠陥ではなく既存の前提。通常の復旧には、同じ台帳/原本へ届く全writerが既存`kakeibo-production` concurrency等で実効的に直列化される証拠、または競合能力の限定閉鎖が必要。別host/CLI/直接SDKはGitHub lockを共有しない。UNKNOWNを再送して安全性を試さない。

旧canary/p14/receipt-unit/manual/category/月次なども同groupの定義を持つが、historical SHA・別repository/hostの同一排他成立は未確認。旧receipt/bank/Amazon workflowのdisabled表示は、配布credentialや別hostの無権限を意味しない。#151で確認したpublic Cloud Run+旧credential参照/Accessor bindingの金融到達性は未解決。今回Cloudへ新しい試験/変更はしていない。

`KAKEIBO_VALIDATED_MAIN_SHA` 参照はmainで14workflowに存在する（evidenceの静的抽出）。parentだけの変更ではない。

- manual-entry、category request/navigation、migration、diagnose、旧canary等の到達条件も変わり得る。dispatch/re-runは別承認とし、再開確認に使わない。
- category-backfill-sheetは毎時07/27/47分。`CATEGORY_BACKFILL_SHEET_RUNNER_ENABLED=true`かつprojection有効ならSHA更新で条件が成立し得る。projection空ならSHA不一致でも起動し得る。rule UI/save/backfill flagと既存checked requestを別確認。共有SHA更新の前にこの波及を確定する。production/schedule flagだけでカテゴリjobが停止するとは仮定しない。
- monthly-maintenanceは独自main gateでバックアップ/cleanupに届き、validated SHAと無関係。原本/書込み/バックアップの競合確認対象。
- 3flagがtrueのままvalidatedをd62へ変更すると、**全4scheduleに適用**される。scope単位の段階再開をSHA更新だけで実現できるとは説明しない。

## 5. 二つの案

| 案 | 効果と最小変更 | 危険・前提 | 判定 |
|---|---|---|---|
| 検証済main d62へvalidated更新 | 変数1件だけ。runtimeはfffと完全一致、採用済文書を保持。新PRのheadやmerge SHAをvalidatedへ入れない | 次scheduleが即apply可能。関連14workflowの波及、pending/競合/有効credentialは未解決。変更直前main/実flag再取得とユーザーの明示的な再開承認が必要 | 確認後の優先候補、現在未実施/NO-GO |
| 旧fffを使う | 既存コードを戻しても金融runtimeは同じで、安全性の追加利益はない | 旧branch dispatchはmain ref guardで拒否。checkoutだけfffに変えるとevent/validated/main guard不一致。文書revertは新SHAになるためfffとは一致しない。mainをfffへreset/forceすると履歴と採用文書を失い、main変更禁止にも反する。旧run re-runも現在main checkout guardで拒否され得る | 採用しない。再開のためにmain巻戻し/guard緩和は不要 |

依存定義のblobは同一でも、未固定のpip範囲や外部API/model/credentialの現在挙動まで同一とは限らない。今回Ubuntuで現在解決した依存によるmain回帰を行い、実API検証は別承認とする。

## 6. 必要な確認・承認と再開順序（提案のみ）

1. **read-only設定確認**：変更直前のmain SHA、4変数、全関連flag（bank income/review、Amazon money、receipt binding、PDF intake、category runner/UI/save/backfill、projection）を人間の既存権限で確認。Secretは名前/供給範囲のみ。過去Variables値は不明のまま保存。未確認flagを既定値で補わない。
2. **read-only状態確認の個別承認**：production ledger/native stateの対象fileを暗号化bindingから安全な非公開領域で特定し、request ID/phase/generation/last_success/cursor/error/attemptだけの取得可否を示す。native全本文・添付原本・SQLite全exportは禁止。必要なfieldを抽出できなければそこで停止して承認範囲を再定義する。金融/医療値や全ledger read-backは対象ID/field/目的/一時保存先/保持期間を特定して別承認。Git/PR/CIへ実データを残さない。
3. **旧writer競合確認**：Actions全状態・旧canary/旧runner・Cloud/CLI/Apps Script/直接SDKの実効権限と利用予定を確認。同じ保存先へ届く未統制writerがあればNO-GO。利用者に必要な定期取込/本人表示を維持できる最小限定閉鎖を提案し、その操作だけ別承認。Cloud全停止を標準条件にしない。実backend強CASへの改修をこの復旧調査で行わない。
4. **事前照合**：最後のjob成功ではなくnative writer完了・対象ID read-backを確認。pending/UNKNOWN/PARTIALは保持。旧attemptの再送/自動retry/初期化/fence解除0。無関係sourceの未解決状態も依存関係に沿って判断し、core ready要件を満たさなければ後続を有効化しない。
5. **再開変更の明示承認**：上記確認後、`KAKEIBO_VALIDATED_MAIN_SHA`をその時点で同じコードと検証したmainへ更新する操作、影響する全schedule/関連workflow、開始時刻・監視担当・異常時停止権限を一括提示する。3flag維持なら次scheduleで全scopeが起動可能になることを承認対象に含む。狭い変更窓/段階再開のためflagを一時変更する場合も別承認し、今回は実施しない。dispatchで試さない。
6. **再開後監視・独立read-back**：run event/checkout/validated同一を確認し、各scopeのsource結果・counts・pending・catch-up・privacy/duplicate/authority拒否を監視。金融成功件数0でも無条件に障害扱いしない。最初の対象を限定して独立全対象/無関係行/数式/原本archive/nativeを照合する範囲を事前承認。backlog上限/authority期限を勝手に延長しない。
7. **異常時（実施承認を事前に用意）**：新規受付を止める最小flag/workflow操作を対象別に承認。進行中writerを安易にcancelせず、queued/in-flightとnative開始/完了を照合。結果不明ならpendingを保持してread-onlyへ移行。会計逆write、旧無条件writerへの復活、旧checkpoint復元・再送をrollbackに含めない。validatedを旧値へ戻して今後のmain scheduleをskipにしても、すでに開始したjob/他workflowの停止にはならない。

今回は上記の承認を要求して実施を進める段階ではなく、具体的な再開判断材料として保存する。実データを読まずに完了できる本調査を優先した。

## 7. 新R1の独立判定

旧金融権限統制、実backend強CAS、pending/UNKNOWN照合、未接続金融入口、実Google認証/本人確定UI、iPhone実機受入、旧public Cloud hostの権限問題はすべて維持。#152の隔離合成成功もmain定期取込の新R1化を意味しない。従来の再開が将来GOとなっても新R1はNO-GOのまま。一般/医療完全手入力を先行しPDF新金融接続は後続とする方針を変更しない。

## 8. 検証と保存

追加監査テストはGit全tree/commit分類、実YAML job条件のtruth table、実Bash routingを記録stubで実行、checkout guard/排他・既存非CAS前提、専用CIのPR限定/Secretなしを検証する。app production mainを起動するテストではない。

専用Ubuntuは#151由来アプリを実行せず、**d62そのものの独立worktree**で既存全synthetic pytestと既存Node3群を実行する。socket connectを拒否し、Secret/Variables/Environmentを供給しない。元テスト・安全assert・production workflowは一切変更しない。OCRは既存合成画像/fixtureとローカルtesseractだけを用い、原本やAI APIを使わない。CIの実測結果は新Draft PR本文/runに記載し、本番の強CAS/資格情報/未完了状態の証拠に流用しない。

ローカルWindowsは30 passed / Bash専用9 skipped。9件はUbuntuで必須実行する。main全回帰はUbuntuを正本とし、実結果を完了報告する。PR #123〜#152のhead/base/Draft/stateはbaselineと終了時にread-only比較し、#152コードは変更しない。
