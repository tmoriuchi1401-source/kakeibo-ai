claim済みレシートの限定終端復旧（オフライン実装・review専用）

通常処理の基準SHA：`fe90f64b47ffefae1efbca0ae7f646002e50576f`。
実装の親：既存GET診断review commit `e5d709a4f2e84311fb68cd4e8d485c9b2d409470`。
専用branch：`review/receipt-terminal-recovery-offline-20261116`。
本番NO-GO。実際の5件目は照会・変更・再実行していない。

**実装した動作**

claim CASが保存された後、外側metadataのJSON version差だけでreadbackが失敗し、
journal generation 3／検証中／result null／cache 0になる状態を、既存の完全合成fixtureで再現した。
失敗したadapterは書込み停止のまま保ち、通常workerの「受付済み」限定入口も維持する。

`TerminalRecovery.inspect()`は独立したGETだけで復旧候補を照合し、保存予定値を返す。
権限は発行せず、journal・cache・権限storeのいずれにも書き込まない。

`TerminalRecovery.recover(approval_id)`は、保護storeにあらかじめ発行された限定権限を必要とする。
原本照合・登録計画生成・再照合を通常workerと同じ関数で実行し、承認済み予定値と一致する場合にだけ権限を消費する。
権限消費後にも同じ原本／入力チェックを再実行する。例外や不確定な結果を成功・保留へ自動変換せず、write前に停止する。
正常に計算された「保留」「修正が必要」は、許可された終端結果として扱える。

**コードの分担と既存再利用**

|ファイル|変更|
|---|---|
|`app/receipt_terminal_recovery.py`|新規。readonly提案、限定権限のdurable消費、terminal専用IO、1回のcache publicationを実装。live factory、CLI、資格情報取得、grant発行／reset機能はない|
|`app/receipt_private_adapter.py`|GET-onlyの`read_checkpoint()`を追加。2回の既存厳密readとmetadataを照合して、本文バイト・envelope・強ETag・metadataを返す。既存IO経路のHTTP順序／回数と`_stopped`は変更しない|
|`app/receipt_sheet_validation.py`|claim後の検証を`checked_registration_result()`へ抽出し、通常workerと復旧で共有。通常workerのclaim、例外時の保留、終端CAS、ACK、ログ列は維持|
|既存journal／cache|`ValidationJournal.transition/save`、private adapterの予定本文等値・新ETag・metadata検証、core ACK、`result_cache_entry/cached_result`、既存projectionのRAW appendを再利用。journal schemaやcache列を変更しない|

**独立GETと権限に束縛する値**

- UUID、PURPOSE、snapshot全体と再計算digest、要求の世代、検証中／result null、他要求とのcase/source重複、active_preparedを確認する。
- private JSON全体のバイト列／SHA-256、catalog digest、強ETag、Drive version、folder／JSONの全metadata digestを固定する。ACL、owner、親、MIME、trashedの既存チェックは維持する。
- 対象private JSON／folder／owner／service、Spreadsheet、固定cache rangeを束縛する。後から最新ETagへ追従しない。
- 正常worker相当の原本bytes/hash/version、入力snapshot、候補／取込／rule等の独立照合、登録計画、再照合を行う。claim済みという事実だけで検証完了としない。
- 許可する遷移は「検証中→指定されたFINAL」だけ。結果、終端保存予定本文hash、cache key、審査済み実行コードSHA、元runのUUID／SHA／completed／attempt 1／claim readback失敗地点を固定する。
- 独立したcontrol portが、検証フラグfalse、競合run・他復旧0、全publisherの停止／排他、cache読取の完全性を毎回確認する。部分的なrange読取から「0件」を断定しない。

control portの値はSheet入力や自己申告のActions値を信頼するためのものではない。
将来のhostが、認証された独立GETと全writerの把握によってこれらを証明しなければならない。
現時点では、そのlive control portと実環境での排他証明を提供していない。

**一回限りの権限とappend未試行の証拠**

`DurableRecoveryAuthority`は、注入された保護CAS transportを使う。
grantはUUID・snapshot・対象世代・resource binding・許可結果等のscope、期限、消費状態、append履歴を持つ。
同一incidentへの別approval IDの重複grantも拒否する。

1. 保護storeをpreflight付きGETで読み、scopeの完全一致、未消費、期限内、完全な履歴と試行数0を確認する。
2. grantを消費済み・当該実行tokenにするCASを1回行い、独立readbackを確認する。応答不明ならreceipt CASへ進まない。
3. 消費確認後の同じ実行だけが、指定本文しか書けない`_TerminalOnlyIO`を取得する。
4. receipt終端のCASは最大1回。既存のadapter readbackとcore ACKを経て、終端全本文とjournalを確認する。versionはread区間内で一致を維持し、当該書込みの前後では増加を要求する。ETagが新しくてもversion不変／減少ならcacheへ進まない。
5. cache追加の直前に、grantが当該実行の消費済み権限で、履歴が完全・試行数0であることを再確認する。試行予約を1へ進めるCASとreadbackを先に完了する。
6. 予約後も期限・metadata／本文・排他・cacheを再照合してから、既存appendを同じ実行で最大1回呼ぶ。結果の独立readと終端recordの一致で表示完了を確認する。

grant消費とappend予約のCASは、業務journalの終端CASとは別の保護された権限storeへの書込みである。
通常journal schemaへメタデータを混ぜず、通常workerへ権限機能を接続しない。
試行予約は実際のPOSTより先に行うので、予約1・POST 0のまま停止する場合がある。安全側の未完了として残し、予約を0へ戻さない。

append履歴の`complete=true / attempts=0`は、信頼されたissuerが全publisherの履歴を照合して保護storeに置く証明である。
ライブラリはcache 0件からこれを生成しない。元runの停止地点と固定コード経路に加え、別publisher・不明な先行要求がないことまで証明できなければ、grantを発行してはならない。
grant発行、履歴初期化、store作成、署名／認証のlive hostは今回実装・展開していない。
オフラインfixtureだけが信頼issuerとして合成storeをseedする。

消費済みgrantは、復旧objectを作り直しても再利用できない。nonce／scopeを変える代替grantも同じincidentでは拒否する。
権限storeのCAS不明では、そのauthority instanceも永久にwrite-stoppedとなり、同じinstanceで再試行しない。
未到達の場合はstoreに消費が残らない可能性があるため、「送信要求が全世界で一度しか存在しない」とは保証しない。
保証は成功した権限消費を最大1回、その消費を確認した実行からのreceipt終端CAS最大1回・cache append最大1回という範囲である。
未知のauthority操作を再起動後に自動的に再送するhostは用意しない。

**停止と再開しない境界**

|障害／不整合|結果|
|---|---|
|scope・UUID・snapshot・世代・本文・ACL・ETag／version・run／排他・cacheの不一致|停止。最新値へのmerge／追従、再claimなし|
|原本照合・計画生成・再照合の不確定|停止。claim済みを検証完了に読み替えない|
|権限消費CASの応答不明|receipt CAS 0、cache 0。消費済みなら次のinstanceでも拒否|
|終端CAS前後の応答喪失、412、adapter／core ACK失敗|終端CAS再試行0、cache追加0。保存済みの可能性があっても直ちに停止|
|終端確定後・cache前のプロセス終了|終端は保存済みでも、別実行へcache権限を復元しない|
|append予約CASの応答不明|cache POST 0。予約・grantをresetしない|
|cache未到達／commit後応答喪失|再append 0。cache 0件を未送信証拠にしない|
|cache重複、矛盾、同UUID別key|停止。都合のよい行だけで成功にしない。削除・整理もしない|

遅延commitと未送信は同じcache 0件になり得るため、全障害から必ず表示まで完了させる保証はない。
応答不明後にcacheが完全一致で見える場合は、既存のreadonly結果照合で確認できるが、復旧grantを再利用したpublicationは行わない。
履歴記録だけではSheets appendを冪等にできない。将来、常に完了することまで求めるならkey単位の原子的な重複排除を持つ保存先契約が別途必要である。
金融write権限は常にfalse／件数0で、既存会計writerを構築・呼出しする経路はない。

**オフライン試験と完了監査**

新規 `tests/test_receipt_terminal_recovery.py` は、既存BytesIO／Projection／reader／journal／登録計画を使用する。
さらに実際の`ExistingReceiptDriveIO`へ合成HTTP sessionを接続し、If-Match PUTが1回だけであること、412・PUT応答喪失・JSON version差・core ACK失敗を確認する。
全試験でsocket接続・名前解決・金融SheetsDB構築を禁止し、資格情報や実案件をfixtureへ入れない。

|要求|主な証拠（テスト名の接頭辞 `test_` を省略）|
|---|---|
|5件目相当の失敗形と独立GET|`inspection_is_independent_get_only_and_normal_worker_still_rejects_replay`。JSON version差コード、generation 3、旧adapter停止、提案のwrite 0|
|通常worker相当の検証|`validation_uncertainty_never_becomes_a_success_or_hold_write`、`snapshot_change_from_reader_is_rejected_before_authority_or_terminal_write`、`successful_revalidation_can_finalize_a_non_success_result`|
|scope・ACL・世代・version・競合|`stale_or_reusable_authority_and_checkpoint_mismatches_never_write`、`control_gets_must_prove_exclusion_and_complete_cache`、`original_run_evidence_is_bound_not_inferred_from_time`、`authority_store_acl_preflight_failure_cannot_consume_a_grant_or_write`|
|一回限りの消費と二重復旧|`double_recovery_consumes_one_durable_grant_across_instances`、成功後の別instanceでのreplay拒否、別approval IDの同一incident拒否|
|終端CASの1回・競合更新|`existing_http_transport_retains_if_match_and_no_retry_or_reclaim`、`competing_update_at_cas_is_412_without_any_retry`、`foreign_update_after_authority_consumption_blocks_terminal_cas`|
|CAS前後の喪失・ACK|`terminal_cas_and_ack_faults_stop_without_cache_or_permission_reuse`、`authority_cas_ambiguity_never_advances_or_retries`|
|version／ETag検証維持|実transportのmetadata race／ACK試験、`post_cas_version_must_increase_even_when_existing_ack_succeeds`、`full_checkpoint_still_rejects_metadata_drift_on_repeated_get`|
|過去appendとcache不明|履歴不完全／試行済み拒否、`cache_unknown_never_reappends_even_when_zero_rows`、`changes_during_append_reservation_stop_before_post`|
|終端成功後の停止|`process_exit_after_terminal_commit_never_restores_cache_capability`。終端後／append予約後のプロセス終了と再生成後の拒否|
|期限・金融権限・ログ|消費後／append予約中の期限切れ拒否、`consumed_authority_cannot_change_target_or_gain_financial_scope`、`only_fixed_codes_no_private_values_or_exception_text`|

新規65件＋既存関連528件＝**593件成功**。
基準SHAの実クラスとの既存84故障プロファイル差分でも、HTTP列、CAS回数、永続状態、停止と照合後の再送拒否、既存ログ列が一致した。
通常workerの既存stage試験・private／metadata／cache／PDF registry関連試験も含む。全リポジトリの全テストを実行したという主張ではない。

再現する最小コマンド：

```powershell
python -m pytest -q tests/test_receipt_terminal_recovery.py
```

**実環境への非接続**

workflow、通常workerのdispatch条件、Variables、Secrets、main／実行branch、Apps Script、実5件目のjournal／cacheは変更しない。
この機能のnormal run/resultへの配線、live authority transport、独立control portのlive実装、grant発行や新規Drive資源作成も行わない。
今後の実環境適用には、保護storeのACL／永続CAS、全publisher履歴・排他、認証されたissuer／control、review済みSHAの固定を別途実証する必要がある。
専用review branchへpushしてremote SHAを独立GETで確認したところで停止する。本番NO-GOを維持する。
