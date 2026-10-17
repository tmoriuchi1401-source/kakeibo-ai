# Phase 5A：合成認証付きread-only接続

2026-10-18。到達点は **既存本人確認ホストのコードに、保存済み1タブ下書きの一覧・詳細を隔離合成環境で接続したこと**。確定受付・記帳できるシステム、本番接続、実Google OAuth、iPhone実機検証の完成ではない。

## 1. 調査と依存関係

main `d62e1e2` の `AGENTS.md` と `docs/iphone-receipt-review-mvp-spec.md` 全文（第10節を含む）、指定されたDraft PRの本文と継承コードを確認した。

| PR | head確認時点 | base | 再利用した役割 |
|---|---|---|---|
| #123 | `1db04ac` | main | canonical `Case`、read-only projection、旧案件隔離、eligibility |
| #124 | `936388f` | codex/unified-receipt-readonly | purpose、既存本人確認shadow gate、native validator/writer、共有CAS契約 |
| #127 | `ec38ae4` | codex/unified-receipt-write-bridge | 1タブ保持契約、native preview、継続仕様 |
| #128 | `e8a8048` | codex/iphone-one-tab-safety | raw reader、原本revision照合、Phase 4B JSON journal/CAS、version binding |

作業ブランチは `codex/phase5a-synthetic-readonly`、#128 headから分岐。新Draft PRのbaseは `codex/iphone-raw-draft-adapter`。既存4件のhead/base/Draftを変更しない。採用済みSheets「確認」1タブの列・保護・表示・値・原本リンクを変更していない。

## 2. 接続箇所と再利用

`app/iphone_readonly_host.py` の `make_isolated_readonly_application()` が、hash固定された既存 `services/human_general/web.make_application()` の**後**に、専用Blueprintを注入する。追加routeは次の2件だけ。

- `GET /isolated-synthetic/iphone-drafts`
- `GET /isolated-synthetic/iphone-drafts/cases/<case_id>`

Flask既定のHEAD/OPTIONS以外のmethodは登録しない。prepare/submit/確定受付のPOSTはない。既存ホストfactory・production composition・auth-session baseline/patch・native validator・writer・authority schemaは無変更。新モジュールの呼出し元は合成テストと合成HTML出力スクリプトだけ。本番factoryへのimport/入口登録はない。

既定OFFに加え、明示 `ShadowConfig(True, 'isolated_synthetic')`、`synthetic_only` runtime、`shadow_memory_only=True` clientを必須とする。登録後も既存session gateがruntimeの合成境界を再検証する。環境変数で本番へ有効化する仕組みは追加していない。旧shadowのmetadata POSTも、この新compositionには登録しない。

| 既存コード | 今回の利用 |
|---|---|
| `auth-session/compose.py` とhash固定host | 実factory/Google署名・issuer/sub/audience/nonce validatorを無変更で構築 |
| `unified_receipt_shadow.ExistingHostSessionGate` | 暗号化ticket、browser binding、owner、revocable shared login、既存actor、600秒request TTL。GETでsession延長なし |
| `unified_receipt_purposes.PurposeBoundary.get_list/get_detail` | 同一ownerの認証済みread、全件projection、詳細の一意性、常にoperation_targets=[] |
| `iphone_draft_journal.LocalJournal.read_versioned` | Phase 4Bのschema、contract、receipt chain、batch digest検証を伴う読取り。GETはinitialize/save/recoverを呼ばない |
| `iphone_sheet_reader.validate_snapshot` | **保存済み**raw snapshotの構造・保護・規則・順序・source ID/hash/revision照合。GoogleRawTransport/RawReaderを呼ばない |
| `iphone_native_dry_run.bind_case/check_version_binding` | 管理側alias→canonical Case、別namespaceのDrive revision/native versionの保存済み対応を照合 |
| `iphone_review_drafts.check_row` / `iphone_review_preview.check_native_case` | kind/purpose、canonical source/page/Unit/index、native input snapshotを純粋照合 |
| 既存medical/item validator、writer、CASテスト | offline native dry-run→保存→認証付きGETの追加統合試験と、既存回帰。GET中にはnativeサービスを呼ばない |

認証bootstrapは既存HostRigのRSA合成claims・memory証明書/code交換・既存Google verifierを通す。新しい認証provider、実OAuth設定、実token、実Googleログインを作成・使用していない。既存 `/start`・`/oauth/callback`・`/logout` の合成認証POSTと、禁止される新しい案件確定POSTを区別する。

## 3. 案件identity・purpose・下書き

管理側の非公開saved bundle（上限2 MB）をcompositionのdigestに固定する。内容は保存時のcontract/raw snapshot/source attestation、8案件のcanonical mappingと保存済みnative snapshot。SheetやURLからdigest・owner・canonical・purposeを採用しない。

GETはbundleと既存journalのbatchが完全一致した場合だけ表示する。別の下書きがjournalへ保存されていれば、古いbundleを自動採用せず409。読取り前後のjournal tag差も409。ファイル欠落・破損・receipt不一致をGETで作成・修復・隔離保存しない。認証失敗時にはrepository自体を読まない。

- SYN-G101/G102/M101/M102/P201/P202は、管理側が明示したcanonical `Case.case_id` と原本ID/hash、Drive revision、native source version、kind、purpose、page identity、Unit/index、raw input digest、native snapshot digestを照合する。行番号は保存済みlayoutの検証にだけ用いる。
- SYN-G103は原本/入力と保留を保持し、operation bindingを発行しない。SYN-H301は `LEGACY_UNMAPPED_HOLD`。canonical履歴表示Caseはあるが、原本・page/Unitへのmappingは空、リンクなし。類似値による紐付けを作らない。
- 一般の明細snapshotを別に保持し、item identity/digestとcanonical fieldsを照合して表示する。レシート単位のカテゴリセルを明細へ代入しない。異なる明細カテゴリを維持する試験と、既存native item snapshotをそのまま表示する試験を含む。
- 医療は保存済み手入力だけ。AI/OCR推論経路を持たない。手入力未完了の合成例も入力を保持して表示する。表示成功をnative validation/重複解消/記帳許可にしない。
- PDFは `STRUCTURE_REVIEW`、Unit空/index=0を維持する。一般・医療の `ACCOUNTING_POST` というpurpose表示も、記帳許可ではない。全案件 `authorized=false`、`accounting_allowed=false`、operation target 0。

GET時点のDrive/nativeの最新状態は観測しない。ここで検証するのは**保存された証拠間の整合性**であり、原本の現在の新鮮性・本人の再承認ではない。実行直前のfresh readと版対応はPhase 5B以降のゲートである。

## 4. 自動テストとGET副作用0の証拠

新規 `tests/test_iphone_readonly_host.py` は66件。対象は以下。

- 8案件の一覧・各詳細、alias/canonical/原本リンク/input snapshot、一般の明細別snapshot、医療・PDFのpurpose分離、2件の保留。
- 未認証の全9GET、別Google subject、署名/audience/nonce不正、owner/case owner不一致、request/session期限、logout、ticket改変、不正/null Origin、cross-site fetchを拒否。
- raw原本ID/hash/revision、native version/page/Unit/kind/purpose/native snapshot/input snapshot改変、行挿入/sort、legacy mapping捏造、hold解除、item snapshot/identity変更を拒否。
- 新規namespaceのPOST非登録、default OFF、production/non-memory runtime拒否、登録後のruntime変更も拒否。
- 全9GETと未知案件GETを各3回、ホストcomposition再作成、credentialsを継承しない別Pythonプロセスによる再読取り、journal破損/欠落/競合、HTML escape。
- 既存native医療/一般validatorをofflineで通したsnapshotを保存し、同じcanonical identityで認証付きGETへ接続。GETによるnative再観測・再検証はしない。
- 別ディスクの2つのローカルjournalは同じ初期CASを各々受け付ける、という**共有CASとして不十分なnegative evidence**。GETはlockを取得・作成せず、writerがlock保持中でも保存済みsnapshotを読める。

副作用テストではDB create/update、journal initialize/save/_replace、RawReader.read、native port observe/execute、grouping display/review、native state replace、session touchをfail-on-callにする。さらに認証DB全体/更新数、journal・bundle・native state bytes、ディレクトリー内ファイル集合の前後一致を確認する。全GETでnative calls、accounting append、AI send、source move、native state writeは0。認証bootstrap/fixture作成時の合成memory更新を、GETの更新に数えない。

ローカル検証は **598 passed**（新host 66＋従来adapter/安全回帰531＋新workflow境界1）、compileall/diff check成功。ネットワーク接続はpytest既存fixtureが拒否し、Firestore/Google client構築も新host suiteで拒否する。

専用CIは `.github/workflows/iphone-readonly-host-checks.yml`。#128 branchをbaseとするPRだけで実行し、Ubuntu/Python 3.12、contents:read、credentials非保持、Secrets/Variables/environment/schedule/deploy/buildなし。head SHAをcheckoutし、新66件をcollect＋個別実行、既存安全回帰を実行する。既存production workflowを緩めず、このCI全体の完全一致assertionだけ追加した。CIのURL・最終結果はDraft PRと完了報告で示す。

WindowsではGit配下のTEMPからの `git apply` がpatchをskipし、既存bundle hash検査が停止した。作業許可済みのGit外TEMPへ移し、hash/patch/tar filterを無変更で全件実行した。テスト成功のためにvalidatorを緩和していない。

## 5. 永続CASの本番適合性

**現行LocalJournalは本番利用可能と判定しない。** 今回は実運用ホストへアクセスせず、ソースとローカル合成試験だけで評価した。既存hostの認証stateには `FirestoreConditionalState`（update_time precondition、曖昧な通信失敗の自動retry/set禁止）があるが、draft JSONをそこへ接続するコードはない。認証stateの既存耐久性をdraft journalへ転用して保証したことにはならない。

| 状況 | 今の証拠 | 不足・必要な保証 |
|---|---|---|
| 同一ホスト/同一ディスクの再起動 | OSプロセス終了、replace前後の中断、別processでの再読取り/recoverを既存Phase 4Bで検証 | ホスト/ディスク喪失、電源断、filesystem/storage controllerの耐久性は未証明。Windowsはdirectory fsyncなし |
| 同一ディスクへの同時writer | OS advisory lock＋strong ETag CAS、2writerで1件だけcommit、process死亡でOS lock解放 | 全writerが同じlock/atomic replace契約に従う必要。容量制限/lock busyは停止。侵入者やlockを無視する別実装の防御ではない |
| 複数instance/別ディスク | 独立JSONが同じCASを双方acceptするnegative test | 共有・線形化されたCASなし。process local lockを分散lockとみなさない |
| shared/network filesystem | 対応・実測なし | OS lock/rename/fsyncの共有性・障害時意味が不明。共有volumeだけで保証したと判定しない |
| 通信断・応答不明 | replace前/後の中断をexact request fingerprintでread-only recovery、payload変更/replay/stale拒否 | Sheets・bundle・journal・native・ledgerとの原子transactionなし。新requestや再appendへの自動切替は禁止 |
| 保存領域の寿命/可用性 | ローカル既存ファイルでの再起動保持だけ | 実ホストの永続volume、再配置時の保持、backup/restore/RPO/RTO、暗号化・ACL・監査・復旧運用を未確認 |
| bundleとjournalの一致 | digest固定・batch一致・前後tagで観測競合拒否 | 2ファイルtransactionではない。更新中は表示停止になり得る。読了後の変更や実Sheetsの現在値は観測しない |

本番へ進む前に、既存versioned IO/conditional backendを再利用した非公開durable repositoryの設計・容量・権限・migration・recovery・複数instance競合/障害試験が必要。金融の共有 `ConditionalRequests` はdraft保存CASとは別の役割であり、置換しない。Cloud資源や永続backendの新設・設定は今回は実施しない。

## 6. PC・390pxとiPhone実機の区別

`tests/iphone_readonly_visual.py` は合成Google/session認証後の実Flask GET HTMLを6種類だけprivateへ出力する。cookie/token/DB/journalは出力しない。`iphone_readonly_browser.cjs` がそのHTMLをheadless Chromiumの1365px/390pxで表示する。

一覧・一般・医療・PDF・G103・H301の **12画像**を出力。全viewでdocument幅≤viewport、案件数一致、フォーム/入力/確定button/script/iframe 0、リンク高44px以上、外部原本リンクnoreferrerを検証した。ページのネットワーク要求0。複数の実画像を目視確認し、日本語、保留、入力/途中メモ、PDFの非記帳表示、明細表の表示を確認した。生成物はGit除外の `.private/phase5a-visual/` に置き、既存Sheets UIや本番入口は変えていない。

これは認証済みserver responseのPC/390px描画検証であり、ブラウザーの実OAuth往復・Drive原本表示・iPhone操作の成功ではない。架空source IDのリンクは到達可能なDrive資源ではない。

iPhone実機での未実施項目（安全な合成実機導線が別途用意・承認された後に実施）:

1. Sheetsの検索→対象ID→本人確認済み一覧/詳細でID・原本・保存時点を見失わないこと。
2. 合成原本を開く/拡大/戻る際のSafari・Drive・Sheets間の往復、メモと入力保持、意図しない新規windowや実行がないこと。
3. Google再認証、期限切れ、logout後の戻る/再読込み、別account選択、長時間background復帰時の拒否と秘密情報のcache表示がないこと。
4. 機内モード/通信復帰/二重tap/再読込みで再受付されないこと。保存済み表示と未保存編集を混同しないこと。
5. 文字拡大、縦横回転、横scroll、明細カテゴリ/G103/H301/PDF理由の読みやすさ、Sheets縮小倍率とアプリ再起動後の入力保持。

今回は実機導線の公開/デプロイを行っていないため、これらを合格扱いしない。採用済み1タブの過去の実機評価結果を今回の認証ホストの新しい証拠へ流用しない。

## 7. Phase 5Bの設計と承認事項（未実装）

| 次工程 | 必要な契約/検証 |
|---|---|
| 本人への再表示 | 原本ID/hash/revision、native version、全page/Unit/index、旧値・全明細・入力snapshot・保留・purposeを同一server acceptance recordへ固定。途中で変われば再表示・再確認。digestだけで本人承認を代替しない |
| 認証済み明示POST | 既存hostのactor/owner/live session/browser ticket/600秒TTL/Origin/CSRFとrequest ID/expected revisionを再利用。下書き読込と確定submitを分離。GETは引き続き保存済み表示だけ。状態セルの変更をPOST扱いしない |
| 全legacy入口の共有CAS | `ConditionalRequests` と同一native operation identity/Unit fenceを全既存service instanceと新入口で共用。新旧同時claim、purpose/request ID衝突、unknown停止を実証。入口ごとの別storeを作らない |
| 版対応 | trusted readerで原本bytes/hash/Drive revision IDとnative versionを独立co-observeし明示対応。文字列一致やSheet値から推測しない。prepare/acceptance直前/実行直前に再検証 |
| 一般の複数明細カテゴリ | 既存item_id/identity/token/field markerを固定した全明細の編集・本人再確認契約が必要。レシートcategoryの暗黙配布禁止。現在の1タブに対するUI変更案は影響範囲を示し事前承認 |
| 既存writer | 新しい直接appendなし。既存native validator/authority/ReceiptConfirmation・PDF Unit writerを通し、duplicate/replay/read-back/partial/lost responseを検証。応答不明から再appendしない |
| 追加ユーザー | issuer/sub単位の明示認可とnative grant/owner policy/案件ACLの一致を設計。現行owner契約を妻等へ偽装しない。principal追加・共有範囲は個別承認 |

Phase 5B着手・変更範囲、確定済みUI変更、永続backend接続、追加ユーザー、実機導線をそれぞれ具体化して承認を得る。本番入口登録/切替・実OAuth・canary・本番記帳・原本移動・AI送信・実authority変更・Cloud再開/deploy・main mergeは別途承認。今回のDraft/合成成功をそれらの承認にしない。

今回の実作用はローカルコード・合成テスト・Gitの独立branch/Draft PRだけ。本番Spreadsheet、既存検証ブック、Drive資源、Cloud、実認証情報、実医療情報へアクセス/変更せず、本番write・原本移動・AI送信・実authority更新・新規Drive資源は各0。
