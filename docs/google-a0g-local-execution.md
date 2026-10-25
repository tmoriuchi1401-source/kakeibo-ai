# A0-G ローカル単発入口と終了手順（準備・合成検証のみ）

基点はDraft #164の `3bdcd34e7254a57cb59efd9f49da7f88e2ae59e2`。
この追加は実OAuth認可・token発行・Google GET・revokeの許可ではない。
本番4処理・R1・無人runnerはNO-GO。#162/#163/#164のhead、Apps Script、本番設定は変更しない。

## 1. 承認の再利用防止

実行入口は `python -m staging_shadow.google_a0g_local` の一時processのみ。
既定OFFで、明示実行flagがなければ契約/承認fileを読まずDISABLED。
入口が現在のclean checkout HEAD、private契約digest、保護された承認文書のfile SHA-256を照合する。
承認文書は人間の承認を証明する署名方式ではなく、既存のprotected-file承認方式を踏襲する。
文書と期待digestを配るtrusted operator、および実行入口/OS権限が信頼境界である。
任意Pythonコードや同じOS権限の管理者を隔離するsandboxとは扱わない。

サーバー/DBサービス/認証基盤を追加せず、既存 `app/aupay_card_production.py` 等の
SQLite BEGIN IMMEDIATE / synchronous=FULL方式をA0-G用の独立local fileに適用する。
金融DB・本番checkpointを開かず、そのmodule自体もimportしない。

保護stateはcheckout外・非同期local disk上の固定pathとし、registry固有のrandom IDへpinする。
実行中はmode=rwで開き、欠落・破損・置換・lock・registry ID不一致では停止。自動再作成/restoreしない。
別の新DBは新registry IDになり、旧承認文書と一致しない。
provision/enrollは実行入口から呼ばれない別のoffline operator操作。今回、実承認文書/実台帳は作成・登録していない。

認可とGETは別の承認ID/approved/期限を持つ。同じ固定SHA・private契約digest・registry・承認文書digestに結び付く。
承認文書schemaのauthorization承認は、本人Picker認可、code交換POST1回、access/refresh発行を明示して承認するもの。
metadata承認は、固定2fileに対する従来6 GETのみ。片方だけでは開始できない。
tokenを持って承認待ちしないため、同じ短い実施時間帯の2つの独立承認を先に用意する。

両IDを単一transactionで `UNUSED → CONSUMED_UNKNOWN` にcommitしてからbrowserを開く。
loopback bind失敗・browser起動失敗・拒否・中断・UNKNOWNでも返却/解除/再登録しない。
stateは通常終了時COMPLETED_NO_GO、失敗時STOPPED_NO_GO、異常終了時CONSUMED_UNKNOWNのまま。
いずれもconsumed=1。SQL triggerとAPIが消費の巻戻し/削除・回数減少・terminal再開を拒否する。

run nonceはmemoryのみ、台帳にはhashのみ。handleはprocess IDに束縛しpickle不可。
交換1回、metadata session開始1回、各GETの固定ordinalを外部通信の直前に別transactionで先に消費する。
新object、別process、再起動、並列、os._exit後でも入口の再消費は拒否され、追加通信は0。
新しいwire objectを同じmemory handleで作っても交換済み/GET済みordinalは復活しない。
低level adapterの既定live senderは拒否される。fake senderは合成試験用で、人間の承認の代用品ではない。

local SQLiteは管理者によるdisk rollback、台帳の削除/backup置換、改変した実行コード、別hostの独立stateまで防げない。
owner-only ACL、単一host/固定path、非同期・非一時disk、台帳の削除/restore/複製禁止を事前確認する。
これを確認できなければ実行しない。state不明は新DBを作って回復せず、停止・別レビューにする。

## 2. Desktop callbackからtokenまで

消費後、暗号乱数state（32byte）とverifier（64byte）を生成し、127.0.0.1のOS割当portに先にbindする。
listenerは最長120秒、かつ承認期限以内。accepted socketも絶対watchdogで閉じ、slow requestを残さない。
PKCE S256、同一redirect URI、固定client、drive.file単独、prompt=consent、trigger_onepick=true、
allow_multiple=true、folder不可、固定2file filter、include_granted_scopes=falseを生成する。
Edgeの明示した実行fileをInPrivateで起動する。既存default profileへfallbackしない。

callbackは最初のrequestで消費。不正path/method、重複key、不正encoding、state不一致、error、
scope拡大、2file集合不一致、code不正、重複callback、timeoutなら交換しない。
raw queryのscope/picked_file_idsを厳密変換して、既存validatorで再検査する。
受領codeは同一processの変数から交換adapterへ渡す。clipboard/chat/terminal/一時fileを介さない。
callback requestの標準access/error logを抑止し、responseはcodeを反射せず303でqueryなしの/completeへ送る。
listenerは受領後すぐ閉じるため/completeに接続errorが出ても異常ではない。browserは閉じてよい。

token交換は固定endpointへのauthorization_code POST1回で、ledger消費後のみ。
GoogleのDesktop仕様ではclient_secretはoptionalであり、この入口はPKCEのpublic clientとしてsecretを読み出さない。
secretなしでproviderが拒否したら停止し、secret取得・再発行・再試行へfallbackしない。
callbackとtoken responseの単独scope/client/期限を照合する。refresh tokenはresponseのmemoryから破棄し、GETへ渡さない。
実providerでの成功は未検証であり、合成成功を実認可成功と扱わない。

## 3. 正常・失敗・timeoutの終了

HTTP sessionのcloseがtoken参照、sender、response cacheを解放する。失敗時も即座に実施する。
交換のfinallyでcode/verifier/request/response/parsed tokenを解放し、refreshを含むdictをclearする。
入口のfinallyでcallback、state/verifier、grant、metadata、adapter/receiver/wiresを解放し、listener/workerを閉じる。
例外は固定STOPPED_NO_GOへ変換し、traceback localsやraw error/bodyを外へ出さない。
CLIはsanitized status/count/blockerのみ出力してprocess終了。実行後にREPL/notebookへ戻らない。
強制終了時はOSのprocess終了に依存し、台帳はUNKNOWNの消費済みとして残る。

Python文字列コピー、GC、del、clear、process終了はRAM/pagefile/crash dumpの完全消去証明ではない。
ブラウザーやOSの記録抑制も合成テストでは証明できない。InPrivateの閉鎖、history/session sync、extension、
HTTP debug/例外収集/crash captureの無効化を実環境で事前確認する。InPrivateを暗号学的消去と呼ばない。
browserが別processへ委譲する可能性があるため、無関係なbrowserをkillせず、本人が対象InPrivate windowを閉じる。

### ローカル破棄とGoogle側撤回

code未交換でもPicker grantが成立している可能性がある。交換timeoutではtoken発行の成否もUNKNOWN。
ローカル参照破棄、access期限切れ、台帳STOPPEDはGoogleのgrant/refresh token失効ではない。
revokeは自動実装・自動実行しない。再認可も自動化しない。

公式仕様ではrevokeは同じCloud project内の全OAuth clientのscope/tokenに影響し、反映遅延もあり得る。
保存済みA0-C一覧は専用Desktop client1件だが、現在のclient追加・他app依存・既存grantの不存在は未証明。
実環境への新しい照会は今回禁止のため行っていない。この証拠限界を残し、独立した直前確認を必須とする。

認可前にprivate cleanup planを作り、対象project/app/client、同projectの他client/利用者/既存grant、
担当本人、期限、影響先への対処、拒否/timeout/結果不明/正常終了別の撤回要否を記録する。
そのfile digestを承認文書cleanup_plan_refへpinする。現在状態の証拠がなければlocal_controls_reviewedをtrueにしない。
値を保持しないため、Google Accountの接続管理で本人が専用appを特定し、別承認後に撤回する方式を第一候補とする。
programmatic revoke POSTは別実装/別レビュー/別承認。今回の6 GETにrevokeやtoken確認GETを足さない。
失効確認も追加APIを勝手に送らず、account画面の結果/時刻を秘密値なしでprivate記録する。

公式: [Desktop OAuth / PKCE / optional secret / revocation](https://developers.google.com/identity/protocols/oauth2/native-app)、
[Desktop Picker](https://developers.google.com/workspace/drive/picker/guides/desktop-mobile-picker)、
[Testing token期限](https://developers.google.com/identity/protocols/oauth2#expiration)。

## 4. 実行前のoperator手順（今回は行わない）

1. 変更後のcandidate SHA/clean tree、private契約digest、この入口と既存6 GET制限を再レビューする。
2. 同じ専用client/本人/Testing/drive.file/正確な2fileを再確認し、cleanup依存関係とbrowser/OS/state ACLを確認する。
3. checkout外の保護された非同期local diskへofflineで専用registryをprovisionし、その新IDをpinする。
   実行入口はDBを作らない。既存金融/Google/Firestore stateを流用しない。
4. 認可・code交換・refresh発行と、最大6 metadata GETを別々の人間承認とする。
   承認ID2件、候補SHA、契約digest、registry ID/path、発行/終了時刻、browser実行file、cleanup plan digestを固定。
   期限はissued_atから最大600秒。個別期限の短い方で全runを制限する。
5. 承認文書をowner-onlyのcheckout外fileへ置き、期待するfile SHA-256をtrusted operatorが独立に固定する。
   schemaはAuthority.loadを参照。authorization.approvedとmetadata.approvedを別々に確認し、offlineで1回だけenrollする。
   単にJSONのapprovedをtrueにしたことやCLI flagを、人間の承認と扱わない。
6. 一時local processで入口を1回だけ起動。code/tokenは引数/env/fileにしない。本人はprivate browserで対象を確認。
7. 完了/中断後はstatusと台帳consumed/count/terminalを確認し、InPrivate windowを閉じる。
   UNKNOWN含む消費済みを解除しない。別承認のcleanup planに従い、Google側撤回の要否を処理する。

## 5. 成立範囲と残条件

| 最終レビュー不足 | この追加 | 残る実施条件 |
|---|---|---|
| B1 callback/秘密handoff | loopback、strict parser、同一process交換、PKCE/state、停止/非記録経路を実装し合成検証 | 実browser/OS設定・provider到達性は未証明。実認可と発行は別承認 |
| B2 再利用 | durable pair消費、交換/session/GET ordinalの先行debit、process束縛、crash/並列/再起動の合成検証 | 実台帳のACL/耐久disk/単一hostとprotected operator運用を確認し、実承認を登録する |
| B3 終了/失効 | 全終了経路の参照解放、refresh分離、例外出力抑止と別revoke手順 | Python/OS完全消去は保証しない。最新のproject依存確認とcleanup planの個別承認が必要 |

コードと合成環境の不足解消を実行許可に置き換えない。
認可工程もGET工程も、変更後SHA/契約digestの再レビューと各工程の承認が必要。
checkpoint/IAM/保存試験/Secrets/無人化/本番4処理/R1はこの入口へ接続せず、NO-GOを維持する。
