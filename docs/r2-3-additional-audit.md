# R2-3追加監査：公開host・定期実行設定

2026-10-21 UTC、#150固定commit `d25d3823310c832fe955a2b3d51eb1df539c7184` からの独立分岐。**本番移行NO-GO継続**。本番設定・ジョブ・権限・実データは変更していない。金融明細、原本、native本文、Secret本文、Cookie、鍵、トークンは取得していない。

## 結論と証拠の読み方

最新productionはjob開始前の条件でskipped。**どのflagが条件を満たさなかったかは未確認**。Repository Variablesへの既存GETは401、既存ブラウザーは未ログイン404、接続済みGitHubツールは設定系API対象外。新規ログイン・認可・権限追加は行わなかった。推測による「production=false」「SHA不一致」への特定はしない。

Cloudでは旧revision/traffic/image/public IAMを再確認し、追加でruntime SAの条件付きDatastoreロールの式、旧credential Secretの有効versionとresource IAM Accessorを確認した。Datastore条件はSecret権限を制限しない。公開呼出し権限と旧credential参照・取得権限の組合せを阻害条件とする。アプリ内部認証の突破、漏洩、金融write成功を示す証拠は取得していない。

機械取得したActionsメタデータは [追加evidence](r2-3-additional-evidence/requests.json)。Cloudの画面表示を転記したメタデータは [Cloud観測記録](r2-3-additional-evidence/cloud-observations.json)。後者はAPI exportではなくConsoleの閲覧記録で、実効金融権限の証明ではない。project/SA/個人識別子はマスクした。#150時点の39入口分類は変更せず、今回の差分だけ追記する。

## A-1 GitHub Actions

[workflow実状態50件](r2-3-additional-evidence/workflows.json)：active43 / disabled7。activeは起動設定であり、金融実行中を意味しない。親 `kakeibo-production.yml` はactive。今回観測したqueued/waiting/in_progress/pending/requestedは各0件。ただしnative pending、旧端末・旧資格情報の不在は証明しない。

| 観測項目 | 結果 | 限界 |
|---|---|---|
| 最新run | [39548028007](https://github.com/tmoriuchi1401-source/kakeibo-ai/actions/runs/39548028007)、schedule、2026-10-21T05:10:05Z | job `119050589543` skipped、steps=[] |
| 直近3 run | 39548028007 / 39527573301 / 39519630251、すべてskipped | [run](r2-3-additional-evidence/production-runs.json) / [job](r2-3-additional-evidence/production-jobs.json) |
| event head SHA | 3件とも `d62e1e254692e676f4e4a72dc593524a80fbea08` | checkoutが実行されたSHAではない |
| checkout SHA | 最新3件はcheckout stepなし | 実checkoutをevent headから推定しない |
| validated SHAの実値 | **未確認** | Variable値とrun headの一致判定は不可 |
| 実行中のActions | [5状態とも0](r2-3-additional-evidence/active-runs.json) | 観測時点・GitHub側に限定 |
| job timestamps | APIの開始/終了に逆転がある | 生のメタデータを保存。これを金融実行時間に変換しない |

main固定SHAのソースではjob `if` が repository、main ref、`KAKEIBO_PRODUCTION_ENABLED=='true'`、`KAKEIBO_LEGACY_DISABLED=='true'`、schedule時の `KAKEIBO_SCHEDULE_ENABLED=='true'`、event SHAと `KAKEIBO_VALIDATED_MAIN_SHA` の一致を要求する。今回repository/main/scheduleはrunメタデータで確認できる。残る4設定のどれが不成立だったかを絞れない。

ログとの区別：最新jobはstep自体が存在しない。checkoutやvalidatorのログから失敗理由を確認したとは扱わない。過去のproduction stdoutには金融値が含まれる可能性があるため、無限定ログ取得は実施しなかった。#150が確認した過去successと、今回のskipped観測は別の証拠である。

### Repository VariablesとSecrets

以下は**定義上の参照名**。実値はいずれも未確認。`vars` はRepository/Organization/Environment等の設定変数で、`secrets` と同一視しない。

| 用途 | ソース上のvars参照（主なもの） | 実値 |
|---|---|---|
| 全体 | KAKEIBO_PRODUCTION_ENABLED / KAKEIBO_SCHEDULE_ENABLED / KAKEIBO_LEGACY_DISABLED / KAKEIBO_VALIDATED_MAIN_SHA | 未確認 |
| 銀行 | BANK_INCOME_WRITE_ENABLED / BANK_REVIEW_ENABLED | 未確認 |
| 通常receipt | RECEIPT_CONFIRMATION_BINDING、親scopeとschedule。独立したbooleanがあると仮定しない | 未確認 |
| PayPay | 親scope、PAYPAY_PROCESSED_DRIVE_FOLDER_ID | 未確認 |
| Amazon | KAKEIBO_AMAZON_MONEY_MODE、旧workflow固有flagは抽出表参照 | 未確認 |
| PDF | PDF_INTAKE_AUTOMATION_ENABLED、PDF_INTAKE_CONFIG_FILE_ID、canary/個別workflow flag | 未確認 |
| 日常/分類 | KAKEIBO_DAILY_CORRECTIONS_MODE / CATEGORY_RULE_AUTO_APPLY_ENABLEDほか | 未確認 |

全workflowのflag名・secret参照名・schedule・if・checkoutは [ソース定義の抽出](r2-3-additional-evidence/definition-metadata.json) に分離した。値を保存していない。資格情報は例えば `GOOGLE_SERVICE_ACCOUNT_JSON`、Gmail token、bank/card authorityのSecret参照があるが、実際の登録有無・値・配布先・可用性をソースから断定しない。

main側workflowにはEnvironment binding定義がない。追加の公開GET `/environments?per_page=100` は200・total_count=0で、**観測時点のrepository Environmentは0件**と確認できた（[アクセス記録](r2-3-additional-evidence/variables-access.json)）。このrepositoryに既存Environment/そのsecret供給経路は観測されなかった。一方、repository/organization secretアクセス設定、既定GITHUB_TOKEN権限は未確認。親はソース上contents:read、OIDC token要求なし。これをGoogle credentialの権限なしと読み替えない。

### schedule / dispatch / re-run

親のcronはUTC `17 9,21 * * *`（core）、`31 */3 * * *`（receipt）、`47 21 * * *`（bank）、`11 23 * * *`（PayPay）。dispatchはpreview既定、applyには追加確認と同じjob gateがある。activeとschedule定義は確認済みだが、条件を満たす自動再開は試していない。

historical `pdf-unit-write-canary.yml` はactive、`inputs.approved_sha` のcheckoutが存在し、PR #91 head/checksとvalidated mainの検証も持つ。runのhead SHAとcheckout SHAを混同しない。p14/receipt-unit系の旧dispatchもactiveとして残る。re-runのUI実行権限、再実行時点の変数評価、全Secret供給範囲は未確認。dispatch/re-runを行って確認することはしない。

補足：接続済みGitHubのcollaborator permission照会では、既存run主体であるrepository ownerのrepository roleは **admin** と確認できた。これはsettings用APIが現在の接続ツールで公開されていることや、旧workflowへ必要な全credentialが供給されることの証明ではない。照会結果は [Variablesアクセス記録](r2-3-additional-evidence/variables-access.json) に付記した。実行操作はしていない。

確認手順（人間の既存権限で閲覧のみ）：Settings → Secrets and variables → Actions → **Variables** で上記4設定と関連flagの名前・値・更新時刻を照合。Secretsは名前・供給範囲のみ。Settings → Environmentsでbinding/保護規則を確認。現在とrun実行時点の値は分けて記録し、過去値不明なら理由の確定は保留。flagをtrueへ変更したりvalidated SHAを更新したりしない。

## A-2 Cloud Run公開範囲

| 項目 | 今回のConsole観測 |
|---|---|
| service / region | hga-auth-synthetic / asia-northeast1 |
| revision / traffic | hga-auth-synthetic-00019-xf7、100%（最新にルーティング）。旧18件は0% |
| ingress / IAM | サービス一覧：すべて / 公開アクセス。resource IAM：Cloud Run起動元にallUsers |
| image digest | sha256:509b0a6612c29824636eecc992e3886be9f735cda602bac8cf024873dad304ab |
| image source/build | Console表示なし。現行repoの認証実装とdeployed imageの完全一致は未証明 |
| runtime SA | hga-synthetic-runtime@&lt;project&gt;.iam.gserviceaccount.com |
| project IAM | Cloud Datastore User、条件名hga_synthetic_database_only |
| 条件式 | `resource.name=='projects/<project>/databases/hga-synthetic-auth'` |
| 旧credential | Secret hga-p14-drive-credential version1、有効、`/secrets/p14-credential/credential.json` |
| 旧config | Secret hga-p14-page-config version6、`/secrets/p14-config/config.json` |
| credential Secret IAM | runtime SAにresource直付けSecret Manager Secret Accessor、表示上は継承なし |

他にweb-config / owner-policy / session-keyのversion1マウントを確認した。本文・OAuth client secret・credentialに含まれるSA IDやscopeは未取得。runtime SAはDatastoreの条件付きロールとは**別binding**で旧credential Secretを参照できる。Secret version1の存在・有効・Accessorは確認済みでも、Secret内のcredentialの有効性、失効、実Sheet共有ACL、Drive対象範囲、他projectの権限は未確認である。

IAM invokerはCloud Runへの入口、アプリ内部認証は到達後の処理の認可であり別の層。repoのauth-sessionはGoogle本人、owner、session、Origin/CSRF、nonce/PKCE、期限と目的別のnative検証を持つ。しかしimage→commit対応を証明しておらず、それらが現行host全endpointで成立すると断定しない。公開invokerだけで匿名金融write可能とも断定しない。今回は匿名HTTP試験・認証試行・金融POSTを一切行っていない。

解釈の一次資料：[Cloud Run公開アクセス](https://docs.cloud.google.com/run/docs/authenticating/public)、[end-user認証](https://docs.cloud.google.com/run/docs/authenticating/end-users)、[IAM Conditions](https://docs.cloud.google.com/iam/docs/conditions-overview)。条件は該当role bindingへ適用され、別のSecret Accessor bindingまで制限するものではない。invokerを閉じた場合の既存ブラウザー導線への影響は、これらの仕様とrepo設計からの推定で、現行endpointで試験した結果ではない。

repoには旧p14/PDF/native金融実行に至る設計が存在し、hostにはそのconfig/credential参照が残る。実効write到達性は未確認。この組合せはR1切替前に閉じるべき阻害条件である。

### 最小変更の比較（提案のみ、別承認）

| 案 | 影響 / 最小範囲 | rollback / 承認条件 |
|---|---|---|
| 旧金融能力だけを閉じ、本人確認画面を維持 | 旧金融route、credential/config供給と必要最小resource IAMを対象化。一般ログイン・表示を先に影響調査 | 別承認でread-only表示へrollback。旧無条件writerと金融credentialを復活させない |
| allUsers invokerを削除 | ブラウザーの既存Google OAuth cookieはCloud Run IAM用ID tokenではないため、/login・callback・確認画面へ到達できなくなる可能性。既存iPhone画面を壊すおそれ | 匿名公開を戻すrollbackは金融能力が閉じたread-only hostに限り別承認。新認証方式/IAP追加を今回の最小案には含めない |
| ingress縮小 / Cloud全停止 | 閲覧も止まり、定期処理/他利用者への影響未確定 | 今回採用しない。サービス全停止を独断で実施しない |

優先案は**金融能力の限定閉鎖と既存本人確認導線の維持**。ただしSecret内主体・各route/image対応・利用者依存が未確認なため、削除対象権限はまだ確定できない。Datastore条件の変更やDB追加は不要。ConsoleでSecretの「権限」、SAのIAM、対象Drive/Sheetの共有**メタデータだけ**を人間が確認し、旧金融能力と表示能力を区別してから個別変更承認を受ける。

## A-3 旧入口・資格情報

| 入口 | 確認した範囲 | 未確認 / 阻害条件 |
|---|---|---|
| historical SHA / canary | active登録、承認SHA checkoutと参照Secret名をソース照合 | そのSHAで現在供給可能なcredentialとre-run主体権限。旧SHAのgate未接続を維持 |
| p14 / receipt-unit dispatch | active登録とgate/Secret参照定義 | 同一ledger/原本への実効金融権限が閉じている証拠なし |
| Cloud旧host | public IAM、旧revision100%、credential v1のAccessor | 金融route到達性、credential内主体、Drive/Sheet ACL、他project IAM |
| CLI / 直接SDK | repo内のwriter/CLIとclient組立が存続。#147の39入口・#149の13群参照 | 配布済み鍵・OAuth refresh token・別host・ローカルenvの使用可否 |
| Windows / cron / Apps Script | #150のアクセス限界を継続 | ユーザー端末/他端末/別projectは不在としない。権限追加・旧端末ログインは行わない |

## pending・停止条件・R3承認

native/共有CAS/ledgerの本番本文は未読、pending/UNKNOWN件数も未確認。Actions 0件からpending 0件を推論しない。必要なら対象を**native request ID/phase/固定ID・全台帳の値と数式/原本version**まで限定し、目的（未完了照合）・一時保存先（承認した非公開作業領域）・保持期間・マスク方針を個別提示して承認を得る。金融値や医療欄に触れる照会は、その承認前には実施しない。Git/PR/CI artifactへ実データを保存しない。

R3開始に必要な順序は #150の8工程を維持する：①旧入口の新規受付停止だけを個別承認→②queued/in-flightとnative writer完了を照合して待つ→③pending/UNKNOWNを保持・個別read-only照合→④旧SHA/host/直接SDKの金融迂回を権限側でも封鎖→⑤切替前の独立全ledger/native/source read-back→⑥強CASと全金融入口統制が証明された範囲だけ新gateを限定有効化→⑦独立read-back→⑧異常時受付停止・read-only rollback。各々別承認。job終了だけでwriter完了と判断しない。

既存の銀行・PayPay・Amazon・通常receiptの定期設定は維持した。R1一般/医療完全手入力を先行候補、PDF金融接続は後続とする。ただし今回の追加監査でNO-GO解除条件は満たしていない。Bの合成成功も本番CAS成立・旧権限閉鎖・R3有効化承認にはならない。

## 保存・検証

変更は本書・マスク済みメタデータ9ファイルと、既存静的監査3ファイルの正確な追加パスallowlistだけ。既存のassert本文、app、host、auth bundle、workflowは変更なし。ローカルでR2-1 / M1〜M3 / M5〜M6の静的検査 **27 passed / 479 subtests passed**、`git diff --check`成功。A用の新規workflowは追加していない。#146〜#150のhead/base/Draft/stateの比較基準は [PR baseline](r2-3-additional-evidence/pr-baseline.json)。
