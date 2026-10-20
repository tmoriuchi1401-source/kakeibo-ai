# R2-3 Cloud / 旧host / 権限のread-only観測

2026-10-21 JST（2026-10-20 UTC）の既存Cloud Consoleセッションで閲覧。新規ログイン、Cloud Shell起動、API有効化、credentials取得、権限変更、service endpoint呼出しは行っていない。観測はUIメタデータであり、secret本文・Firestore文書・Sheetsセル・原本・アクセスログは未取得。

プロジェクトは既存資料と一致する `project-4cc…-89c`、以下 `<project>` と表記。個人メール、project番号、OAuth client ID、host URL、SAのproject部分は保存しない。Consoleリンクはproject未指定で、所有者が既知の対象を選択する。全project/全組織/他端末の不存在を証明する監査ではない。

## 確認結果

| ID | 閲覧画面 | 実観測 | 判断と限界 |
|---|---|---|---|
| C01 | [Cloud Run services](https://console.cloud.google.com/run/services) | フィルタなしの表示に `hga-auth-synthetic` 1 service、asia-northeast1。available、0 requests/sec表示 | 配備・起動経路あり。実際のrequest、writer実行、pending件数は不明。0rpsや直近1日graphの「データがありません」を停止証拠にしない |
| C02 | service → 変更履歴 | 19 revisions。`hga-auth-synthetic-00019-xf7` は100%（最新にルーティング）。00018〜00001は0%。最新deploy 2026-10-10 20:20:59 JST | 旧revisionが保存され、再配備/traffic変更の候補になる。0%はartifact廃止・credential失効の証拠ではない |
| C03 | 00019 → コンテナ | image `asia-northeast1-docker.pkg.dev/<project>/hga-auth-images/hga-auth@sha256:509b0a6612c29824636eecc992e3886be9f735cda602bac8cf024873dad304ab`。concurrency 1、max instances 1、timeout 60s、CPU 1、512MiB | digest確認。build/source情報は「ありません」。#149や現在mainのソース対応は証明できない。1 instanceも外部writerとの排他ではない |
| C04 | 00019 → セキュリティ | `hga-synthetic-runtime@<project>.iam.gserviceaccount.com`、Google管理暗号鍵 | 実runtime SA確認。GitHubの金融SAと同一とは確認していない |
| C05 | service → セキュリティ | 「認証が必要」checked、IAM checked、IAP unchecked、custom audienceなし | この画面だけで非公開と判定してはいけない。C06の実bindingを優先 |
| C06 | services → 対象選択 → 権限 → Cloud Run起動元を展開 | 「このリソースは一般公開されていて、インターネット上から誰でもアクセスできます」。Cloud Run起動元(1)のprincipalは **allUsers** | **公開invokerが実在。** IAMチェック有効でもallUsersにinvokerがある。アプリのGoogle本人認証・owner認可が無効という証明ではないが、旧host閉鎖/限定有効化の阻害条件。変更・匿名呼出し試験はしていない |
| C07 | service → ネットワーキング | ingress「すべて」checked、default HTTPS endpoint enabled、VPC接続unchecked、service mesh unchecked | 外部から呼べる設定。通信先の安全性や実egressは未検証 |
| C08 | 00019 → コンテナの参照設定 | `HGA_CONFIG_PATH`/`HGA_OWNER_PATH`/`HGA_SESSION_KEY_PATH`、`HGA_REAL_PAGE_CONFIG_PATH`、`HGA_DRIVE_CREDENTIAL_PATH` のsecret mount参照あり。`HGA_DATABASE=hga-synthetic-auth`、`HGA_SESSION_CANARY_ENABLED=true`、login TTL 28800/3600秒 | 旧config/credential利用経路が残る。secret値・鍵・token・owner policy・mount内容・secret version/IAMは未取得。mount参照があるだけでfinancial write可能とは断定しない。canary trueは合成requestの存在/実行証明でない |
| C09 | [Artifact Registry](https://console.cloud.google.com/artifacts) → hga-auth-images → hga-auth | Docker standard repo。17 image rows、現行digest prefix `509b0a6612c2` / tag `pdf-intake-v1` が存在。旧 `f3a3ec1fbf61` / `unit-items-c29ecc44e1fb` 等も残存。immutable tags False | コンテナbytes/manifestはダウンロードしていない。tagだけでソース同一性を認定しない。repo単位reader/writer権限と実pull可能性は未確認 |
| C10 | [Cloud Run Jobs](https://console.cloud.google.com/run/jobs) | フィルタなしtableのbodyは空、作成案内表示 | 当該projectの表示範囲で0件。他projectや削除済みexecutionは未確認 |
| C11 | [Cloud Scheduler](https://console.cloud.google.com/cloudscheduler) | `cloudscheduler.googleapis.com` の製品ページへredirect、「このAPIを有効にする」button | 有効化せず停止。scheduler設定一覧は未確認。「schedulerが存在しない」とは断定しない |
| C12 | [IAM](https://console.cloud.google.com/iam-admin/iam) project許可ポリシー | build SA: ログ書き込み。runtime SA: Cloud Datastoreユーザー、条件名 `hga_synthetic_database_only`。owner: project Ownerほか組織由来role | 条件式・deny/PAB・resource別Secret/AR/Firestoreの実効権限は未確認。role名やsyntheticという名称だけで金融権限なしとは判断しない。Sheets/DriveのファイルACLはCloud IAM一覧から分からない |
| C13 | [サービスアカウント](https://console.cloud.google.com/iam-admin/serviceaccounts) | default compute、hga-synthetic-build、hga-synthetic-runtimeの3 SAは有効、各row「キーがありません」 | 当該projectの表示上のkey metadata。GitHub金融credentialがこの3 SAのいずれかとは未確認。他project/削除済み鍵/委任/OAuthは別。秘密鍵を取得していない |
| C14 | [WIF](https://console.cloud.google.com/iam-admin/workload-identity-pools) | pool tableではなく「はじめに: Workload Identityプールを作成」の案内 | 当該projectのUIにpoolは表示されない。別project/orgのpool/trust、SA委任、外部credential不存在の証拠ではない。repo定義にもid-token:write/Google WIF設定はない |
| C15 | [Google Auth Platform clients](https://console.cloud.google.com/auth/clients) | `kakeibo-hga synthetic web` (Web)、`kakeibo-ai desktop` (Desktop) の2 client | client詳細のsecret表示/JSON downloadはしていない。redirect URI/実issued token scope/expiry/委任は未確認 |
| C16 | Auth Platform → 対象 / データアクセス | External、Testing、テストユーザー1人。非機密/機密/制限付scopeの各tableは「表示する行がありません」 | 設定UIのscope一覧が空でも既発行tokenを無権限・失効済みと扱わない。Gmail/Drive/Sheetsの実credential scopeは未確認 |

C06の証拠は権限dialogの表示と展開したprincipal。単なるservice定義からの推測ではない。画面の一般公開警告を読む操作に留め、principal追加・編集・削除、radio/設定checkbox、API有効化は実行していない。service一覧の選択checkboxは権限dialog閲覧のためだけに使用した。

## 保存された旧artifact

ARの表示された17行のdigest **prefix** / tag。prefixを完全digestに補完しない。

| digest prefix | tag |
|---|---|
| 509b0a6612c2 | pdf-intake-v1 |
| f67affef9ee2 | pdf-archive-d117190 |
| b389b398a98b | standalone-logout-d70be8c |
| 350918453373 | shared-session-1cbfdda |
| f3a3ec1fbf61 | unit-items-c29ecc44e1fb |
| dccbc01977b4 | items-1ac98bdbe694 |
| 5f84fd4145ae | items-7d3419083668 |
| d6d8190bcfd2 | independent-6d81847518e9 |
| 01ab1f1266ef | p14-timeout-20261007 |
| 2bc874b50790 | p14-permission-20261007 |
| ea1eb15a0e07 | p14-20261007 |
| 98176402d019 | final-20261007 |
| 5af8b5b53952 | fixed-20261007 |
| 6d6bbd1f7ede | headers-20261007 |
| 58e5b9b2a07c | synthetic-20261006-csp |
| 4d0ec08ecb46 | synthetic-20261006-patched |
| f7aca7addb1e | synthetic-20261006 |

## 未確認項目を閉じるConsole手順（実行依頼ではない）

1. 既存ownerがGitHub Settings → Secrets and variables → Actions の **Variables** で `KAKEIBO_PRODUCTION_ENABLED` / `KAKEIBO_SCHEDULE_ENABLED` / `KAKEIBO_LEGACY_DISABLED` / `KAKEIBO_VALIDATED_MAIN_SHA` / PDF・category・bank等の有効flagだけを確認する。secret本文や暗号化IDの値をコピーしない。ActionsのSettings、Environments、repository/branch権限、再実行可能な期間・主体を確認する。監査側はgh認証無効、IAB GitHubは未ログインの404、connectorの設定系GETは非対応。これはGitHub上の権限不足の確定ではなく利用手段の不足。
2. Cloud Run → 当該service → 変更履歴でdigestとtrafficを再確認。IAM条件dialogの式、resource policyのallUsersとinvokerを記録。**service URLのGETもapp requestになるため監査代替として試さない。**
3. IAM/SA → runtime/build/金融credentialに対応するSAの権限・委任・key metadataだけを確認。SAのメールは内部対応表で照合し、公開PRではalias化。Keysの「作成」/JSON download、Secret Managerの「値を表示」、OAuthのclient secret画面は開かない。
4. AR → repo情報/権限でbuild writer、runtime reader、ownerの継承元と条件を確認。Cloud Run image digestとAR完全digestの一致、旧revisionのタグURL/trafficの有無を確認。imageを実行しない。
5. WIFは既知の別projectも所有者が指定した範囲だけ確認。pool/provider、audience、subject/attribute mapping/condition、principalSet、SA impersonation、repo/ref/SHA制限を記録。設定がなければ新規作成しない。
6. Schedulerは既存APIが利用できるproject/regionを指定して状態・cron・target種別・SA aliasだけを確認。HTTP body/header/tokenは開かない。本projectのAPI有効化が必要なら別承認であり今回は未確認のまま。Jobsは他projectが対象と判明した場合のみ別閲覧する。
7. OAuthは既存認可のmetadataでredirect URI・scope・所有者/実行者・grant失効状況を確認する。token読取りや認可し直しは行わない。Apps Scriptは対象deployment ID/版、実行者、公開対象、time triggerとdispatch権限だけを所有者が確認する。Sheet本文/Script Propertiesのsecret値は取得しない。
8. 端末所有者がWindows Task Scheduler/cron/systemdと配布済みCLI/SDKの実行元を確認。名称だけで漏れるjobもあるため実行ファイル・引数の**秘密値を除いた**path、頻度、principal alias、checkout SHAを対象別に示す。本端末の `Get-ScheduledTask` はCIM資源アクセス不可で停止し、管理者昇格していない。[結果](r2-3-evidence/local-scheduler.json)。他端末は未アクセスであり不在とはしない。

## NO-GOに残る権限境界

旧hostの公開invokerとcredential mount、旧image群、active canary、未確認のrepository secret供給・desktop OAuth・直接Sheets SDKが残る。金融write権限の残存を否定できない。Cloud Runを止めるだけ、最新コードへgateを入れるだけ、GitHub concurrencyだけで旧principalを封じたとは扱わない。

旧hostの構造/明細確認・既存本人認証を維持できるかは、金融authority発行/実credential参照と分離して判断する。Cloud全停止を先に要求せず、必要最小限の金融入口・公開invoker・権限の閉鎖案を所有者が選び別承認する。今回の観測はCloud凍結を解除するものではない。
