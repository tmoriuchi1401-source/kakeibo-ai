# R2-3 本番稼働read-only監査と最小切替計画

監査日: **2026-10-21 JST**（GitHubの時刻表記はUTC）。結論: **本番移行 NO-GO**。確認できるメタデータを収集し、最小adapter/短時間drain/旧入口閉鎖の設計を確定した。未確認の実行元・旧金融権限・実保存先CAS・pending照合が残る。今回の完了は監査/設計の完了であり、R1の本番有効化・本番記帳・切替開始ではない。

## 1. 基点・変更範囲

- main `d62e1e254692e676f4e4a72dc593524a80fbea08` のAGENTS.md、継続仕様全文（第10節含む）、PROJECT_STATUS/NEXT_SESSIONを確認。過去の運用記述はlast verifiedで、現在の稼働証明にしない。確定「確認」1タブ・一般画像・医療完全手入力・既存入力/保留を維持する。
- #146 R1、#147の39行/drain、#148 M1〜M3、#149 M5/M6を確認。#149 headは指定の **`9ba5d11d75aeb55d21b3540ff06ee11dc937ac68`** と一致。#146〜#149はOPEN/Draft/未merge。[開始時](r2-3-evidence/pr-baseline.json)。新branchはこの固定headから分岐。
- 新branch `codex/r2-3-read-only-audit` のみ。処理コード、writer、production constructor、workflow/schedule、validated SHA、auth-session bundle、継続仕様、既存39行の過去判定は変更しない。追加資料と、その正確なパスだけを既存静的検査の許可一覧へ追加する。安全assertは保持する。
- 新Draftは既存の合成CIをworkflow変更なしで利用するため #148 branch `codex/r2-2-isolated-mandatory-gate` をbaseとする。PR差分に**無変更で継承した#149**が含まれる。今回独自差分の基点は#149であり、既存PRのbase/headは変更しない。main向け統合PR/mergeではない。

## 2. 証拠と実稼働の結果

| 提出物 | 内容 |
|---|---|
| [Actions報告](r2-3-actions.md) | 50登録workflowのactive/disabled、main定義schedule/dispatch/権限、運用22workflow各直近3run、各最新job/step、実head SHAとcheckout/validatedの未確認区分 |
| [Cloud/旧host/権限](r2-3-cloud-host.md) | 実service/revision/traffic/digest、AR旧image、runtime SA、IAM/allUsers、OAuth/WIF、Jobs/Scheduler/端末の確認範囲とConsole手順 |
| [39入口表](r2-3-ingresses.md) / [JSON](r2-3-ingresses.json) | #147の全39 IDを維持し、実稼働状態・同一原本/台帳・gate迂回と証拠を追加 |
| [A01〜A13最小方針](r2-3-minimal-adapters.md) | A/B/C/Dを各groupに選択、関数/workflow/権限/順序/完了条件、日常定期維持とPDF後回し |
| [取得GET記録](r2-3-evidence/requests-start.json) | 実観測URL・時刻・HTTP結果。ログ/実データ本文は含めない |

主な実観測:

1. Actions登録50件はactive 43 / disabled_manually 7。mainにないbranch CIも登録されており、active数を本番writer数にしない。開始時のqueued/waiting/in_progress/pending/requestedは各0。これはnative内未完了0の意味ではない。
2. 統合production最新 `39483987991` / job `118839618684` はmain headで **skipped**、直近3runもskipped。安全flag/validatedの現在値は閲覧手段不足で未確認。**銀行/PayPay/Amazon/通常レシートの現在の定期稼働は認定できない。** 有効化も再実行もしていない。
3. production直近successは10/15の `38730949804` / job `116408287057`、head `fffbe7568db3acd647e9a088280bd4fcbed6c5c9`。checkout/main guard/source実行step成功を確認。対象金融件数・native全完了は未確認。過去成功で現在のwriter完了を代用しない。
4. 旧process-receipts、独立au PAYカード、独立bank recurring/backfill、retired AmazonはAPI disabled。一方p14、receipt-unit、historical PDF write canaryはactive/dispatch定義が残り、過去writer step成功。historical workflowはevent SHA検査後に別approved SHAをcheckoutする。**run headと実処理SHAを同一視しない。** re-run/dispatchの実効権限と現在のcredentialは未確認。
5. 旧hostはasia-northeast1、`hga-auth-synthetic-00019-xf7`にtraffic 100%、image digest `sha256:509b0a6612c29824636eecc992e3886be9f735cda602bac8cf024873dad304ab`。19 revisions・17 AR image rowsが残る。build/source metadataなしで、現在main/#149との一致は未証明。
6. **Cloud Run resource IAMにallUsersのinvokerが実在。** ingress all。service設定の「認証が必要/IAM」表示だけでは非公開にならない。アプリ内部のGoogle認証とは別の境界。旧credential mount/real-page config参照、session canary=trueも残る。金融write権限・scopeは未取得/未確認。
7. runtime SA、project IAMのrole/条件名、2 OAuth clients、External/Testing、1 test userを確認。実効file ACL、旧key/copy、発行済OAuth tokenのscope/失効、別project WIF、金融SAの対応は未確認。secret値の取得はしない。
8. Jobsは当該projectの表示で0。SchedulerはAPI有効化画面で停止。Windows SchedulerはCIM閲覧不可。旧host/別端末cron・手動CLI・直接SDK・Apps Script deployment/trigger/実行者は不在と断定できない。

取得は既存GitHub connector、公開GitHub GET、既存Cloud Consoleの閲覧だけ。無効gh認証の修復・新規ログイン・権限昇格は行っていない。公開APIのためのネットワーク許可による取得は、Googleや会計データへの新規認可ではない。GitHubログ、CI artifact、Cloud application log、service URL呼出し、Firestore/Drive本文、Sheetsセル、原本の読取りは0。

## 3. pending / UNKNOWN の監査境界

**進捗: 対象の保存先構成をコード/設定参照から特定。実pending数・request一覧・UNKNOWN件数は未取得で「未確認」。0件と報告しない。** R1/共有CASの本番保存先が存在するかも未確認。#149のmemory fixtureを実storeへ読み替えない。

既存preview/dry-runも安全なmetadata-only照会とは限らない。`production_source.receipts(apply=False)`は原本download/ローカルprivacy検査、`receipt_confirmation_production.execute(apply=False)`は既存会計表・native・本人入力を読む。今回これらのCLI/preview/audit workflowを**実行しない**。既存`PostingStore`や`ProductionLedger`のconstructorも本文loadを伴うためmetadata確認のためにimport/instantiateしない。

| 対象 | 所在の確定方法 / 必要閲覧権限 | 承認後の最小取得範囲（今回は未実行） |
|---|---|---|
| 親`ProductionLedger` | `KAKEIBO_RUN_LEDGER_FILE_ID` + `KAKEIBO_STATE_FOLDER_ID`、`production_flow`のStateBinding。既知固定fileのDrive metadata権限、本文は別承認のreader | metadata段階はmimeType/version/modifiedTime/capabilitiesとACLだけ。内容段階はsource、phase、attempt alias、generation、更新時刻、状態code。files/payload/金額/原本IDを公開しない |
| Amazon/card/bank `DurableState` | `AMAZON_STATE_FILE_ID` / `AUPAY_CARD_STATE_FILE_ID` / `BANK_STATE_FILE_ID`。各native StateBindingのsource/schemaを対応。実file IDは未取得 | 各固定文書のphase/pending/attempt/generation/version。対象requestがなければ別文書を探して全folderをexportしない。親ledgerとのattempt対応を所有者内で照合 |
| 旧receipt confirmation/reimport v1 | `RECEIPT_CONFIRMATION_BINDING` の既存固定manifest/file、再解析の別store binding。`ReimportStore`/receipt recovery構成 | native envelope/statusとoperation lineageだけを第一段階にする。ただし本文にparsed/医療入力/planが混在するため、読取り前に固定対象・field mask/所有者側の限定抽出を承認。v1の無条件saveは呼ばない |
| R1 v2/v3 / shared ownership-CAS | `ConditionalRequests`・v3 manifest/parent pin・`RetainedHistoryReader`の対象一覧から実backend bindingを所有者が照合。現時点では実所在不明 | manifest/namespace/schema/version、request/op alias、accepted/executing/writer_started/complete/UNKNOWN/PARTIAL、parent lineage。payload全文、owner本人情報、browser/session/CSRF/authority文字列を取得しない |
| PDF Unit/registry/posting/canary | `PDF_INTAKE_CONFIG_FILE_ID`、registry/journal/processing/`PostingStore`/`UnitPostingStore`、historical CanaryStore。旧hostのreal-page configとの対応は所有者側で確認 | 固定namespace・whole/Unit/partition完全性・state/開始証跡/版を限定抽出。不完全partition、旧p1/H301は保留。原本PDF・page画像・OCR/medical parsedを読まない |
| DailyCorrections / money / category / manual | 既存projection/correction/money native、管理request/history、manual受付。`DailyCorrections._requests/_read_target`はコード上確認だけ | 対象request IDは内部alias、state/revision/before fingerprint/固定target ID対応を限定。入力内容や取引額が混在するSheet range読取りは別承認 |
| 金融ledger | `SPREADSHEET_ID`から既存レシート/支出/取込/収入・Amazon関連表へのbinding。Sheets metadata reader/Drive permissions readerが必要 | 今回はコードのtable名のみ。承認後は固定requestに対応するID・対象行/式・無関係行不変を所有者管理領域で比較。初期の全件exportはしない。全ledger snapshotが不可欠なら対象表/range/期間/保存先を明示し別承認 |

実IDは今回入手していないため、connected Driveを広域検索して推測しない。既知対象が提示されるまでmetadata readerを作るための新権限も要求しない。情報が必要になった時点で「固定file/表/range・field・目的・閲覧主体・保存先・保存期間」を示し、承認を得るまでその読取りで停止する。Git/PR/CI artifactに実データを入れない。native本文のAPIがfield maskを持たず機微payloadを返す場合は、取得してからmaskする方法を採らず、所有者の承認済み限定抽出へ切り替える。

照合の完了条件は、job ID/attempt ↔ source/request/op ↔ native pending/start ↔ shared CAS ↔ writerが実際に触れた全table/ID ↔ 独立read-backの対応が一意になること。job成功、HTTP200、shared completeラベルだけではwriter完了にしない。証拠が欠ける場合はpending/fenceを残しNO-GO。旧pendingを新request/空v3に置換しない。

## 4. 最小切替の順序と工程別承認

**以下は設計のみ。操作を実行していない。** 作業窓の長さはpending/実writer所要時間未確認のため未確定。90分のActions timeoutをdrain SLAや自動cancel期限に使わない。平常の取込cronを変えず、無統制C/D/B入口だけを先に対象化し、全laneが必要な瞬間だけ短い変更窓を承認する。

| 工程 | 将来の操作と対象 | 通過する証拠 / 停止条件 | 人間が別途承認する内容 |
|---|---|---|---|
| P0 事前確認 | current flag/validated/実principal、対象native固定IDとfile ACL、旧host/Script/端末を埋める | メタデータと保存先binding。未確認実行元が残ればNO-GO | 新規アクセスや本文が必要なら固定対象と範囲。今回は新規アクセス/本文取得なし |
| P1 新規受付停止 | 旧確認金融受付、canary dispatch/re-run、旧local/SDK/Script、必要なB窓の受付。通常定期の継続対象を明示 | 対象別の受付閉鎖read-back。停止前のjob/attempt/requestを別台帳へ記録 | 各workflow/flag/Script/host/端末の受付停止と窓。既存job cancelは含めない |
| P2 queued/in-flight待ち | queued/waiting/in_progressと、runner外のrequest/native writerを分けて追跡。旧writeが開始済なら完了を待つ | 独立native/ledger/共通完了照合。job終了だけでは不可。timeout/応答喪失/不一致は保留し中止 | 必要範囲のread-only本文照合。cancel/再実行/復旧は別承認で自動化しない |
| P3 pending保持 | pending/UNKNOWN/PARTIAL・旧appliedと対応不明医療・PDF履歴を版付き保持 | operationの既存結果をread-only突合。未解決は凍結しgate閉鎖 | 機微対象の限定照合のみ。移行/解除/authority更新は本計画の承認に含めない |
| P4 迂回防止 | 旧SHA/artifact/host/直接SDK/コピー済みcredentialsのwrite能力を閉鎖。旧一般/医療v1金融saveとPDF金融を保留 | 古いartifactが新financial secretを取得できず、旧credentialで同ledgerを書けないこと。陰性証拠は承認された非金融検証で取得 | IAM/ACL/OAuth/key/secret供給/Environment・workflow変更、必要なhost変更を対象別に承認。今回は変更なし |
| P5 切替前read-back | 既存台帳のID/値/式、原本identity/version、全native/共有CASの前状態を承認範囲で独立観測 | 完全な対応・保存先同一・前状態が静止。実CAS/排他契約が未成立なら不可 | 固定source/native/表rangeの閲覧、private証拠保存先/期間。原本bytesが要る場合は別に明示 |
| P6 限定有効化 | Aの定期金融stageを同じ境界/実行laneへ接続、Bは短い直列stage、C/Dは閉鎖維持。その後一般画像R1、医療完全手入力R1の順 | 未移行writer/旧credentialが0。元authority/duplicate・本人/session/Origin/CSRF・snapshot・実backendを確認。PDF金融は後続 | 新artifact/main統合、constructor/host/UIの最小配線、実authority/金融POST、canary対象、deployを必要なものだけ個別承認 |
| P7 独立read-back | 各承認requestについてwriter結果に依存しない全table/ID/値式・native/shared completion照合 | 期待反映・余分な記帳0・既存無関係行/本人入力保持。ledger/native/sharedいずれか不明なら成功表示しない | 実データcanaryと前後read-back範囲。後続の定期run確認も別承認された運用内のみ |
| P8 異常/rollback | 新規受付を保留しread-only照会へ戻す。開始済operationの証拠を保持 | 旧v1/無条件writerを復活させず、不明結果を再送しない。read-onlyで復旧判断待ち | 新受付の保留/必要な既存financial境界停止を対象別に承認。データ巻戻し、fence解除、native初期化は含めない |

### P4の具体的な最小権限案

workflowをdisabledにするだけでは、古いcode copy・別host・SDK資格情報が残る。現在のrepo secret名に新keyを上書きするだけでも、同名secretを参照する旧workflow re-runへ新keyを供給し得る。**既存金融principalとそのsecretの供給範囲を同時に閉じる**必要がある。

優先案は既存の単一financial runner/既存SAを再利用し、必要なfile ACLと実行artifactだけへ限定すること。現mainのworkflowにはenvironment指定がなく、Environmentの実在/実効policyは未確認。既存の保護environmentが使えるならfinancial credential供給をそこへ限定し、旧repository secret供給を止め、配布済み旧key/tokenのfinancial能力を失効する案を所有者が審査する。Gmail/解析用credentialまで一括失効させず、金融write・原本移動・authority発行の権限を対象別にする。環境作成/鍵操作が必要なら別承認であり、新ログイン方式やWIFをこの監査で追加しない。

新R1 hostに裸のfinancial credentialを渡す方式は避け、既存native/本人承認を保ったrequestを同じfinancial laneで処理する最小境界を別実装する。実保存先CASが成立せず、外部principalを閉じられない場合は、read-backやconcurrency設定で代用せずR1を有効化しない。旧hostの公開invoker対策も、そのhostの既存非金融認証/確認フローに与える影響を示して別承認する。

## 5. R3開始に必要な変更・承認

1. **コードだけで進められる次の一手**: A10の既存固定ID/before fingerprintとA01〜A03の金融stageから、同一全ledger境界に必要なnative固有adapterを独立PRで実装/合成検証。A05〜A08のparser/duplicate/authorityは再利用し、常時定期を長期停止しない順序で接続する。共通laneで通常/買取/R1の排他、B窓、crash/UNKNOWN保持を検証。これは本番GOではない。
2. **実backend read-only開始**: 保存先binding/ACL/CASのmetadata確認を完成し、必要なら上表の固定native/ledger fieldだけを読む承認を得る。今ある本番pending/UNKNOWNを保持したread-backと実保存先の強い条件付き更新契約が必要。新CAS engineで置き換えない。
3. **旧実行権限閉鎖の承認**: canary/historical SHA/旧host/allUsers/直接SDK/Apps Script/端末・credential供給を具体的対象にしてP1/P4を承認。部分閉鎖や停止予定だけでGOにしない。
4. **実運用接続の承認**: main統合、validated SHA、workflowの金融secret境界、既存constructorと本人認可provider、必要な既存host最小変更/Cloud deployを、それぞれ差分・rollback付きで承認。Cloud凍結は維持し、必要性が判明した最小範囲以外を再開しない。
5. **実canary/利用開始の承認**: 正常な一般画像1件を先に、医療は新規の完全手入力を別に許可。旧H301/p1を再利用しない。PDF金融は別工程。実データをGit/PR/CIに保存せず、独立read-backと既存iPhone UIの本人/session/入力保持の受入が完了してから利用開始を判断する。

## 6. GO / NO-GO と完了条件

**NO-GO**。阻害条件は次のとおり。

- 旧金融writerへ到達するactive canary/historical SHAが残り、現在の実効dispatch/re-run・secret供給・旧credentialの失効を確認できない。
- 旧hostのallUsers invoker、credential mount、旧imageが残存。実ソース/権限/authority経路と閉鎖範囲が未確定。
- 他端末/CLI/SDK/Apps Script実配置・file ACLが未確認。共有ledgerへ書ける旧principalを否定できない。
- 実native/共有CAS/ledgerのbinding・強いCAS/排他、pending/UNKNOWN/writer完了は未照合。合成gate成功は代替にならない。
- A01〜A13の本番adapter金融接続は0。定期parentの最新jobはskippedであり、平常稼働の前提自体も再確認が必要。

可能な実稼働metadataの証拠、39分類、13groupの最小方式、未確認範囲/Console手順、pending承認境界、8工程の切替/rollbackと承認点を保存した。未確認を埋めずに設計できる範囲は完了。本番の安全性証明・R3実接続・切替・記帳は未完了かつ未承認のまま。

新Draft/commit/CI・終了時メタデータ照合は [検証記録](r2-3-validation.md) に記載する。既存main/PRが変化した場合は外部更新として区別し、この作業で更新したとは扱わない。
