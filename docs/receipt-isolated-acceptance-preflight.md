# PR #200：隔離合成試験の事前確認と単一JSON CAS実測（2026-11-08）

**最新判定：承認された専用JSON 1件のDrive v2実API CASはPASS。iPhone実機は未実施。一般レシート登録・本番接続・金融writeは引き続きNO-GO。**

## 承認範囲内の実測結果

基準commit `a1c934591c5929ffef7357874082f028bc432f41`、Draft PR #200の実装・停止条件とローカル非公開承認計画を照合した。既存owner接続で承認済みprivateフォルダ直下の同名ファイルが0件であることを確認後、`receipt-validation-acceptance-20261108.json`を1件だけ作成した。別のowner metadata GETと既存service accountの直接GETで、返却された実ID・単一parent・MIME・trash=false・owner＋当該service account writerだけのACL・アクセス・初期bytesを照合した。権限追加・共有変更はしていない。

| 確認項目 | 実測結果 |
|---|---|
| JSON作成 | 成功。完全合成の初期JSON 128 bytes、16 KiB以内。作成1件、削除0件 |
| ID・ACL独立確認 | PASS。owner側の作成応答とは別のGETで照合し、service accountでもmetadata/mediaともHTTP 200 |
| A：正常更新 | PASS。最新の強ETag T0でPUT 1回、実HTTP 200。独立GETでAの121 bytes完全一致と別の強ETag T1を確認 |
| B：古いETag | PASS。T0で異なるBをPUT 1回、Googleの実HTTP 412。独立GETのbytesはAと完全一致、ETagもT1のまま |
| C：並行更新 | PASS。最新ETagを独立取得し、同じETagで異なるC/Dを各1回並列PUT。C=412、D=200。独立GETは唯一の勝者Dの121 bytesと完全一致し、ETag更新 |
| D：応答喪失 | PASS。PUT 1回後にアプリ側で応答を意図的に破棄。transport観測は200。その後PUTせずGETだけで意図した140 bytesと新しい強ETagを確認。**ローカル故障注入であり、実ネットワーク障害の実測ではない** |
| 送信・読戻し | media PUT合計5回（A/B/C/D/応答破棄それぞれ1回）、自動再送0回。独立metadata GET 12回＋media GET 6回、すべて200。各media GET前後のmetadata一致とbytes完全一致を検査 |
| 対象外write | Drive書込みは承認された新規JSONの作成1件と同じ実IDへのmedia PUT 5回だけ。対象外ファイルwrite 0件。親フォルダには承認どおり子が1件増えた |
| 総合判定 | **単一JSON CAS試験PASS。本番GOではない** |

PUTは`upload/drive/v2/files/<固定した新規ID>?uploadType=media&fields=id,etag`へ`If-Match`付きで送信した。既存`DriveRegistryIO.replace_versioned()`のclient側stale比較は使用せず、BもGoogleまで送信して実HTTP 412を取得した。HTTP adapter retry=0、redirect無効、事前取得token固定の通常Sessionを使い401時の暗黙再送を禁止し、resumable uploadも使用していない。一回性のローカルmarkerと固定ID照合を設け、別IDへのPUT経路は持たせていない。

実ID・ACLメール・ETag値・bytesのhash・各HTTP応答と送信回数を含む詳細証跡は非公開ローカル記録に保持し、公開PR/CIには実ID・メール・資格情報・private JSON全文を保存しない。計測helperもローカル専用で、製品adapter・workflow・登録bindingは変更していない。試験用JSONは最終合成payloadのまま保持し、削除・再利用・本番接続しない。

合成ブック・合成原本・Apps Script triggerの作成0、共有/権限/Secrets/Variables変更0、本番dispatch 0、AI送信0、原本移動0、Cloud Run操作0、金融write 0。main・validated SHAは非変更、PR #200はDraft・未mergeのまま。今回の差分はこの監査文書だけ。

今回の承認範囲はここで終了する。iPhone実機は未実施。次の隔離合成試験は、下記の原本読取・adapter未接続の障害を含め、必要な資源・共有・変更範囲を別途提示して個別承認を待つ。今回のCAS合格を一般レシート登録機能の有効化に使わない。

## 以下は今回の作成承認前の事前確認記録

以下にある「未作成」「未実施」「作成前停止」は当時の記録。JSON作成とCAS実測の現在値は上記が優先する。合成ブック・原本・trigger・iPhone実機の未実施と本番NO-GOは現在も変わらない。

## 今回のコード差分

`result_cache_entry()`は既存のFINAL（修正が必要／保留／検証済み・未記帳）だけを許す。UNKNOWNは`project_result()`による一時表示に限定し、永続cacheへappendできない。UNKNOWN表示後に元の同一UUID要求が終端確定し、privateを再照会して終端cacheを読めること、照会でjournalを変更・再実行しないことを2件の合成テストで確認する。他の機能・既存安全条件・workflowは変更しない。

## 読取専用で確認したこと

- 既存の環境設定にservice account資格ファイルが存在し、そのidentityと既存private保存フォルダのwriterが一致した。秘密鍵・token値は出力／保存／Git登録していない。
- 接続済みDrive ownerのmetadata読取と、既存service accountを`drive.metadata.readonly` scopeだけで用いた直接GETを分けて確認した。
- 既存private保存フォルダはservice accountでHTTP 200。owner＋当該service account writerの2 user ACL、`canAddChildren=true`を確認。これはファイル作成やCAS成功の実測ではない。
- 既存の完全合成一般PNGをowner側で発見しmetadataのみ確認。ownerのみのACL。同じservice accountでは当該PNGとその合成フォルダがHTTP 404だった。原本bytesは取得していない。
- private保存フォルダ直下で名前にSYNを含むimageのmetadata検索は0件。調査した範囲で追加共有なしに使える合成画像は特定できない。Drive全体に存在しないとの断定ではない。
- 実際のフォルダID・候補原本ID・ACLメールは、workspace直下のprivate承認用資料にだけ記録する。本public PRには転記しない。

## 作成候補（まだ作成しない）

| 論理対象 | 作成予定名 | 保存先・期待ACL | 用途 |
|---|---|---|---|
| BOOK_NEW | KakeiboAI_PR200_隔離合成受入_20261108 | 確認済み既存privateフォルダ。既存owner＋既存SA writerだけを継承。親ACLは変更しない | 「確認」1可視タブ、完全合成入力・表示cache。既存ブックのcopyや再生成はしない |
| JSON_NEW | receipt-validation-acceptance-20261108.json | 同じ既存privateフォルダ、同じACL | CAS probe専用の小さい非金融JSON。合格前に登録用journalとして有効にしない |

作成者は既存owner接続とする。service accountを新ownerにしない。実IDはいずれも未発行。特にGoogle Sheetsは[事前生成IDでのWorkspaceファイル作成がサポートされない](https://developers.google.com/workspace/drive/api/guides/create-file#generate_ids_to_use_with_your_files)ため、架空の予定IDや資源作成による既成事実は作らない。

「作成前に実IDを提示」をそのまま満たすことはできないため、**名前・親ID・ACLを対象にした作成承認が必要**。作成が承認された場合でも、返却IDを独立GETで照合して固定allowlistへ記録するまでは、CAS PUT・trigger設定・共有操作・登録要求を行わない。JSONは事前採番可能なAPIがあるが、今回採番や資源作成はしていない。

既存資源への影響は、承認後に親フォルダの子が2件増えることだけ（一覧・変更時刻等のmetadataが変わり得る）。既存ファイル本文・権限・親フォルダACL・本番設定・Secrets・Variables・validated SHA・main・金融stateは変更しない。新しいグループ／ユーザー／IAM／OAuth scope追加が必要なら停止。ACL継承が想定と違っても親ACLを修正して合わせない。

## 2資源で可能な試験と成立しない部分

新規2資源の承認後、JSON_NEWだけを対象にしたCAS実測は準備可能。一方、現在のreaderは独立した原本のDrive ID・画像MIME・bytes hash/version・parent/ACLと、候補・private anchorを照合する。Sheet入力やJSON内に原本らしいbytesを埋めてこの検査を代用しない。

**したがって現在の2資源制限と既存権限のまま、iPhone→独立原本読取→検証の完走まで準備完了とは判定できない。** 一般合成画像を1件追加する（3件目の資源）か、既存合成画像の共有変更が別途必要になる見通し。どちらも未承認なので実施しない。合成原本のparent/ACL条件も同時にレビューが必要であり、単にリンクを置いて合格としない。

private anchor/candidateとjournalを1 JSONへ収める実adapter、実列対応、Apps Scriptの実行project/trigger、検証専用のUUID→Actions入口も未接続。既存の本番dispatch／金融workflow／限定canaryを流用して埋めない。既存資格ファイルの存在だけで、Apps ScriptやGitHub Secretsの実行identity・許可scopeまで確認済みと扱わない。

## 実API CASの試験手順（未実行）

1. owner作成後のJSON_NEW ID、単一parent、owner＋既存SA writer、MIME=application/json、trash=falseを独立GETで照合。本文は合成probe専用で16 KiB以内、金融情報・医療・原本データを含めない。PUTの宛先はこの実ID1件に固定する。
2. Drive v2 metadataの強ETag T0とmedia bytesを取得し、metadata前後一致を確認。
3. `PUT https://www.googleapis.com/upload/drive/v2/files/<JSON_NEW>?uploadType=media&fields=id,etag`へ、`If-Match:T0`を付けた合成payload Aを1回送信。HTTP 200を記録し、独立GETでAのbytesとT1を確認。
4. **client側の事前比較で止めず、同じ実media更新APIへ**古いT0＋異なるpayload Bを1回送る。HTTP 412と、独立GETでAのbytesが変わらないことを実測。既存`DriveRegistryIO`のlocal precheckが発した文字列HTTP_412は、この実測に数えない。
5. 新しい同一ETagを使う異なるpayload C/Dを2 clientから並列に各1回送る。成功1件・412 1件、独立GETが唯一の勝者のbytesと一致することを確認。両方200、その他の曖昧な応答、弱ETag、不整合ならNO-GO。
6. 1回の送信後に応答を意図的に破棄するclient fault injectionを行い、PUT送信回数が1であること、その後はGET照合だけでPUTを再送しないことを確認。実ネットワーク障害とは分けて記録する。

HTTP adapterのretry・redirect・resumable再開・認証401時の暗黙再送も無効にする。Google `AuthorizedSession`の既定動作を無検査でwriteに使わない。通信結果不明時は同じ/別UUIDで再送せず停止。合格前に登録経路を有効にしない。

## CAS合格後のiPhone試験（未実行）

独立原本と実adapterの上記障害が解消し、別途承認された**合成ブックだけ**へ列・custom checkbox・限定triggerを設定する。event.value/oldValueはUUIDを検査するための最小情報だけを合成private journalへ記録し、editor emailや本番データを収集しない。

確認→必要な商品修正→原本照合→登録チェック1回→UUID受付→Python独立検証→同案件結果、の順に端末で確認。直後sort、行挿入/削除、別案件チェック、受付後編集、同UUID/別UUID競合、送信応答喪失と一時UNKNOWN→終端再照会を含める。iPhoneは利用者の実操作・観測結果が必要で、PC/Node成功をiPhone成功としない。

## 停止方法・今回の結果

- 作成前：いまここで停止。資源・共有・trigger・live dispatchは0。
- 作成後でもCAS不成立/不明：新しいbindingを無効のまま保持。既存folder/金融stateへrollback writeや削除をしない。
- 将来の限定triggerを停止する場合：承認で確定した新規trigger IDだけを削除し、新ブックの登録を無効化する。既存category/manual/定期triggerは対象外。UNKNOWNを消去して再送しない。資源の削除は別途承認。
- 実API CAS：未実施。iPhone実機：未実施。owner/SA metadata GETは成功/404を上記のとおり観測したが、CASの成功・失敗ではない。
- 実API CASとiPhoneが両方成立した場合のみ、次は本番接続の最小差分レビュー。それまでは本番NO-GO・Cloud Run不使用・金融write 0。
