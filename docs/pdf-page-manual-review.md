# PDFページ確認を共通入口にする

既存 `PDFページ確認` を項目／内容の2列にし、ページ種別・grouping・医療完全手入力を縦のカードに集約する。新しい確認シートやWebアプリは作らない。既存の `_PDF確認受付` がownerの入力snapshotを受付ける。技術情報はC:Pをhiddenにし、通常操作でhash、revision、UUIDを入力させない。

## 3つの独立した確認

* page-kind: 原本・ページに限る人間の種類回答。`page_kind_only`。Gemini、会計、Medical handoff、archiveは全てfalse。
* grouping: 従来のproposal／confirmed authority。privacy分類は自動観測のまま。人間の「一般」回答はroutingにのみ使い、送信権限へ変換しない。
* Medical: 原本を見た完全手入力＋明示操作だけを既存 `ReceiptConfirmation` のdurable confirmationへ渡す。種類回答・grouping確定だけではwriterに到達しない。

page-kindは既存の専用 `pdf-grouping-authority-v1` Drive正本に厳密なoptional `page_kinds` collectionとして保存する。旧schemaの読込みは維持する。source ID/hash、page number/hash、automatic/human classification、日時、confirmation digest、falseのscope flagsのみ。保存は従来のv2 strong ETag→If-Match→exact read-backを通る。欠損・破損・原本やページhash不一致は権限なし。自動privacy分類のmedical/payrollと、一度確定した人間medical/payrollは一般へ降格させない。OCR本文、施設名、金額、画像はこのstateとauditへ保存しない。

Drive正本から確定Unitを取得する際は、自動分類と現在有効な人間分類の安全側を採用する。人間のmedical/payroll/判定不能は将来のpayload対象も制限する。人間のnormalで自動sensitive_unknownをnormalへ弱めない。保存済み自動分類とgrouping identityを書き換えない。

## スマホ操作

原本を開く→支払日／施設名／実支払額／カテゴリを入力→「医療費を確定」を選ぶ。カテゴリの「医療費」は既存マスタの `医療・保険｜病院` に対応し、マスタに存在するときだけ初期表示する。薬代・その他はマスタの有効な項目から選ぶ。支払方法・メモは任意。施設名、金額、日付は空欄から完全手入力。OCR・Vision・過去candidateを表示・転記しない。

4項目が揃うまで操作dropdownは保留のみ。Apps Scriptは入力編集ではvalidationを更新するだけで、確定操作だけrequestをcaptureする。backendが既存validator、category master、原本、ページ、人間kind、review identity、現在のowner inputsを再照合する。statusセルはauthorityにしない。原本リンクにはページ番号を併記しDriveの既存権限を使う。画像previewを保存しない。Drive viewerがpage fragmentを無視する環境でも、画面のページ番号から原本を確認できる。

## 既存Medical backendの再利用

`PdfManualConfirmation` は入力UIとページ鮮度確認のadapterのみ。既存PDF `DocumentUnit` IDをsource unitとして使い、既存review_id、manual parser、duplicate検査、durable intent、writer、会計read-back、pending/replay処理をそのまま使う。PNG hashがreview version、原本hashがsource sha256。会計の原本リンクは親PDF＋ページ番号。Medical input/stateは既存receipt confirmation Drive正本へ保存し、新しいMedical storeを作らない。

全ファイルの旧受付workerはページreviewを列挙しない。archiverも明示的にPDF pageを除外する。医療ページが終端しても親PDFをprocessedへ移動しない。部分書込み・不明書込みはpendingのread-back照合で停止し、欠損行を再追記しない。UI更新失敗は保存済みMedical confirmationと会計を読戻してprojectionだけ復旧する。一般ページはMedical writerへ渡さない。

人間確認時は原本のmetadata／bytesを読み直し、保存されたscaleで対象ページを逐次再render・PNG hash照合する。全ページの再OCRや金額抽出をしない。Medical入力再照合時は対象ページに限定し、一般grouping確認時は全memberのpage hashを照合する。元PDFや生成PNGをAIへ送らない。

既に `pdf-grouping-authority-v2` へ移行した原本の完全手入力確認には、独立した
`confirmed_legacy_observations` verifierを注入できる。Drive v1/v2正本のACL・strong
ETag・migration linkage、原本SHA、ページ数、全ordinal identity、旧human intentを
照合し、処理中のauthority変更を拒否する。旧render fingerprintはlegacy reviewとの
紐付けとして保持し、現在のPNGエンコードとの一致を要求しない。この確認はrender／
OCRを一切行わず、Medicalのreview ID・HMAC・manual writerを変更しない。新しい
proposalやAI permissionには使わない。未移行の既存workerは従来のstrict verifierを
維持し、自動で緩いfallbackへ降格しない。

## 一般ページだけのgrouping確認

`DurablePdfGrouping.regenerate_general` はfresh原本／全ページhashを確認し、人間の種類回答から一般ページだけの候補を作り直す。`DrivePdfReader.grouping_evidence` は対象一般ページだけを逐次local OCRし、既存の隣接groupingロジックへ一時的な証拠を渡す。原本画像・OCR本文・証拠の店舗名や日付を保存しない。機微ページを飛び越えた結合は禁止。弱い証拠は単独候補にする。

proposalには原本全ページの観測metadataを変更せず保持し、`grouping_page_numbers` で確認対象を限定する。`page_kind_digests` が全ページの有効な種類回答に紐付く。候補生成用のeffective normalは永続privacy分類やpayload gateを書き換えない。Medicalはこのpartitionから除外し、医療入力待ちのカードと入力を維持する。

分割／隣接結合は選択範囲を全て網羅する新proposal・revisionを作り、confirmed authorityを破棄して再表示する。改めて明示「確定」を選んだ場合だけ、その一般範囲のpartitionを保存する。人間確認前の自動候補はauthorityではない。source hash/count、member hash、種類回答digest、proposal digest、revisionを再照合し、旧画面・範囲外・Medicalを含む操作は拒否する。

scope付きconfirmationは一般範囲と種類回答digestを含み、Gemini／会計／Medical handoff／archive flagsを全てfalseとする。旧全ページproposal／confirmationのschemaと読込みは維持。種類回答の変更でscope confirmationを失効させ、replayは既存Unit ID・confirmation digest／時刻を再利用する。scopeの取得や同一request replayは追加authority writeを行わない。保存は従来のstrong ETag／If-Match／exact read-backのみ、412後の自動retryや無条件fallbackはない。

## privacy保留一般ページの完全手入力

自動 `sensitive_unknown`、有効な人間 `normal`、確定済みの単独partitionが
揃うページだけ、同じ2列UIに一般手入力カードを表示する。Medical/payrollはこの
カードへ入れない。必須は支払日、正の整数金額、現在カテゴリマスタの組合せ。
店舗名、支払方法、メモは任意で、全て空欄から入力する。OCR・AI候補を転記しない。
入力編集はvalidationだけを更新し、「一般手入力を確定」だけが受付をcaptureする。
画面上の一般回答、状態セル、旧PNG fingerprintは記帳authorityにはしない。

`process_page_request` の明示的な `general_factory` は、現在DriveのUnitと原本を
照合し、既存一般manual writerで保存・read-backした結果だけを返すhost adapter。
handler未注入のworkerはfail closedで、確定済み表示や書込みを行わない。
呼出し側はcaptured UUIDのsnapshot、現在入力、原本リンク、source/page-kind/
proposal/revisionを各書込barrierで再照合する。UI更新はdurable成功後のprojection。
別UUIDで同じUnitを確定した場合も、元のintentのread-backを行い追加記帳しない。
変更された入力やpendingの不明書込みを自動上書き／再送しない。

Medicalの4項目、review identity、manual writer、amount/HMAC/admissionの仕様は
変更しない。両経路ともcaptured snapshotの差替えを拒否する。新シート、schedule、
Secret、scope、triggerは作らない。実Apps Scriptの更新とprotected hosted dispatcher
への接続・canaryは別の導入作業であり、このコード変更だけでは有効にならない。

## Draft中の実行境界

manual workflowの既存main SHA gateと `PDF_GROUPING_REVIEW_ENABLED` を維持し、自動dispatchは既定OFF。新schedule、scope、secret、トリガーは追加しない。既存Medical完全手入力への停止措置2commitをPRへ取り込み、旧AUTO/candidate経路を復活させない。live canaryは利用者が指定したpage-kindの保存と空のMedical入力欄／一般groupingの表示のみ。実医療費の記帳はownerが原本を見て手入力・明示確定した後の別操作とする。PRはDraftのまま、mainへmergeせず停止する。
