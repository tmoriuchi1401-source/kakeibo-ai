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

## iPhone実機への接続候補と承認対象（2026-11-07追加）

今回の対象を**U22＝10月22日の既存「登録接続・iPhone受入」合成ブック**に固定する。M/U17、本番原本、既存金融処理は対象外。PR #195〜#199はOPEN/Draft/未mergeのまま、#199 head `7f9fae209455f0ac3b0f6e036df9d56a215fa3ce` と既存コードを再確認した。以下は具体的な変更提案であり、実施承認や接続完了ではない。今回は文書のみ変更する。

### 今回の限定再確認

U22は「確認」1タブ、先頭1行・1列固定。ACLはownerのみ、接続アカウントは共有操作可能。Sも同じownerで、owner＋既存service account writerだけのACLを維持している。既存service account自身によるSのmetadata読取は200、`canAddChildren=true`、そのwriterが当該credentialと同一であることを値の出力なしで照合した。U22の一般合成行が参照するPNG原本もownerのみ。**既存原本の共有変更を伴わずに使える構成ではない**ので、下記の合成コピー案とする。

既存設定originと過去資料URLの `/health` は今回もGoogle Frontendの503。既存service accountのCloud Run一覧読取は403。管理者経路として既存Chromeへ接続を試みたが、Computer Useのapp接続承認がタイムアウトし、Cloud Consoleを読めなかった。ローカルの通常PATHにgcloudはなく、標準の既存gcloud認証ディレクトリも見つからなかった。**管理者による現revision・503原因・runtime資格の確認は未実施**であり、403解消や復旧方法を特定したとは報告しない。Secret値の取得、別資格の作成、IAM付与、復旧・deployは行っていない。

残す管理者調査はこの1サービスに限定する。既存管理者セッションで、Ready条件／traffic先revision、runtime service account、Secret参照名を含む設定metadataとorigin/callbackの対応、503に関係するplatformの起動・準備失敗理由を確認する。アプリの生ログやSecret payloadは取得しない。既存service accountへCloud管理権限を付与して403を消す案は採用しない。原因が未確定なので、復旧・deployを包括承認の対象にはしない。

### 共有変更は1ブックへの1grant

変更先はU22だけ。追加する相手は**Sの現writerと同一の、既存 `GOOGLE_SERVICE_ACCOUNT_FILE` の `client_email`**。ownerは維持し、親フォルダ・M・原本・他ユーザーの共有は変えない。

Drive/Sheetsの読取APIだけならreaderが候補になるが、#199の `checked_metadata` はowner＋指定service accountの**writer**というexact ACLを要求する。コードをそのまま再利用する最小差分は、U22へこの1件のwriter grantを追加すること。閲覧者を付与しただけで合格とはしない。writerは合成ブック全体を編集できる強い権限なので、承認が必要。実行コードはSheets read-only scopeとし、Sheetsへの結果write・編集trigger・金融writerを持たせない。readerへの設計変更を希望する場合は別のコード／安全性レビューとなり、本提案では実施しない。

hostのruntime service accountがこの既存資格と同一か、既存のDrive資格参照を使う構成かは管理者確認待ち。別のservice accountを同一扱いしたり、追加の共有者を黙って許容したりしない。共有後にexact ACL・version・metadata前後読取を再確認し、一致しなければ停止する。rollbackは今回追加したU22のgrantだけを除去する。

### private分離：既存Sの中へ専用4ファイルを提案

SのACLとmetadataから、**別ファイルとして分離する配置は可能な候補**と判定する。既存ファイル内へキーを追加する方式は、既存schema／generation／金融stateと競合するため採用しない。使ってよい空の専用ファイルは依然特定できていない。したがって新ファイル作成は承認前に行わない。提案するB方式の初回試験は、次の4ファイルに限定する（名前は未作成の提案名）。

| 提案ファイル | 内容・境界 | 更新権限／保持 |
|---|---|---|
| `receipt-correction-synthetic-source-v1.png` | U22で確認した完全合成PNGのコピー1件。既存原本の内容・ACL・リンクを変更しない | ownerが既存接続でSへコピー。固定後はbackend読取のみ |
| `receipt-correction-synthetic-cases-v1.json` | 新しい合成案件1件の表示ID→取込case ID、コピー原本ID/bytes hash/version/用途、保存済み候補・取込行のdigest、合成カテゴリ／商品ルール、同一版の総額確認記録 | 初期設定はowner。通常処理は読取のみ。Mの実データや限定canaryのanchorをコピーしない |
| `receipt-correction-synthetic-drafts-v1.json` | Bの未登録商品修正だけ。case/source/登録者、revision、operationsをCASで保持 | 現在の認証済みowner＋編集用Origin/CSRF。書込みは専用draftのみで、金融許可を作らない |
| `receipt-correction-synthetic-results-v1.json` | #199 `ResultJournal` の専用scope/envelope。UUID・actor・原本版・snapshot・検証状態 | 既存CAS／独立read-back、600秒後も保持、UNKNOWNは再送しない |

作成主体は既存owner接続とし、service accountを新ownerにする作成方法は採らない。作成後は各ファイルのowner＋既存service account writer、S parent、MIME、trash、強ETagを検査する。新共有者・新フォルダは追加しない。この配置はまだ作成／書込／耐久性実証済みではない。既存Drive v2 If-Match/read-backと#199のportを接続し、PDF専用schemaや限定canary scopeを偽装して流用しない。既存Firestoreの短期request/ticket/loginはその用途を維持し、結果journalと混在させない。

合成原本のコピーによりIDが変わるので、新規合成caseとしてanchorを作る。旧行の入力・総額確認・authorityを移植しない。U22の既存8案件を変更せず、新しい検証専用1案件を追加する承認案とする。新sourceのbytes/hash/version、候補・カテゴリ等はownerが合成fixtureから初期化し、独立readerが照合する。既存実原本の追加共有・移動・AI送信は不要。

### 商品修正A/Bの最小変更比較

ここでのBは**既存認証ホスト内の編集画面**であり、仕様書の旧「Apps Script B方式」を新設する案ではない。Sheets「確認」1タブを入口として維持するが、商品編集場所の変更は未承認。AGENTS.mdおよびiPhone仕様10.5に従い、実装前に承認を得る。

| 観点 | A：Sheets内で商品明細編集 | B：Sheets入口→既存認証画面で編集・登録（推奨） |
|---|---|---|
| 最小UI差分 | 1行1案件を保つため、右側へ商品ID/名称/数量/金額/除外/税値引き根拠等の反復列と追加枠が必要。現在の19列に商品表はない | 既存8行と列配置を保持。新検証1行に入口リンクを設け、既存hostに商品フォームを追加。結果はhostで表示 |
| backend差分 | Sheet→operations parser、可変商品枠・不足時hold、候補IDとの対応、前後再読取。#198の固定snapshot/POSTと#199のreader構造を最も直接再利用 | 認証済みprivate draft保存、draftの独立reader、編集後snapshotの再固定が追加で必要。#199のHTMLは現在表示専用で、入力保存は未実装 |
| 操作見積り | 商品セル編集を除き、リンク→既存開始/続行→固定内容を登録＝3操作 | 商品入力を除き、リンク→既存開始/続行→修正内容を確認→固定内容を登録＝4操作。登録POSTは1回、previewが1操作増える |
| iPhone入力 | 複数列のセル選択・横移動。商品追加／除外や数量の対応を見失いやすく、旧PDFカードの複数行UIをそのまま移植できない | 1商品ずつ縦に名称・数量・金額・除外を編集。根拠付き追加、税／値引き、商品別分類、合計・差額を同じ画面に置ける |
| 入力保持 | Sheets同期を利用するが、同期済みの独立確認・ソート安全性は別途必要 | dedicated draftへCAS保存し、保存済み表示と再読取で復元。未保存／通信断／UNKNOWN時の原本遷移は止め、入力を画面に保持。未保存値の強制終了後復元は保証しない |
| 安全性 | Sheet値は未信頼、row numberや編集者を本人証拠にしない。並替え／列ずれ／同時編集を独立検査 | browser値も未信頼。現在login/ownerと編集CSRFを確認して下書き保存し、登録時はprivate再読取と原本／候補を独立照合。行書戻し競合を除ける |
| 共通再利用 | #196商品修正preview/税・値引き/商品分類、#198 OIDC/owner/session/Origin/CSRF/UUID/600秒、#199結果journal/照会を利用 | 同左。既存認証・金融writerを新設しない。登録purposeを記帳authorityへ昇格しない |

操作数は既存開始画面を維持する設計見積りで、Google側のログイン操作・原本往復・商品ごとの入力は別。実機での測定値ではない。Aの初回private追加はsource/case/resultsの3ファイル、Bはdraftを加え4ファイル。Bがコード行数でも小さいとは主張しない。

実装単位で比較すると、共通差分は実reader binding、既存runtime/profile/callbackとdeployment whitelist、結果照会の接続。A固有は「Sheetの商品列生成・入力規則」「列からoperationsへのparserと同時編集照合」の2単位。B固有は「商品フォーム／編集用POST」「private draftのCAS保存・独立reader」「previewでの新request再固定」の3単位を想定する。Bは `auth-session/receipt_correction_web.py` と `receipt_correction_io.py` を主な追加先とし、#196の修正validator・#198のOIDC/最終登録validatorは再利用する候補。両方式とも合成契約の追加は必要。未実装のため行数・工数の確約はしない。

**推奨はB。** Aは認証コードの差分が少ないが、商品数に応じた横長の列・追加枠・入力規則を合成ブックへ新設する必要がある。Bは下書き保存とpreviewの追加を明示的に引き受け、既存1タブと入力・メモを保持しながら、可変商品をiPhoneで編集しやすい構成にできる。既存ブックのCのIF式をbackend結果と混同せず、初回はhostの結果照会だけにする。

### Bで固定snapshotを維持する接続経路（未実装）

`U22の新検証行リンク → 既存start/OIDCまたはshared login → 商品編集 → private draft保存 → 「修正内容を確認」POST → 新UUID/600秒の固定snapshot → #199の「この内容を登録」POST → #196検証 → private結果journal → 同じhostの結果／期限後照会`。

初回認証は既存の対象を限定したprofile／署名付き要求を使い、登録前の編集権限と金融権限を分離する。公開の汎用seed endpointを新設しない。GETではrequest/CASや登録を開始しない。編集用CSRFは現在loginとcaseへ、最終登録CSRFは新UUIDと固定snapshotへそれぞれ結合する。別purposeのCSRFや古いactor証拠を転用しない。

編集値のCAS保存後に、原本・候補・draft・分類を独立再読取し、previewから新しい#198 requestをprepareする。既存SharedLoginsで**新bindingへ**登録者証拠を結び直し、#199の固定内容画面へ移る。編集前の認証が変更後snapshotを承認したとはみなさない。長い編集を600秒の登録期限へ含めず、期限は固定後に開始。固定後の編集・別タブ・同時更新は登録を拒否し、新previewが必要。final POSTの現在の `csrf + snapshot_digest` 契約は維持し、そこへ商品編集値を直接追加しない。

下書き保存は金融処理を呼ばず、UNKNOWNではCAS／登録を自動再送しない。Safariの原本往復前に保存成功を表示し、復帰時はcase/source/draft revisionを照合して復元する。Sheetの既存入力・O列メモは読み書きしない。今回のBの保存／再固定はまだ実装・合成テストされておらず、#198/#199の過去の71件で成功済みと扱わない。

### 次に承認すべき操作と停止点

| 順序 | 具体的な操作 | 影響・rollback／今回の状態 |
|---|---|---|
| 0 | 既存Chrome管理者接続を利用可能にして、上記1サービスのmetadata／platform失敗理由を読取 | 作業自体は今回依頼済み。app接続承認タイムアウトで未完了。新IAM・Secret値・修復を要求しない |
| 1 | U22へSの現writerと同一の既存service accountをwriterとして1件追加 | U22だけに編集権限が増える。削除でrollback。未承認・未実施 |
| 2 | S内に上記4ファイルをownerとして作成し、完全合成の新caseだけ初期化 | 既存private記録は非変更。解除は新bindingと入口の無効化、既存データを削除／上書きしない。未承認・未実施 |
| 3 | Bの商品編集／draft保存／preview再固定をDraft上で実装・合成検証し、U22へ新検証1行の入口だけ追加 | 既存8行、列見出し・固定・メモ・原本リンク・C式・保留を保持。追加入口を無効化してrollback。UI方式変更を含むため未承認・未実施 |
| 4 | 管理者確認で判明した必要最小限の既存host復旧と#199/Bのbundle/profile/callback遷移の展開 | 現revision／原因が未確認のため具体的な復旧差分は未確定。**この行への包括承認では実行しない**。変更対象・現/新revision・traffic・検証・rollbackを示して別承認 |

承認対象は上記の範囲に限定し、別ブック・追加Cloud基盤・本番原本・通常金融登録へ拡張しない。0の確認ができるまで復旧／deploy差分の確定は保留する。1〜3も今回は実行しない。承認後の検証は、新caseの本人／別ユーザー拒否、原本・draft変更、二重登録、期限、期限後結果、通信断/UNKNOWN、iPhone原本往復と保存済み入力の復元に絞る。金融writeは全経路0が条件。

**今回確定したのはU22・共有1grant・S内4ファイルの分離案・B推奨・承認範囲。管理者によるhost現状確認と実接続の確定は未完了。** 新たな監査範囲は増やさず、その未確認点で停止する。文書差分／whitespace／秘密情報混入のみ検査し、コード・合成テストの再実行・実Google認証・iPhone実機検証は行っていない。main／本番Sheets／台帳／原本／authority／validated SHA／本番フラグは未変更。共有・資源作成・復旧・deploy・金融write・原本移動・医療AI送信は0。NO-GO維持。
