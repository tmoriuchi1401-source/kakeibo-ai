# Phase 5B-1：永続下書き・原本版対応・明細入力の合成基盤

2026-10-18。到達点は **Phase 5B-2で本人再表示と明示要求を実装するためのoffline基盤**。本番利用可能性・Firestore耐久性・記帳許可を認定するものではない。確定POST、live factory、新認証、会計writer、Cloud資源は追加していない。

## 事前確認と依存関係

mainのAGENTS.mdおよび `docs/iphone-receipt-review-mvp-spec.md` 全文（第10節含む）を確認。正式採用のSheets「確認」1タブ、検索、原本往復、入力・注記保持を継承する。列・タブ・既存ブック・本番UIは無変更。

開始時のOPEN/Draftとhead/baseを記録した。独立ブランチ `codex/phase5b1-draft-foundation` は #129 のheadから派生し、新PRのbaseも #129 のブランチとする。既存PRを更新しない。

| PR | head SHA | base |
|---|---|---|
| #123 | 1db04aca633531871b3519e9eed8f7b76712f0e5 | main |
| #124 | 936388f2580e3c12c3f8c0ae470d8a59c1c431ef | codex/unified-receipt-readonly |
| #127 | ec38ae45445ce738a8630234366ecf2735a50c12 | codex/unified-receipt-write-bridge |
| #128 | e8a8048efc08327c1b37f3ba8cb38dfdb6fc71b5 | codex/iphone-one-tab-safety |
| #129 | a9066dc753fcf2271a13de8332ed43bba31d5f22 | codex/iphone-raw-draft-adapter |

調査対象：既存hash固定 `services/human_general/backend.py` のFirestoreConditionalState（auth-session baseline c29ecc44）、`ConditionalRequests`、Phase 4B LocalJournal、Phase 5A SavedDraftRepository / ExistingHostSessionGate、ReceiptConfirmation、receipt_item_review、PDF Unit・purpose adapters。既存認証・validator・authority・writer・baseline patchは変更していない。

## 1. 永続CASと既存再利用

`app/iphone_persistent_drafts.py` のPersistentJournalが既存 `read_versioned() / replace_versioned(before, strong_tag, after)` を受ける。テストではhash固定された**実装そのもののFirestoreConditionalState**へAtomicDocument fakeを渡す。Firestore Clientや実認証情報は生成しない。update_time precondition、retry=None、timeout=10、通信失敗後のset/retry禁止は既存backendのまま。

LocalJournalのbatch/state validator、UUID、request fingerprintの考え方、ConditionalRequestsのCAS→read-back手順、既存strong_etagを再利用する。独立DB、新サービス、分散ロックは追加しない。fakeのLockはserverのatomic update_timeチェックを再現するテスト専用であり、運用ロックではない。

owner hash・contract digest・projection generation・初期input snapshot・DRAFT_ONLYを暗号化domainへ固定する。batchはcase ID・kind・source identityの集合を維持する。入力snapshotと全明細detailsを同じ暗号化payloadへまとめ、1回のCASで更新する。request fingerprintはexact batch・expected tag・detailsに固定。同一IDの異なる内容は拒否し、正確な再送とrecoverはread-only lookupとなる。旧形式のrequest ID/fingerprintも移行後に保持する。

Fernet認証付き暗号で一般・医療・PDF・メモ・明細を含むprojection全体を保護する。認証state/EncryptedTicketsの文書・鍵・600秒保存期間へ転用しない。医療だけ別の非医療処理へ出力する経路はなく、kind/purposeの既存境界を維持する。ただし鍵管理・IAM・バックアップの実装/設定は未検証。

保存前のbatchとdetailsを履歴に保持し、途中入力・注記・保留・未送信値を剪定しない。暗号化後700,000 bytes、既存request上限256を超えると保存停止する。historyには旧request表も含まれるため容量増加は単純な明細件数に比例せず、先にbyte上限へ達し得る。productionでの世代切替・アーカイブ計画は未実装。現在のpayload validatorは承認済み**合成8案件契約**を使用する。本番case集合への一般化には正式な契約発行adapterの追加検証が必要であり、任意の本番案件を受け入れる実装ではない。

成功応答は保存payloadのexact read-back後だけ。通信断・応答不明・read-back競合では成功を返さず、同じID/内容/tag/detailsを保持してrecoverする。新request IDで再採用しない。後続更新後の旧receiptはis_current=False。下書き保存はいずれもauthorized=False / operation_targets=[]である。

### 保存・移行・隔離・復旧の責任

| 責任者・境界 | 必須操作 |
|---|---|
| 本人・クライアント | 未送信値とrequest ID/expected tag/exact payloadを結果判明まで保持。確定とは扱わない |
| repository | スコープ検証、条件付き更新、冪等照合、read-back、容量停止。create/delete/期限延長/自動repairを持たない |
| composition管理者 | owner・全案件・generation・鍵domain・source/入力snapshotを固定し、trusted reader/validatorを選定する |
| 移行担当者 | 旧writerを停止・退役させ、LocalJournalをロック下で二重読戻ししtag/hashを固定。migration_payloadによる純粋変換後、別domainへcreate-onlyで配置、復号read-back一致を確認。旧原本・注記・request表は保持 |
| 復旧担当者 | corruption/不一致を論理隔離して利用停止。recovery_exportのciphertext/tagをprivateに保全。原因調査と本人入力照合後、新generationへ明示復元。破損文書の上書きや自動再採用をしない |
| 運用・データ管理者 | 保存年限、削除承認、backup/PITRと鍵の保管・削除、監査・費用・障害訓練を担当 |

GETは隔離記録や復旧をwriteしない。恒久的なincident管理は運用責任であり、本実装は壊れた文書を読んだ全instanceが停止する。private exportは成功/採用を意味しない。復旧テストは別fake文書の新generationへ明示配置し、破損文書を残す。v1に存在しなかった過去の入力履歴は復元・捏造しない。

移行payload生成だけで旧writerが停止したとは保証できない。本番移行は旧入口の停止確認・create-only配置・read-back・退役監査を含む別作業である。

## 2. 原本revisionとnative version

`app/iphone_source_observation.py` のtrusted reader契約：

1. read_frameでDrive file ID / revision ID / 取得可能ならnumeric versionと、独立した既存native source version / page / Unit / index / purposeを取得する。
2. read_revision_bytes(file ID, revision ID)で対象原本bytesを取得しSHA-256を計算する。
3. frameを再取得し完全一致を確認する。取得objectの再利用による途中変更もdeepcopyで検出する。
4. 計算hash、Sheet原本identity、既存native identity、既存stable page生成関数（PDF/画像それぞれ）、Unit/indexを照合する。input snapshot・観測時点・全観測のsnapshot digestを保存する。

Drive revision IDとnative versionは別欄で保持し、同じ文字列を条件にしない。代入・推測・numeric versionからの変換もしない。numeric versionが取得できない場合のみNoneを明示する。revision、bytes、native version、page/Unit等の必須証拠が欠ければUNKNOWNで停止する。

revalidate(stage=prepare / acceptance / execution)により、各段階で新しい観測と保存済み観測のidentityを比較できる。byte更新だけでなく**同一hashのrevision/numeric/native version変更**も拒否する。会計Unitを持たないSTRUCTURE_REVIEWとACCOUNTING_POSTを別検証する。既存native validatorの版判定を変更していない。

未解決：live reader未接続、実Driveのrevision-specific bytes取得・既存native version取得の証拠、API権限・利用可能性・競合観測の実測。二重読取りは複数サービスのtransactionではなく、検証後の変更を永久に防ぐものでもない。5B-2以降も実行直前の既存native freshness fenceとwriterの再検証が必要。GETは保存証拠の整合性だけを確認し、native/Drive観測をしない。

## 3. 一般の明細入力契約と操作性

`app/iphone_item_drafts.py` はnative record/snapshotから、既存item ID・identity・token・item field marker・原本ページリンク・旧値・提案値（名前/金額/カテゴリ）・状態・途中メモを明細ごとに保持する。レシートカテゴリをbroadcastしない。

unentered / entered / held / not_applicableを区別。対象外でも明細を削除せず、その理由はメモに保持でき、会計dry-runの完成扱いにはしない。部分入力はdetailsとして同じCASで保存できる。追加・削除・順序変更・duplicate・別原本・token/marker/旧値/snapshot改変は拒否する。

既存validate / card / check_snapshot / evaluate / snapshotを通し、各明細のカテゴリ・型・金額・identityに加えて合計、税/値引き配賦、構造確認を維持する。金額/名前修正は既存recordが許可する場合だけ。入力完了時もready_for_owner_redisplayに留め、本人再確認必須、authority発行なし。入力契約だけの結果を確定証明へ変換しない。

| 案 | 操作性 | 保守・安全上の影響 |
|---|---|---|
| 第一候補：必要時のみ既存本人確認ホストの案件詳細で明細編集 | 1タブで探索し、複数明細案件だけ詳細で原本・旧/提案を照合。iPhoneでの往復検証が必要 | native item UI/token/validatorを再利用可能。認証/再表示/保存操作を既存ホストへ接続する作業が残る |
| Sheetsに明細用列を増やす | 横幅が増大し可変明細数・別原本との対応が読みにくい | 既存保護・規則・注記・raw reader契約を更新する必要があり事前承認が必要 |
| 明細タブを追加する | 案件・明細間の往復が増える | 正式1タブ仕様を変更するため不採用。必要になれば別途提案・承認 |

今回追加した表示は**隔離合成ホストのread-only詳細**だけで、原本ページ/明細、旧値/提案値、状態/メモを表示する。編集フォームや実ユーザー向け経路を追加していない。Sheetsに列・タブ追加の承認を要する変更はない。

## 4. 合成host・安全回帰・CI

Phase 5AのSavedDraftRepositoryへexact PersistentJournal型だけを追加。暗号化保存batch/detailsとpin済み表示bundleの一致、SourceObservation/VersionBinding、既存canonical/page/Unit/purpose、明細ID/token/markerをread-onlyで確認し、最後のtag再読取りで観測できた競合を停止する。新しいPOST route、prepare/submit/authorityはない。全新規namespaceのPOSTは405。

検証対象：8案件一覧/詳細、G103・H301保留、一般/医療/PDFのpurpose、既存Google認証・owner/session/600秒TTL/Origin、旧journal移行、別instance保存後のstale表示停止、GET中のread/write競合、既存native validator dry-run。

ローカルで新規98件（永続CAS・移行・復旧、版観測、明細、統合host）と、Phase 5Aまでの既存598件＋新CI境界1件＝599件、**合計697件が成功**した（38.02秒、skip/xfailなし）。compileall・diff checkも成功。専用 `iphone-phase5b1-checks.yml` はUbuntu 24.04/Python 3.12、contents:read、資格情報/Secrets/Variables/environment/デプロイなし、write flag false。新規node IDをcollectし全件をverbose実行し、既存安全回帰を別stepで実行する。remote CIの確定結果とcommitはDraft PR本文へ記録する。

テストはsocket接続を拒否し、Firestore Client/Sheets/Drive/live OAuthを使わない。認証bootstrapの合成memory更新とGET副作用は分離。GET前後の認証DB・ciphertext・旧journal一致、およびwrite/native観測/authority更新/会計append/原本move/AI送信0をassertする。CAS更新はfake documentだけ。

Windowsでは最初に既存composerのbundle_result_changedを検出した。temporaryをrepository内に置いたためgit applyの対象解決が変わる実行環境の問題だった。temporaryをworkspaceの別private領域へ移し、**hash検査・baseline・patch・tar filterを変更せず**既存66件が成功した。認証/validatorの安全境界の緩和はない。

本番Spreadsheet・既存検証ブック・Drive資源・Firestore・OAuth・Cloud・実医療データは未アクセス/未変更。本番記帳/原本移動/AI送信/実authority更新0。iPhone実機の今回の追加契約は未検証。

## 5. Firestore本番設計：公開資料のread-only調査のみ

以下は必要条件の設計で、既存プロジェクトの設定/IAMを確認・変更した記録ではない。

| 項目 | 実接続前の条件 |
|---|---|
| 保存先 | 既存DBの利用可否を調べ、auth/sessionと別のdraft文書domainを明示許可。自動作成なし。必要な分離が得られなければ停止 |
| IAM | 認証ホストに会計writeや全データ管理権限を追加しない。server SDKはSecurity Rulesを迂回するため、実効IAMとapplication owner認可・鍵分離を独立確認。collection名だけをIAM分離の証拠にしない |
| 有効期限・保存期間 | 認証600秒と未送信下書きの保存期間を分離。既存backendのexpires_atもdraft用期間で明示provision。保存年限/本人削除/保留/再送receipt保持を承認後に設定。期限延長をGETで行わない |
| 削除 | TTLは即時削除保証ではない。アプリ側は期限で読取停止し、承認された削除計画・backup/鍵の失効期間も管理。未送信入力を無断削除しない |
| バックアップ・復旧 | 同一DB/鍵を含むbackup/PITRの可否、復旧先、RPO/RTO、旧writer退役、新generation、復旧試験を承認。バックアップ復元だけで旧authorityを復活させない |
| 暗号化 | サービス標準at-rest暗号に加えて今回のdraft-domain暗号。鍵保管/ローテーション/回収・医療閲覧権限・private display bundleの保護は要実装審査。auth鍵の流用禁止 |
| 監査 | CAS/競合/unknown/回復/削除をrequest ID・scope digest・結果で監査。原本bytes・raw input/医療情報・鍵をlogs/PR/CI成果物へ出さない。Data Access logsの設定と保存先/費用も確認 |
| 費用・容量 | backendのread_snapshotを含め、保存は通常read数回＋条件付きwrite1回＋read-back、GETも前後readを行う。件数、地域、暗号化payloadと履歴、backup/PITR/TTL/監査を見積もる。named DBには無料枠を仮定しない |

根拠：server IAMは[公式IAM資料](https://firebase.google.com/docs/firestore/security/iam)、条件付き更新は[Python LastUpdateOption](https://cloud.google.com/python/docs/reference/firestore/latest/google.cloud.firestore_v1._helpers.LastUpdateOption)。保存期限/削除遅延は[公式TTL資料](https://docs.cloud.google.com/firestore/native/docs/ttl)、backupは[公式backup資料](https://cloud.google.com/firestore/docs/backups)。標準暗号化とCMEKの性質は[公式CMEK資料](https://firebase.google.com/docs/firestore/cmek)、監査対象は[公式audit logging資料](https://docs.cloud.google.com/firestore/native/docs/audit-logging)、費用は[公式料金表](https://cloud.google.com/firestore/pricing)を参照（2026-10-18閲覧）。固定単価や実費を確定していない。

Driveのversionはrevision IDとは別の更新番号であり、headRevisionIdには対象形式の制限がある。[公式Files resource](https://developers.google.com/workspace/drive/api/reference/rest/v3/files)。本契約は必要情報が取得できない原本をUNKNOWNにする。利用可能でない版情報の代用品を作らない。

## 6. Phase 5B-2の接続点と具体的な承認事項

1. **本人再表示と詳細編集の範囲承認**：1タブを維持し、必要時だけ既存ホスト詳細で原本・全明細の旧/提案・途中メモ・exact input snapshotを本人へ再表示する案。iPhoneで原本往復、認証期限切れ、通信断、未送信値復帰を試験する。実ユーザー編集は今回無効。
2. **認証付き明示確定POSTを合成環境で実装する承認**：既存actor/owner/session/Origin/CSRF/TTLへ接続し、全明細とsource observation/input digestを本人確認証拠へ固定する。repository保存と会計受付を分離する。今回POSTは未実装。
3. **共有ConditionalRequestsのcomposition確定**：新入口と全legacy入口が同じstore/operation identity/Unit fenceでclaimする構造を調査・実装・競合試験。draft CASは金融claimを代替しない。未共有の入口が残る間は実行不可。
4. **既存authority/writerとの接続レビュー**：prepare前/acceptance前/実行直前にrevalidate、native validatorを再実行し、目的別authorityだけを既存writerへ渡す。duplicate/replay/lost response/read-back/partial_unknown時の自動再dispatch禁止を保つ。
5. **永続backendの運用方針承認**：文書domain・実効IAM・専用鍵・保存/削除年限・容量/世代切替・backup/PITR・RPO/RTO・監査・費用上限・移行/復旧手順を具体化。実backend試験、Cloud設定/デプロイ/本番アクセスはいずれも別承認。合成fake成功を耐久性実証にしない。
6. **live source readerの条件確定**：revision-specific bytesと独立native versionの取得が実証できるread-only canary範囲を別承認。実医療情報を合成テストへ持ち込まない。
7. **追加ユーザーの明示認可**：妻等のGoogle subject・owner案件共有範囲・医療閲覧/編集権限・監査責任を個別承認し、既存owner-only gateを推測で広げない。

Phase 5B-2の合成実装承認、本番backend接続、iPhone実機・実OAuth試験、本番canary、入口切替、main merge、会計記帳/原本移動は別々の判断とする。今回の成果はこの先の接続点を検証可能にした基盤であり、いずれの実操作の許可も含まない。
