# 一般レシート：独立reader・登録画面・期限後結果照会（合成）

2026-11-07。PR #195〜#198、AGENTS.md、iPhone確定仕様全文、商品別修正仕様を確認し、#198 `11509bf9bfc274b85dfb5b03b3e7de1d57345a03` から独立ブランチを作成した。

**到達点は合成adapterと既存appのtest client上での、認証済み登録→独立検証→結果保存／再照会。iPhoneから実環境で利用できる完成版ではない。** 実データの承認済みbinding、1タブの具体的な列対応、既存hostへの新purposeのdeployは確認できないため、その実接続で停止し、検証済み部分だけDraft保存する。NO-GO維持。

## 実装前に示した画面と操作

既存認証appに、案件・原本版・修正後明細／商品カテゴリ・原本リンク・結果を表示する未展開のBlueprintを追加する。既存のscreen（viewport／幅420px／折返し）、暗号化ticket、OIDC／shared session、HTTPS／Origin／CSRFを再利用し、新しいサーバーや認証方式は作らない。

想定操作は「確認」1タブの対象リンク→既存認証の開始／続行（初回はGoogle側の操作も必要）→明細を確認して**「この内容を登録」を1回**→同案件の結果表示。認証後の登録ボタンは1個。過去結果は現在の有効なログインで読取専用表示し、再登録しない。実装前にこの変更範囲と操作数を報告した。リンクを開く／認証開始までを含めた総タップ数が1回という意味ではない。

実Sheetsのレイアウト、タブ、行、リンク、入力・メモは変更していない。G103／H301を維持。既存 `/start`／`oauth/callback` の実host profileから今回の登録routeへ振り分ける処理は未接続であり、合成試験では既存認証coreで作成した暗号化ticket／sessionを用いて、新routeのHTTP GET／POSTを実行している。実端末の操作回数を実測したとは扱わない。

## readerとprivate入力の境界

既存 `app/google_clients.py` のread-only Drive／Sheets client、`download_drive_file`、`SheetsDB.get_raw`、`DriveRegistryIO` のprivate ACL／metadata前後読取、既存の `strong_etag` とCAS契約を調査した。実credentialを開かず、同じread-only portへ接続できる `IndependentReceiptReader` を合成実装した。

- 入力portは原本metadata／bytes、Sheetsの取込行・候補明細・「確認」の下書き・カテゴリ／ルール、protected private case記録。ブラウザPOSTからこれらを受け取らない。
- 案件IDは安定IDで探索し、重複／欠落なら停止する。原本ID/hash/version／一般購入という用途と総額確認記録はprotected case anchorから取得する。原本版が一致する総額確認を再要求しない。
- 保存済み候補の行全体digestと取込行digestをprivate anchorと照合する。候補の解析hash列だけでは内容の真正性を証明したとしない。未紐付けの旧案件を推測で移植しない。
- owner＋既存service-accountの正確なACL、resource ID・MIME・trash状態・版、原本の許可folderを検査する。原本bytesのSHA256を確認する。private強ETag、metadata、Sheets値、case記録を前後で再読し、同時編集／移動／ACL変更／読取失敗は停止する。cacheや行番号を恒久identityにしない。
- 現行のカテゴリ／承認済みproductルールを既存parserで読み、PR #196/#198のsnapshot検証へ渡す。

**portの合成検証であり、実Drive／Sheetsを読み出していない。** private case anchorと実「確認」列を結ぶadapterは未承認／未接続。既存の本番case記録がこのschemaで保存済みとは仮定しない。実接続時は原本privacy判定・本人確認済み総額の由来を含む既存記録を確認し、一致しない案件はholdする。

## 登録と期限後結果照会

PR #198のgatewayは既定動作を維持し、結果専用journalを渡したときだけ、明示POST後の「受付済み」→「検証中」→「修正が必要／検証済み・未記帳」をCAS・独立read-backで保存する。認証・原本・snapshot検証は緩和せず、既存authority／writer／dispatchを呼ばない。

`ResultJournal` は既存 `AuthRequestStore` のversioned CAS／read-backを専用scope・schemaで再利用する。transportはauthority・銀行state・Sheets・600秒auth記録と分離された、既存private journalの承認済み領域でなければならない。今回その実領域を作成／変更していない。容量上限に達したときは停止し、過去記録を自動削除しない。

結果には登録者actor ID、要求UUID、案件・原本版・固定snapshot、検証結果を保持する。JWT・cookie・CSRF・原本bytesは保存しない。checksumは検証登録receiptであり記帳許可ではない。受付後の応答喪失・claim済み・UNKNOWNは再実行しない。不確実なjournal書込みを再送／無条件上書きせず、正確な読戻しでのみ照合する。

`/receipt/result/<UUID>` は古いoperation ticketを使わず、既存 `SharedLogins.current` で**現在の照会者**を認証し、登録者actor IDとの一致を検査する。600秒のauth記録削除後も、新しいResultJournalインスタンスから過去結果を照会できる合成契約を検証した。既存ログイン自体が失効・失効操作済み・改ざんの場合は拒否し、既存Google再ログインが必要になる。再ログイン開始導線の実host接続は未実施。

過去結果の表示は登録時の原本版／snapshotに対する結果であり、現在の原本や下書きを新たに検証した表示ではない。GETによるjournal更新や金融writeはない。残った受付／検証中の記録が要求期限を過ぎた場合、読取結果を「結果不明・照合待ち」と表示するだけで、処理を再送しない。

## 同じ案件への反映

`project_result` は「確認」1タブの論理行に対する純粋な結果projection。案件ID・要求UUID・原本版・登録snapshotを照合し、一致する一意の案件だけへ結果欄を返す。行番号を使わず、別案件・重複・新しい要求との混同を拒否する。明細・メモ・他案件は変更しない。受付後に明細を編集していた場合は結果欄へ「未登録の変更あり」を付け、旧結果を現在の下書きの検証成功と扱わない。

**実Sheetsへ書き込む処理はない。** ソート中の実セル書込み／列対応が安全と確認できていないため、実「確認」への反映はこのprojection契約までで停止する。認証画面側には同案件の結果をHTTPで表示する。

## 検証と実機の区別

新runner合計 **71成功・0失敗**（PR #198の39件＋今回の32件）。正しい署名／本人と別ユーザー拒否、原本・入力・ACL・metadata欠落・同時編集、重複／再送、期限切れ、CAS競合、破損journal、UNKNOWN、同案件projection、入力保持、長期journal未接続の設定拒否を検証した。

期限後再照会は、600秒経過後に短期auth記録とoperation cookieを削除し、別ResultJournalインスタンスから読取。登録前・登録中・修正必要・未記帳・UNKNOWNの表示、他登録者への開示拒否、未認証／失効loginの拒否も検証した。未知の書込み結果は「未commit」と「commit済み応答喪失」の両方を注入し、追加登録0、独立読戻しによる照合だけを確認した。

既存認証契約 **363成功・0失敗**、#196／既存bundle回帰 **67成功・0失敗**。compileall・diffチェック実施。Secretsなしの既存CI runnerが新しいfixtureも実行するようにした。本番workflow、workflow_dispatch、deployment whitelist、認証baselineは変更していない。main以外をbaseとするDraftの間はCI自動起動対象ではなく、ここに記載したものはローカル合成結果である。

iPhone操作性は既存screenのviewport、幅420px、折返し、認証後の登録button1個、HTTPS・no-store・XSS escapeのHTML／HTTP確認まで。実Google token交換、実Firestore／Drive ACL、実private journalの耐久性、実Sheets、実iPhoneは未検証。Flaskのtest clientだけを使い、listenする新サーバーを起動していない。

再現：既存合成テスト依存（Flask／Firestore等）とrepository外TEMP/TMPを使用し、`python auth-session/run_receipt_registration_checks.py`、`python auth-session/compose.py --test` を実行する。

## 最小残作業と停止範囲

1. 承認済みの実データbinding・private case anchor／結果領域・「確認」の列対応を確認する。なければ実読取・書込み・新規資源作成を行わない。
2. 既存hostのprofile／既存OAuth callbackから登録専用routeへの接続をreviewし、deployment whitelistを含む実host変更は別途承認後に行う。新規host・新認証・新writerは不要という方針を維持するが、未承認のdeployを代行しない。
3. 実環境でmetadata前後読取／ACL／同時編集／列ソートの安全性を確認し、実Sheetsへの結果欄だけの反映を接続する。入力・メモや別の要求を上書きしない保証が得られなければ停止。
4. 承認済み環境で実Google認証・iPhoneの原本往復／登録1回／過去結果照会を確認する。金融writeは次工程のまま、NO-GO・authority・validated SHA・既存金融処理を維持する。

main、本番Sheets・金融台帳・原本、Variables／Secrets、validated SHA、authority、銀行等の運用設定は未変更。本番dispatch・金融write・原本移動・AI送信・新規Cloud資源・既存host deployは0。
