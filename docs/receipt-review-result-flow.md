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

## 実資源の読取専用監査（2026-11-07追補）

PR #195〜#199は全てOPEN/Draft/未merge。#199の開始headは `63c556fd1e27c7559694faa5cd97e30f984ea61b`、remote mainは `fa7ac9378ef51e4a51258dd6053afe52ed0a2bb8` と照合した。AGENTS.md・iPhone仕様全文（第10節を含む）・商品別修正仕様を再確認した。今回は**この監査文書だけを追記**し、#199のコード・認証方式・writer・deployment bundleを変更しない。

接続済みDriveアカウントで検索／metadata／見出し／限定セルを読取り、既存ローカルservice accountではDrive/Sheetsのread-only scopeを用いた。環境の `SPREADSHEET_ID` が発見した管理ブックと一致することも、値を出力せず照合した。以下のM/U17/U22/Sは監査上の別名であり、新しい資源ではない。実資源ID・URL・メール・原本内容・商品値・下書き・メモ・秘密は本追補に保存しない。

### 接続判定

「可（読取）」は現在の調査資格での読取可否であり、hostの実行資格・登録権限・記帳権限を証明しない。「不可」は今回の条件では接続を進められないという判定。

| 接続対象 | 既存資源 | 利用可否 | 不足事項 | 次に必要な操作 |
|---|---|---|---|---|
| Sheets | M：環境設定と一致する管理ブック。U17：10月17日の採用済み1タブ合成ブック。U22：10月22日の登録接続合成ブック | 可（読取）／不可（#199登録接続） | Mに「確認」「解析候補明細」はない。U17/U22はownerのみのACLで、既存service accountでは404。商品別修正・要求UUID・原本version・snapshotの実保存対応も未確定 | 使用する既存ブックを明示し、必要なACLと保存欄を別途承認。既存入力・メモ・保留を移行／上書きしない対応を確定 |
| Drive | Receipt配下のinbox/processed、S：既存system-stateフォルダ、既存再取込記録／PDF registry／限定商品確認context | 可（既存資格で一部metadata/JSON読取）／不可（一般登録用anchor） | private caseは存在するが#199の一般案件catalogではない。同一版の総額確認・候補全体digest等の必要anchorがない。原本側には追加共有者があり#199のexact ACL条件と不一致 | 対象一般案件を既存の安定identityで独立照合し、専用anchorの保存先と作成手順を承認。原本bytes/hash/versionと用途を再検証。ACL検査を緩和しない |
| 認証ホスト | 既存private PDF設定のoriginと、10月13日監査資料の既存service URL（別表記） | 不可（現時点で利用可能と確認できない） | 両URLの `/health` と `/logout` はGoogle Frontendの503。設定originのcallbackへのOPTIONSも503。既存service accountのCloud Run一覧読取は403。現revision/IAM/Secret参照/OAuth設定は未確認 | 既存管理者資格で現service/revision/障害・origin/callbackを読取確認。復旧や既存hostのbundle/route変更が必要なら、具体差分と別承認後に実施 |
| private journal | Sと既存JSONはowner＋既存service account writerのACLで読取可。限定canaryのjournal参照も存在 | 可（保存領域の存在確認）／不可（#199の永続結果保存） | 確認した記録に `receipt-correction-results-only-v1` の独立結果領域はない。既存PDF/再取込/限定canaryのstateやauthorityを一般登録結果へ転用できない。host資格のアクセスも未確認 | authority・短期認証記録と分離された既存の承認済み空き領域を特定。なければ停止し、別途保存領域の承認が必要。CAS・期限後保持・独立read-backを検証 |

### Sheetsで実際に確認した対応

- **M**：全41タブのmetadataを確認した。「確認」は存在せず、`領収書確認` と `要確認` は別の旧画面。`解析候補明細` も存在しない。U17/U22をMの本番タブだと扱わない。
- **U17「確認」**：A＝案件ID、C＝外部原本、D＝確認状態、E〜I＝日付／店舗施設／総額／カテゴリ／医療支払方法、J〜L＝PDF下書き、M＝途中メモ、N＝確認理由、O＝入力点検、P＝原本hash、Q＝原本ID。先頭1行・1列固定。商品ごとの金額・数量・追加／除外、要求UUID、原本version、登録snapshotの専用見出しはない。
- **U22「確認」**：A＝案件ID、B＝操作依頼、C＝登録・結果、D＝種類、E＝外部原本、F＝確認状態、G〜K＝日付／店舗施設／総額／カテゴリ／医療支払方法、L〜N＝PDF下書き、O＝途中メモ、P＝確認理由、Q＝入力点検、R＝原本hash、S＝原本ID。先頭1行・1列固定。B/Cの限定読取では登録番号と、入力・原本等とのEXACT比較を行うCのIF式を確認した。**この式による「未記帳」表示は認証backendの受付／永続journalを証明しない。** Cを#199の結果欄として直ちに上書きしない。
- **M「領収書確認」**：A＝確認ID、B＝種別、C＝状態、D＝原本リンク、E＝確認する内容、F＝既存値、G＝候補（未確定）、H〜L＝医療入力、M＝判断、N＝統合先支出ID、O＝本人メモ、P＝反映結果。これは商品別修正入力欄ではない。`要確認` はA＝確認ID、G＝対象資料、H＝比較資料、L＝ユーザー判断、M＝統合先取込ID、P＝ユーザー備考、Q＝反映結果。これらの判断欄から登録処理を起動しない。
- **Mの既存データ**：`取込データ` A＝取込ID、C＝データ元、D＝元データID、K＝元データハッシュ。`レシート` A＝レシートID、F＝画像URL。`商品マスタ` A〜D＝商品ID／商品名／大・小カテゴリ。`カテゴリ自動分類ルール` A＝ルールID、B＝条件種別、G/H＝商品ID/名、J/K＝大・小カテゴリ、L/M＝承認元支出ID/日時、N＝revision、O＝有効。見出しの実在を確認しただけで、各商品の分類や承認内容の正当性を検証済みとはしない。

U17/U22のAと原本ID/hashは見える値であり、未信頼の下書き。原本version・private anchor・要求UUID・snapshotとの独立照合なしに、ソート／挿入後の対応を保証しない。U17のM、U22のO、および明細入力は読取のみとし、再生成や行番号への書戻しを行わない。Mの旧実装は `receipt_confirmation.review_id(kind, source)` でsource ID/version/hashから確認IDを作り、`ui_rows` でID欠落・重複を拒否し、private `confirmation_items.inputs` と照合する。ただし、そのIDは#199の `receipt:<source_id>` という取込IDとは別物で、金額・日付や行位置から結合しない。H301/G103等の旧保留も移植しない。

### Drive identity・保存境界の実測

Receipt配下の `receipt_inbox` と `receipt_processed` の存在を確認した。既存再取込manifestのfolderはprocessedと一致し、manifestの管理ブックbindingもMと一致した。inbox/processedとMには追加ユーザーの共有がある。U17/U22はownerのみ。**#199 `IndependentReceiptReader.checked_metadata` のowner＋指定service-account writerのみという条件に、そのまま合格する構成ではない。** 正当な共有を勝手に削除せず、別の資格を同一service accountとみなさず、exact ACLを緩和しない。

S・PDF registry・再取込記録・限定商品確認contextは既存service accountでmetadata/JSON読取200。原本identityを持つprivate case記録は存在するが、確認したschemaは次のように異なる。

- 再取込記録：`manifest`／`records`／`confirmation_items`。sourceはID/hash/version/MIME/classification等を保持。`confirmation_items` に#199の `total_confirmation` はない。本人が会話で確認した総額を、信頼済み原本版付き記録へ自動昇格させない。
- PDF registry：`anchor`／`generation`／`pages`／`schema`。一般レシートの `cases[case_id]` catalogではない。private設定のspreadsheet bindingはMに一致した。
- 限定商品確認context：`config`／`owner_actor_id`／`schema`。configはcandidate/journal/drive/scopeを限定している。既存p14等のcandidate／journalの実在は、通常案件の修正保存・登録許可を意味しない。

既存の一般分類private caseの原本1件について、metadataのsource ID一致、version、単一parent、SHA256/MD5 checksum、Drive v2の強ETagが取得できることを確認した（原本はPDF）。原本bytesは取得せず、保存hashとの完全一致や現在版の登録適格性は未検証。取得手順は、Drive IDをprivate sourceから取得→metadata/version/ACL/parent→既存read-only bytes readerでSHA256→metadata再読取。解析hashと原本bytes hashは区別する。metadata checksumや表示リンクだけで#199のbytes検証を置き換えない。

Drive connectorのmetadata応答は正規化されversion/checksumが返らなかったため、既存service accountの直接read-only APIで有無を確認した。結果保存で必要な強ETag/CASは既存 `DriveRegistryIO` 等の契約を再利用できるが、今回CAS書込みやjournal初期化は実行していない。Sの一覧・上記既存記録で汎用の専用結果領域を特定できなかった、という調査範囲の結論であり、Drive全体に存在しないとの断定ではない。

### 認証ホストの現状と最小差分

既存private PDF設定のoriginと過去監査資料のservice URLをそれぞれ確認し、HTTPS GETを実行した。両表記が同じserviceを指すことや現在のtraffic/revisionを、503応答から断定しない。過去資料は10月13日時点のもので、READMEのbaseline revisionを含め**現在の稼働証拠にしない**。503の原因は未特定。callbackへのGET/OPTIONS応答も503であり、実Google OAuth・callback・shared sessionは今回試していない。Cloud Run構成読取403を受け、IAM付与・別資格の作成・API有効化・deployには進んでいない。

コード上は既存 `/start` → POST `/oauth/callback` → `/confirm` → `/result`、shared loginと `/logout` があり、callback URIは `HGA_ORIGIN + /oauth/callback`。実OAuth clientの登録済みcallbackと同一かは未確認。#199の最小接続差分は、既存runtimeのpurpose/profile選択へ専用gateway/reader/journalを渡し、認証後の遷移を既存Blueprintの `/receipt/register/<UUID>`、結果／再ログイン後の戻り先を `/receipt/result/<UUID>` に接続し、必要な依存閉包だけdeployment whitelistへ含めること。ブラウザから案件データ・actor証拠・private anchorを採用しない。汎用seeding endpointや新認証・新server・新writerは不要という方針だが、実ホストへ無変更で接続可能という意味ではない。

展開前に、既存管理者による現revision/image/traffic、runtime service account、`HGA_ORIGIN`／`HGA_DATABASE`／`GOOGLE_CLOUD_PROJECT`、既存 `HGA_CONFIG_PATH`／`HGA_OWNER_PATH`／`HGA_SESSION_KEY_PATH` とDrive設定参照、実OAuth callback/owner、既存Firestore request/ticket/loginのIAM・TTL、および専用結果領域の保持／CAS権限を読取確認する必要がある。Secretの**値**は取得／出力しない。現在のservice accountにはCloud Run一覧読取が許可されず、deploy資格・Secret access・runtimeの新領域アクセスを確認できていない。既存限定canary権限の転用やauthority・validated SHA・NO-GOの変更は、この接続に含めない。

### 最短構成と次回の最小作業

採用候補は**既存「確認」1タブ＝下書きの読取元、登録と結果＝既存認証画面**。これなら実Sheetsへの結果write・式置換・ソート中の行競合を初回接続から除ける。認証後「この内容を登録」1回、結果は現在の認証済み登録者がprivate journalから照会する。入力・メモを保持し、600秒経過後は再登録せず照会する。実ブックの対象リンクは未設定で、端末の操作回数も未計測。

次回にまとめて進めるための前提は、(1)既存hostの稼働／実行資格とOAuth設定、(2)使用する既存ブック・修正保存欄・正確なACL、(3)一般case anchor／専用永続結果保存領域の3点が特定・承認されること。承認後の最小実装は、上記実見出しに基づくreader mappingとanchor対応、既存hostのpurpose/callback戻り先・Blueprint/依存閉包、登録リンクの接続に限定する。未解決の一般商品修正の保存欄は既存列への推測割当をしない。列や入力UIを変える場合は理由・保持方法・範囲を示して事前承認する。

既存の専用空き領域が見つからない場合、新ファイル等は今回作らず、保存領域について別途承認が必要。そのため**現状では「次の1回で実機完走できる」とは約束できない**。前提が揃った後に、既存資源だけの無金融write試験で本人／別ユーザー、原本・下書き変更、期限、再送、期限後結果照会、UNKNOWNを確認し、iPhoneでSheets→原本→既存認証→登録1回→結果まで検証する。Sheetsへの結果反映と金融writeは初回接続の必須条件にしない。

**停止判定**：実登録経路・private保存先・ACL・host稼働が未確定のため、接続実装へ進まない。今回の実測は資源読取と副作用のないHTTP確認のみ。#199の71件／既存認証363件／回帰67件は前回の合成結果で、今回再実行していない。実Google認証・iPhone実機・永続CAS成功・金融write成功の証拠にしない。今回の変更はMarkdownのみで、差分・whitespace・秘密情報混入を確認する。

本番Sheets／台帳／原本・設定・Variables/Secrets・authority・validated SHA・mainは未変更。資源作成・権限変更・dispatch・金融write・原本移動・AI送信・host deployは0。NO-GO維持。
