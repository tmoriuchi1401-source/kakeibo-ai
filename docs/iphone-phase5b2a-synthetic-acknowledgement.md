# Phase 5B-2A：本人再確認・明示POSTの合成受付

2026-10-18。完成範囲は **原本・全明細・入力の再表示、必要時編集、暗号化CASへの下書き保存、既存Google/session/CSRFを経由した合成本人確認受付**。会計記帳、PDF構造の実適用、native authorityは含まない。

## 継承と変更範囲

mainのAGENTS.mdとiPhone継続仕様全文（第10節含む）を確認。#123 → #124 → #127 → #128 → #129 → #130 はすべてOPEN/Draftで、開始時のhead/baseをprivate baselineへ保存した。#130の `ebb2e970a52aa55295a11ff61609fb92fb61a021` から独立ブランチ `codex/phase5b2a-synthetic-confirmation` を作成。新Draft PRのbaseは `codex/phase5b1-draft-foundation`。既存6件の変更・main mergeなし。

Google Sheets「確認」1タブ、列、保護、入力規則、承認済みUI、既存検証ブックを変更していない。新しいDrive資源、公開URL、実機アクセス用環境、Cloud資源を作成していない。

`make_isolated_confirmation_application` はPhase 5Aの既存read-only factoryの後に明示注入する合成composition。既定OFFでは新namespaceを登録せず、明示ONでも `isolated_synthetic` 設定と既存memory-only runtimeが必須。既存ホストの認証済み詳細に、ONの場合だけ新詳細へのリンクを表示する。既存GETはread-onlyのまま残す。本番factory、production workflow、auth-session baseline/patch、native validator/writerは無変更。

## 利用フローと画面

1. 認証済み一覧から案件詳細を開く。案件ID・種類・purpose、原本リンク、ID/hash/Drive revision/numeric version/native version、ページ・Unit・receipt index、入力snapshot・対象revisionを表示する。
2. 一般は各明細の旧値・提案値（名前/金額/カテゴリ）、原本との対応、未入力/入力済み/保留/対象外、明細メモを表示。必要な編集だけ行い途中保存する。レシートカテゴリの全明細への適用は行わない。
3. 医療は完全手入力。日付・施設・実支払額・カテゴリ・支払方法・メモを既存医療validatorで確認する。AI/OCRへ送信しない。
4. PDFは既存native proposalのグループ別ページ構成・構造revision・proposal digestを表示し、入力snapshotへ含める。既存構造validatorと現在の保存済みproposalをPOSTで照合する。構造の実適用・会計許可へ変換しない。
5. 保存成功後に正確な保存済み内容をGETで再表示する。「本人再確認」では原本・全明細・旧/提案・状態・メモ・確認対象を再表示し、確認チェック後の明示ボタンで受付する。
6. 受付結果には対象案件・purpose・確認した入力・原本snapshot・revision・要求IDを保存する。結果画面は受付時点の内容を表示する。現在の編集内容が変わっても、過去の受付内容を置き換えない。

G103/H301は保留表示だけ。H301に原本/Unitを推測して付与しない。両案件に編集/確認フォームや有効な確認proofを作らず、別案件のproofを付け替えたPOSTも拒否する。

フォームの全体表示で保存済み/未保存を区別する。原本は別画面へ開き、編集DOMを維持する。未保存のまま再確認へ進む操作は止める。競合や送信失敗ではフォームを置き換えず、入力・メモを保持する。通信結果不明時は同じ要求IDの結果確認リンクを表示し、自動再送・自動の新ID発行をしない。

送信中の追加入力も保持する。保存成功が判明した場合に限りGETで新しいproofを取得し、読戻しinput digestが自分の保存結果と完全一致した場合だけ、追加入力を明示的に再保存できる。他instanceの変更が入った場合や読戻し不能時は自動mergeせず、追加入力を残して停止する。ブラウザー再起動をまたぐ未保存DOM保持は保証しない。保存済み値はrepositoryから再取得する。

## 既存コードの再利用

| 境界 | 再利用 |
|---|---|
| 本人認証 | hash固定既存Flask host、Google issuer/sub、ExistingHostSessionGate.current/read/post、既存SharedLogin actor、browser ticket・CSRF・600秒request TTL |
| 保存/競合 | Phase 5B-1 PersistentJournal/EncryptedDraftIO、既存FirestoreConditionalStateのupdate_time precondition、strong ETag、exact fingerprint、read-back、容量停止 |
| 一般 | iphone_item_drafts.issue/inspect、receipt_item_review.validate/card/check_snapshot/evaluate/snapshot。全明細identity/順序/カテゴリ/金額/合計/構造Gateを維持 |
| 医療 | medical_fields_onlyで部分入力を区別し、完成時はpreview_medical → 既存ReceiptConfirmationPort.validate_draft/_parsed/_planで厳密dry-run |
| PDF構造 | preview_structure/validate_observation、既存native `_valid_proposal`、既存DriveGroupingStore.loadのread-only照合。review/confirm/applyを呼ばない |
| 原本・native snapshot | Phase 5B-1 SourceObservation/revalidate、既存stable page identity、Case/canonical/native snapshot digestの固定 |
| GET | Phase 5A SavedDraftRepositoryと認証を継承。保存snapshotだけから表示する |

一般の商品名・金額の修正も、既存recordが許可する範囲に従う。カテゴリや金額を変更して入力整合性・構造確認が不足すれば部分保存はできても受付不可となる。新しい本人チェックで既存nativeの構造Gateを迂回しない。

## 明示POSTの契約

新namespaceは `/isolated-synthetic/iphone-confirmation`。一覧、詳細、再確認、受付結果、UI scriptはGET。変更入口は案件詳細の `/cases/<cid>/request` へのJSON POSTだけである。

- actionは `save_draft` と `acknowledge_snapshot`。purposeはサーバー側canonicalから取得し、browser申告と完全一致させる。ACCOUNTING_POSTという確認目的でも実行action `post` は存在しない。
- GETで専用鍵によるstateless暗号化proofを作る。journal/認証DBへのwrite、TTL延長、native観測はない。proofはserver canonical、actor hash、既存session/browser ticket、期限、案件/purpose/action、expected revision/strong tag、exact input digest、baseline/source observation digest、generationに固定する。
- request IDはこの固定情報から決定し、同じsession・同じ対象の二重タップは同じIDとなる。暗号文nonceが違っても同じclaimsは同じ要求として照合する。
- POSTではGoogle issuer/subから導出されたactor・owner・案件owner、有効session/ticket、Originと既存ticket CSRF、600秒request TTLを確認する。HTML formは同一Originのfetchで既存gateが要求するX-CSRF-Tokenを付ける。
- ブラウザーは金額・カテゴリ等の編集intentを送れるが、原本URL、source version、purpose、digest、native token/authorityを自分で定義できない。明細編集の順序付きitem IDをserverの既存identity/token/markerへ対応させ、別原本・追加/削除/入替・重複を拒否する。
- 編集時と受付時に既存native validatorを呼ぶ。Case/native snapshotは初期digestへ固定。原本はtrusted readerでrevision bytes/hash・Drive/native version・page/Unitを再検証する。PDF proposalの現在revision/構成もread-onlyで照合する。
- baselineの版対応を再検証したうえで、host編集後のexact input digestで原本を再観測する。保存するsource snapshotはPOST時の観測時点と現在のinput digestを持ち、古い観測日時やSheet入力digestを現在のものとして転用しない。
- 下書き保存でrevision/inputが変われば以前の再確認proofは無効。送信対象は再表示したexact snapshotと一致しなければならない。チェックボックスの明示intentも必須。実際に人が原本を目視したことを技術的に証明したとは扱わない。

合成受付は既存暗号化document内の独立 `confirmation` envelopeへ保存する。旧LocalJournal形式とPhase 5B-1 batch/detailsは維持し、新envelopeは同じCASに含める。下書き履歴・受付結果・request fingerprintを保持する。旧8案件のread-only表示は引き続きSheet保存snapshotを示し、新詳細がhost側編集の最新保存内容を示す。

受付schemaは `synthetic-owner-acknowledgements-v1`。synthetic_only=true / native_authority=false / accounting_permission=falseを固定。native token、sealed proof、operation target/identity、認証資格情報を受付recordへ保存しない。OperationRequest/PurposeRequestとしてもparseできない。recordは今後のnative authorityへ自動変換せず、5B-2Bで別の本人・原本・台帳・入力検証が必要。

同一ID・同一内容は保存済み結果のread-backだけを返す。内容違いは拒否。異なるID/sessionから同じsnapshotの確認が来ても二重受付を作らない。新要求の場合は原本・native検証を済ませてから既存結果と照合する。exact旧要求の再送は過去の受付結果の照合であり、現在の原本が最新という主張ではない。

CASやread-backが不明なら成功を返さず、自動retry/set/新ID再実行をしない。保管容量やrequest上限に達しても入力履歴を削除しない。破損は隔離して停止する。実backend耐久性・削除/復旧運用はPhase 5B-1と同じく未実証。

## 自動検証と画面検証

新規契約テストは一般・医療・PDFの保存/再表示/明示受付、部分入力、exact再送、POST後read-back、別process再起動を検証する。異常系は未認証、Google issuer/sub/署名/audience/nonce、owner/case owner、session/ticket/TTL/Origin/CSRF、原本ID/hash/revision/numeric/native version変更・情報不足・観測途中変更、native snapshot/構造revision変更、stale input、明細追加/削除/順序/重複、case/purpose/ID再利用、G103/H301、二重POST、最大8instance競合、lost response/partial/read failure、journal破損・weak tag、Sheet sort/挿入/削除/入力snapshot変更を扱う。

全テストはsocket接続とlive Google clientを拒否する。native writer/execute、会計append/update、構造review、native store.saveは失敗spyにし、呼ばれれば失敗する。POST前後で認証DB一致を確認する。GETはrepository bytes・write count・認証DB・source reader呼出し数を前後比較し、write/native観測0をassertする。認証bootstrapとnative fake fixture初期化のmemory更新は、その後のGET/新POSTの副作用0とは区別する。

PC 1365px / 390pxで12種類・計24画面をChromiumで描画する。対象は一覧、一般/医療/PDF詳細、保留2種、部分編集、保存後詳細、再確認、結果、期限切れ、競合。横はみ出しなし、表示中の操作対象44px以上、外部へのページ要求0を検証。未保存ガード、明示チェック必須、競合時の保持、応答不明時の保持/再送禁止、送信中の追加入力とexact保存結果からのproof更新を実際のJSで検証する。

ブラウザー検証は**認証済みFlask responseをexportしたHTMLとoffline mock transport**を使う。バックエンドPOSTの実行・native validator/CASは別のhost-levelテストで検証する。実HTTP listenerや公開環境は作成しない。画像/metrics/proofを含むHTMLはprivate/Git除外に置き、CIからuploadしない。実OAuth・Drive原本往復・iPhone端末での成功を主張しない。

専用 `iphone-phase5b2a-checks.yml` はUbuntu 24.04/Python 3.12、contents:read、資格情報/Secrets/Variables/environmentなし、write flag false、deploy/buildなし。新規node IDのcollectとverbose実行、既存安全回帰、24画面/操作、compile/diff checkを実行する。確定した件数・CI run・commitは新Draft PR本文と完了報告へ記録する。

ローカルの一連の検証は **792 passed**（新契約94＋新CI境界1＋#130までの既存697）。最終の新契約94件も再実行し、94 passed（45.19秒）を確認した。PC/390pxは24画面・6組の操作検証・外部ページ要求0を確認。画像は目視でも確認した。既存hash固定hostと新namespaceではStateErrorのmodule namespaceが異なるため、認証失敗の条件を変えずにdata-free codeだけを新namespaceのJSON/HTMLへ整形し、期限切れの次操作と入力保持を明示する。既存ホストの認証仕様やglobal error handlerは変更しない。

## iPhone実機で未確認の項目

- Sheets検索→既存詳細→編集/再確認→原本別画面→復帰で、案件・入力・表示倍率が保持されること。
- 日本語入力/数値キーボード/カテゴリ選択、長い明細、タッチ対象とスクロール、キーボード表示中のチェック/ボタンの操作。
- 実Google本人認証、Safari/Sheets間往復、session/ticket失効・ログアウト・再認証後の過去受付結果確認。
- 通信断、二重タップ、別端末編集、送信中入力、アプリ中断・再起動時の保存済み値と未保存値の扱い。

新しい実機アクセス用環境・公開URLが必要なら事前承認を求める。今回作成・公開していないため、実機/実OAuthを含む製品完成とは扱わない。

## Phase 5B-2Bへの引継ぎ・承認事項

1. **全入口の共有claim**：全legacy入口と新入口のConditionalRequestsを同一storeへ接続し、同一native operation / Unit fenceで二重実行を防ぐ。今回の合成receiptやdraft CASを金融claimへ流用しない。
2. **既存authority/writer接続**：原本・全明細・purpose・actorの本人再表示をnative確認証拠へ接続する設計と合成実装の承認。native authority schema、TTL、privacy、writer安全条件を維持する。
3. **実行直前の検証**：live原本reader・native proposal・台帳/重複・exact input snapshotを直前に再検証し、既存writerとread-backで結果を判断する。duplicate/replay/partial_unknown/lost responseで自動再dispatchしない。
4. **Firestore運用**：保存先domain、実効IAM、専用鍵、保存/削除期間、容量/世代切替、backup/PITR、RPO/RTO、復旧/監査/費用上限、移行時の旧writer停止を具体化し承認する。実Firestoreの耐久性はfake成功では証明しない。
5. **live reader・実機試験**：必要なread-only canary範囲と実OAuth/端末検証を別承認。Cloud設定/デプロイ/本番入口の追加・切替・本番資源アクセスは今回の承認に含まない。
6. **追加ユーザー**：妻等のGoogle subject、案件共有範囲、医療閲覧/編集、owner認可と監査責任を明示承認する。現owner-only仕様を広げない。

本番Spreadsheet・既存検証ブック・新Drive資源・実Firestore・本番OAuth・実医療情報・Cloud/deploy・本番authority/会計/原本移動/AI送信は今回すべて未使用・未操作。新PRもDraft維持。main merge、本番接続、有効化、記帳許可はそれぞれ別判断である。
