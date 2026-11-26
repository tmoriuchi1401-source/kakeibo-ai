# PR #208 実接続の進捗と承認境界

2026-11-26。基準 `fef0d0a33b54f197a9f3bdd659766027563ca365`。
A01～A15不変。本番NO-GO。合成E2Eは実記帳成功と扱わない。

## 実認証と現在の接続

- 過去のphase2に保存された既存継続owner資格を発見。既存client/projectとの一致、
  既存refresh、Google userinfoのowner一致を実測した。新grant/資格発行、scope拡張は0。
- 必須5 scopeを含む既存7 scope（追加はprojects.readonlyとopenid）のまま使用。
  新しいscript.projects/script.deployments権限は要求していない。
- 既存API executableへ `receiptReviewRegistrationStatus(UUID,false)` を1回だけ呼び、
  HTTP200 / done=true / errorなし。backend=false、registration=true、STOP=false、
  対象既存request 1件、financial_write_count=0を読取確認。claim/再登録はしていない。
- `GOOGLE_RECEIPT_OWNER_TOKEN_JSON` をKakeiboAI Repository Secretへ初回保存しmetadataを読戻した。
  refresh/client secretはメモリ→gh stdinで暗号化保存し、値をログ/PR/Google Propertiesへ出していない。
  Secret既存時は上書きしない手順を使用。既存Google資格の失効・権限変更もしていない。
- review binding、E2E config、production receipt stateのhosted namespaceは未変更。
  本番と隔離のbindingを推測して設定しない。

## 実照合で見つかった最小修正

Script project IDとAPI deployment IDは別物だった。基準実装では両方をscript_idで扱い、
URLとScript側自己identityの比較を同時に満たせなかったため、bindingへdeployment_idを追加した。
RPCはdeployment_idへ送信し、Scriptの自己identityはscript_idで照合する。
両者同値・不正値を拒否し、旧4-key bindingへ曖昧なfallbackはしない。
[Google scripts.run reference](https://developers.google.com/apps-script/api/reference/rest/v1/scripts/run)
の配備ID契約と既存の実接続を根拠とする。新しい認証機構ではない。

## Script配備の停止箇所

既存本番兼用Script project `1Ur5mDufU5h6KHcOZ3bcaKNRSLbQFUfYdmoMdov0GVD7m410Pv6EWbWMu` は、
owner一致・HEAD及びversion5のソースとmanifestをAPIで読取確認。
5つの実行scopeとAPI access=MYSELFを確認したが、Hosted入口は配備されていない。
HEAD/固定versionの既存ソースは変更していない。現配備IDを使う読取RPCは成功したが、
deployments一覧は既存scopeではHTTP403のため、一覧からの現version照合は未確認。
過去のv5配備記録とversion5本文が存在することを、現deploymentの完全なversion証明とは扱わない。

過去の隔離Script3件もmetadata/manifestを読取確認した。これらにもHosted入口はない。
現資格はprojects.readonlyであり、APIによるソース反映・配備には別権限が必要。
scopeを増やさず、新しいproject/infraを作らず、本番兼用Scriptも上書きしていない。
既存ブラウザのOAuth callbackは認可コードを含むため画面読取が自動承認レビューで拒否された。
迂回せず、callbackを閉じて安全なApps Script画面にする本人操作を依頼した。

従って、現時点で新PAT非中継入口を使う状態write付きGoogle E2Eは未実施。
この配備部分だけが承認/対象照合待ちであり、hotfix・カード照合・hosted authは独立して進める。

## GitHub-hostedの隔離試験入口

既存 `state-cache-inspect.yml` にbranch/owner/attempt1/expected SHA限定の別jobを追加。
従来のread-only inspect job、入力、permissions、concurrencyは変更しない。
新workflowは追加せず、mainとして誤dispatchしても新金融処理を実行しない。
auth phaseはowner Secretだけを受け取り、userinfo以外のScript/Drive/Sheetsを使わない。
その他phaseは固定の合成target・本番IDとの不一致・owner/既存SAのみACL・原本hash/version・
空金融台帳を検証してから、既存ReimportStore/owner RPC/native prepareを実行する。
金融DBはreadonly Google scopeとwriteメソッド拒否を重ね、native_applyもfalse固定。
AI SDK/keyは使わず、合成解析結果だけを既存validatorへ渡す。

auth以外に必要な固定設定は `RECEIPT_HOSTED_E2E_CONFIG_JSON` Secretで、schema、state file/folder、
ledger book、manifest digest、10 fixture source IDs、review binding digest、ownerを指定する。
通常/例外/UNKNOWNの3原本だけを使用し、残り7件は既存ReimportStoreの10件契約維持用。
fixture/seedは純粋関数で準備できるが、harnessは資源を作成/リセットしない。
Google側の実初期化も、既存状態・旧UUID・STOP・UNKNOWNの上書きを認めない。

実行順：preflight → intake → iPhone登録 → process → 修正/再登録 → process → restore
→ unknown-create → fresh runnerのunknown-restore。UNKNOWNは最後に作り、解除しない。
正常は確認Sheetを回避し、例外だけ表示。金融/実AI/実原本操作は0を要する。
実dispatchの対象SHA・実測結果はPR #208本文とrun evidenceを参照。

### 実施済みのhosted auth証拠

[run45750220158](https://github.com/tmoriuchi1401-source/kakeibo-ai/actions/runs/45750220158)、
SHA `c151c4fdb8f4057238baa4d80b2e73ddf74cd743`、attempt1、owner actor、
PR208 branchのreceipt-isolated-e2e jobがsuccess。従来inspect jobはskipped。
実測出力はowner_authenticated / owner_match=true / required_scopes_present=true / scope_count=7、
financial_write_count=0 / real_ai_requests=0 / google_state_writes=0。
これはhosted本人認証の合格であり、未実施のRPC/state/登録/復元E2Eの合格や実記帳成功ではない。
後続のCI修正は既存production job検査を維持した隔離job境界検査とshallow checkout対応だけで、
auth実行コード・資格・workflow実行経路は変更しない。この実証を再利用してauthを再dispatchしない。

## 原本保護hotfix

[Draft PR #209](https://github.com/tmoriuchi1401-source/kakeibo-ai/pull/209)、
head `3eedf62ebd7417bbf8b79e4f75b2f6d61ddef5c2` はmain `fa7ac937…` 基準の独立修正。
#208の既存retention保護を再利用し、未記帳/確認待ち/不明/全Unit証拠欠落を保持する。
既存processedにも適用。PDFは全件削除拒否で、1 Unit完了を原本全体完了としない。
bindingが取得不能でもCLIは削除0へ閉じる。backup/workflow/本番設定は変更しない。
限定18件と[通常CI45748610528](https://github.com/tmoriuchi1401-source/kakeibo-ai/actions/runs/45748610528)
全3ジョブが合格。main merge・validated SHA昇格は別承認。手動cleanupは未実行。

## カードpendingと復旧境界

対象は[run41825241169](https://github.com/tmoriuchi1401-source/kakeibo-ai/actions/runs/41825241169)、
main fa7ac937、attempt1。guard/固定wheel成功、Applyだけがincremental_apply_stopped_no_retryで失敗。
例外全文/stackはmainが破棄しているため直接原因は未確認。
親generation1412/card pending、native generation121/pending、checkpointは11月3日のまま。
保存SQLiteのmanifest31/capability31 sealed/journal155は以前のもので、今回の一時journalは未commit。
障害区間はnative beginの保存/読戻しからcommit前までで、review/append前後を含む。

4金融表を既存owner OAuthの同一GET-only processで独立2回読取り、同一を確認。
カード取込1683行、ID重複0、formula0、失敗run以降の取込timestamp行0。
この現状だけで過去のwire未送信は証明できず、**UNKNOWNを維持**する。
完全反映/部分反映/未送信はいずれも未確定。現在台帳から推測してpendingを解放しない。

旧native revision本文は403、旧Sheet revisionはconnectorの500000-cell変換上限で取得不能。
既存Gmail readonly資格のローカル2候補はinvalid_grant。新同意/新資格を発行せず、
GitHub Gmail Secret自体の失効とも断定しない。
金融行を別toolへ渡す操作は自動承認レビューで拒否されたため、同一process内のAPI取得/解析へ変更した。
state/金融表の独立照合は成功したが、全原本canonical候補との照合は未完了。
対象hashと必要追加証拠の私的詳細は作業workspaceのcard-pending-findings.mdに保存した。

[既存PR #193](https://github.com/tmoriuchi1401-source/kakeibo-ai/pull/193) はDraft・c03d667aで、
PayPay単独取込の既存障害分離を再利用できる。receipt/coreへの無条件拡張はしない。
PayPayの取込が独立できてもreconcile/auto_expense等の共有core会計後処理は停止維持。

復旧案は既存release_after_reconciliationを用いるstate限定操作だが、先に今回候補と全ledger/review/moneyを
照合し、fresh native/parent digest・証拠reference・予定差分を固定して別承認を要する。
未反映/完全/部分を確定できなければ解除しない。他source・checkpoint・全SQLite・金融行は保持。
未記帳と確定しても、現guardの全失敗revision検査では今回runはreconciled例外でないため、
pending解放だけで次Applyを許可できない。旧runの例外を流用せず、恒久フェンスも解除しない。

## A01～A15と残工程

| ID | 今回の到達・残条件 |
|---|---|
| A01 | 固定SHAの既存GitHub-hosted Linux jobでowner実認証成功。実本番処理の開始ではない |
| A02 | PCは資格照合/保守だけ。hosted処理にPC token/runtime依存なし。物理PC OFF/状態復元E2Eは未実施 |
| A03 | 採用済み1タブを維持。新入口の実iPhone登録・結果表示は未確認 |
| A04 | 既存schedule/Gmailは変更なし。再開にはcard/native状態の照合を要する |
| A05 | 基準解析/分類/validator/重複試験を再利用。実AI0・純粋合成解析のみのharness |
| A06 | 正常の確認Sheet回避/native planは基準オフライン合格。実Google経路は配備待ち |
| A07 | 例外だけ表示する既存経路を再利用。実Googleの表示は未実証 |
| A08 | 1登録/再検証/同一結果は基準合格。実Google/iPhone接続は未実証、金融OFF |
| A09 | 独立hotfix準備/CI合格。main適用前は手動cleanupしない |
| A10 | 実AI0・医療手入力維持。実原本を合成E2E材料にしない |
| A11 | 銀行正本/Payrollを維持。買取票収入の是正は別単位 |
| A12 | 既存Google基盤を利用するharness。実保存/新runner復元/UNKNOWN保持は未実施 |
| A13 | 新SecretはGoogle OAuthでありPATでない。PATのGoogle保存/送信0。現金/カテゴリ依存は別単位 |
| A14 | 新infra/新資格/新署名なし。既存Google projectとActionsを利用 |
| A15 | 既存STOP/UNKNOWN/UUID/履歴/原本/権限を変更・解除しない |

次の最小順序は、隔離Scriptを確定して許可範囲だけ配備 → 固定隔離設定 → 上記E2Eと本人iPhone操作。
その後も実記帳canary前で停止する。canary候補は旧UNKNOWN/医療/買取/PDFを避けた新規一般1件と
同一isolated契約で確認済みの例外1件。予定変更は明示的なnative権限/対象固定/金融apply接続だけで、
原本操作・schedule開始は別許可とする。未記帳試験caseをflagだけで記帳権限へ昇格させない。
現金/カテゴリPAT、買取票収入、認証付きPDF Unitは独立単位のまま全体GO条件に残す。
