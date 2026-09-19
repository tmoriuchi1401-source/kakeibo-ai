# 日常ファイル分離・金銭中心Amazonへの移行

2026-09-19着手。ユーザーのGoalで、対象のバックアップ・実装・通常push・検証後のmain統合・移行・本番切替は承認済み。段階承認は求めず、必要な既存境界を維持して続行する。

## 現時点

- 開始main: `c8ef626731cc8a941982a0d77b22e7ee4d88af31`。専用clone、branch `feat/ledger-daily-money`。
- 実Google確認: 正本31タブ、確保2,273,539セル。`_支出明細カテゴリ候補`だけで1,001,000セル。これは確保gridであり、非空データ件数ではない。
- Driveルートへ変更前のnative copyを1ファイル作成。31タブの名前・行列数一致、コピーの共有は所有者1名のみ。元ファイルはowner1/writer2、公開共有なし。コピーの参照はGit除外`.private/migration-baseline.json`に保持。
- コピー時にwriterは停止していない。このコピーだけを最終切替スナップショットと見なさず、切替時には共通ロック下で最新差分を再照合する。
- bounded A:M読戻しではコピー553明細に対し正本554明細。追加1 ID、既存行差分0、欠落0、ID重複0を確認した。現行カテゴリ56組に含まれない有効明細23行があり、旧分類・空白分類をinactiveカテゴリIDとして明示的に取り込む処理を追加した。元分類値は変えない。
- 最後に観測した本番成功runは`35429733480`、実行SHA `099dea08d07db4fef1fa30a18ea5d4a5d5953636`。最新mainとは異なる。現在の承認Variableは未確認であり、一致を主張しない。

## 実装済みの投影基盤（runtime接続済み・本番未有効化）

`monthly_projection.py`は既存支出明細A:Mのactive行だけから買い物・月・カテゴリ金額を再生成する。receipt/取込IDを買い物のidentityに使い、ヘッダ総額を加算しない。既存のsuperseded行は除く。返金は独立の金銭identityで負の支出、値引き明細は買い物内に留める。ID重複、不正金額、未知カテゴリ、読み取り中の変更は黙って0にしない。

カテゴリはUUIDを一度割り当てる。改名は同IDと旧名aliasを保持する。分割・統合は新IDと旧IDの廃止を明示し、過去分類の自動置換はしない。catalog/index/小さい更新待ちjournal/月別結果/長期summaryをprivate Drive JSONへ保存する実装を追加した。実ファイルの初期化はまだ行っていない。

初期indexは正本のbounded pageから再生成可能。通常は更新前後の月をdirtyとして、その月の物理行ヒントを固定IDとfingerprintで再検証する。変更のないobserveはdirtyを増やさない。月結果は差額加算せず置換する。表示・集計保存失敗の再実行は会計writerを呼ばない。

`monthly_projection_sheets.py`は実SheetsDBを受け取り、初期読込を最大2,000行単位、月内の非連続範囲を最大100範囲かつ26,000セル単位でbatchGetする。5,000行上限はない。要求数・範囲数・返却行/セル数を値を含めず測定する。

`SheetsDB`のappend/append_raw/update_row/update_row_raw/update_rows/set_raw_range/update_expense_categoriesへ書込み前の更新記録を接続した。状態列補完も同じ口を使う。マーカー保存失敗時は台帳を書かず、台帳の応答不明時はマーカーを残す。追記の読戻しは保存済み末尾から、既存修正は指定範囲から行う。日付変更前の月もjournalへ保存してからindexを更新し、途中停止後も旧月を再生成する。正常時にjournalを空にし、全工程イベントは蓄積しない。

`KAKEIBO_PROJECTION_FOLDER_ID`は既存SA鍵で暗号化した任意のVariable。未設定なら現在の動作を維持する。設定時の`expenses-refresh`は変更月投影へ切り替わる。集計stageだけはpendingから安全に再実行し、既存の取込・会計stageの結果不明ゲートを緩めない。表示保存失敗後に会計writerを呼ばない。

既存の親Actionsに`scope=projection`と既定falseの`projection_bootstrap`を追加した。新しいschedulerはない。共通production lockとmain/承認/実行SHA照合を通し、ledgerはread-only credentialで読み、投影ファイルだけを書き込む。取込/銀行/Payroll/Medical処理とOCR runtime準備を起動しない。bootstrapは明示applyかつ単独scopeのみ。カテゴリ専用runnerもfolder設定時は同じSHAを要求する。

Drive保存前にfolderと対象JSONの共有主体が元台帳の既存主体の部分集合であることを確認する。公開/ドメイン共有・新しい共有先は拒否し、共有権限自体は変更しない。作成/更新の自動retryは行わず、曖昧な作成結果は次回名前とsource bindingを再読込して二重作成を防ぐ。CAS/readbackは競合検知であり、排他は既存のActions lockが担う。

履歴関数は直近13か月だけを読み、月指定なら1か月だけを読む。買い物単位で検索・カテゴリ指定・ページ分けと全件数を返す。比較関数は経路/口座別の完了状態が両年とも揃う月だけを対象にし、当月を除外、平均の対象月数を保持する。

## 検証

- 変更前の全既存合成テスト: Windows/Python 3.14、1,965件成功（80.21秒）。
- 新規投影・bounded Sheetsテスト: 18件成功（8.25秒）。10年・10万買い物・20万明細の金額/件数一致、月更新、過去年月修正、年越し、途中保存失敗と再実行、旧ヘッダ除外、複数部分返金、本人カテゴリ保持、改名と分割、旧/空白分類の保持、欠落coverage、履歴ページングを含む。
- 7,001明細+空白行のfake Sheetsで初期読込5要求（metadata1+値4）、非連続2,334行の変更月読込24要求。実GoogleのAPI量・時間の改善実績ではない。
- compileallとdiff-check成功。[Draft PR #41](https://github.com/tmoriuchi1401-source/kakeibo-ai/pull/41)の先行commit `f4f8112`はLinux CI `35445179174`で一般・Medical/Payroll合成・OCR runtimeの3job成功。旧/空白分類の保持追加後のCIは別途確認する。
- 旧/空白分類保持後の`d7be148`もLinux CI `35445328846`で成功。
- 永続化接続後の更新範囲/故障復旧テスト24件成功。月ファイル/index/summaryの保存前後それぞれの失敗と新プロセスでの再開、追記結果不明、journal先行失敗、同時更新検知、catalog ID・coverage入力保持を含む。
- 永続化を含む10万買い物/20万明細の合成評価は13.50秒。過去1行修正は指定行1+変更月1,668行を読み、他119か月の値を書き換えない。履歴は13か月ファイルだけを読み、正本/indexへのアクセス0。in-memory transportの測定であり実Google所要時間ではない。
- Drive fakeとActions境界を含む関連76件成功。全回帰のscope列挙の期待値を新scopeへ更新し、既存main/SHA/銀行/実帳票分離の条件を維持。最終Windows全回帰2,020件成功（95.46秒）。

## 続ける必須作業

1. 基準値・保存済みID/月別/種別別集計・本人入力/未解決のprivate inventory。既存Actionsの時間/読み書き量、承認SHA、共通writer状態を再確認。
2. 接続したcatalog/index/dirty月を本番用private folderで初期化し、全writer入口を最終点検する。旧Amazon専用manual workflow等は切替時に停止/削除するため、現段階の共通hookだけで全本番入口を保証したとはしない。通常の取込/カテゴリ修正から投影readbackまでの実データ受入を行う。
3. 作成済みnative日常試作（6タブ/12,630セル/ownerのみ）を既存実行主体へ必要最小限で共有。接続済みの固定ID修正stageを、実正本の保護・紐付けmarker・旧編集リンク移行後に有効化する。coverageの経路/口座入力とカテゴリ操作・金銭例外受付を統合する。input保持/競合/結果不明復旧は合成検証済み、本番modeは未設定。
4. 実装済みの`CompactCategoryMigration`を、日常修正受付と台帳の自由編集保護の切替と同じ排他区間で実行。全参照監査→最新入力再照合→一括移行→内容/入力規則読戻し。catalog更新時の共通候補同期と過去反映年月候補の旧ホーム依存解消を接続する。正本値は変更しない。
5. 接続済みのAmazon/card共通money modeへ、旧計上IDとの固定対応と最新差分manifestを渡して同時切替する。保存済み商品補足、確認中の金銭例外の固定ID本人判断を接続。旧専用workflow/注文同期・発送照合・手動注文照合を停止/削減し、イベントを退避する。現時点の本番は旧挙動のまま。新modeの基礎writerは31件の関連合成テスト済みだが、この項目全体の完了ではない。
6. 新旧writer排他の下で最終backup/差分照合/移行を行う。隔離コピーで復元。既存CI・Linux・10万件の実transport計測・失敗復旧・入力保持を確認。
7. 検証済みmain/承認/実行SHAを一致させ、非0件の限定本番反映・読戻し・replay追加0を確認。通常運用へ切替。実機スマホ未確認事項は一度にまとめて報告。

Goalは未完了。銀行収入方針、Payroll分離、Medical/給与privacy、対象外フラグは変更しない。追加OCR/AI呼出し、新規サービス、ローカル定期実行は追加しない。

## 戻し方（現段階）

本番コード・台帳値・共有・フラグは未変更。projectionのfolder Variableも未設定なので、現段階の実装を取り消すのに台帳復元は不要。変更前コピーは保持する。実移行の戻し方は、切替manifestとisolated restore検証に基づいてこの文書を更新する。投影ファイルは正本ではなく、欠落時に明示bootstrapで再生成できる。catalogはID保持のためバックアップ対象とする。

## 日常試作の確認記録（2026-09-19）

nativeコピー1ファイルを作成し、コピー内の旧31タブを6タブへ置換。3batch計142requestsで初期表示/プルダウン/入力フォーム/幅・高さを設定した。元台帳・バックアップは変更していない。6タブ/12,630セルのmetadataと作成セル・dropdownを読戻し、Google desktopでホームと確認フォームを実視認して文字切れを修正した。340pxはシート列幅合計であり、スマホ実機の検証済みを意味しない。実データ表示と本番writerは未有効化、試験取引の記帳0。

`DailySheets`は表示所有範囲を比較して変更セルのみを送る。フォームは固定ID/要求tokenで別管理し、表示更新からsubmitを呼ばない。修正時はDrive intent保存→対象固定IDと最新fingerprint再確認→指定列のみRAW更新→読戻し。結果不明時はpendingを維持し、既に同じ結果なら追加書込みなしで回復。本人が処理中に入力を変更した場合は新しい文字を保持し、旧要求に対する結果だけを確認する。正式台帳の自由編集保護と既存修正受付の統合は未実施。

全回帰2,050件成功。その後の10万買い物/20万明細の通し検証で、正本→保存集計→日常renderer/adapterの金額・件数を確認。日常は13月ファイルのみ、正本/index読込0、再表示の値書換え0。API transportはfakeのためGoogle実速度の数値として使わない。実ファイルID/リンクは`.private/migration-baseline.json`（Git対象外）へ記録した。

日常実装`d243d41`のLinux CI `35447752526`は成功。初期表示のGoogle desktop確認はホーム/履歴/確認フォーム/推移/設定で実施済み。データ投入後・スマホ実機の確認とは区別する。

## Amazon金銭処理の接続記録（2026-09-19）

[au PAYカード公式説明](https://www.kddi-fs.com/security/initiatives/usage_info/)により、速報と売上確定の「ご利用詳細」を区別。既存の詳細メール経路は確定売上の正式元として使える。月全体の請求総額通知は銀行引落しのauthorityのまま維持し、個別支出へ再計上しない。

新しいmoney bookは金銭ID、支払元、日付、金額、関係ID、原本リンク、短い結果と旧計上aliasを保存する。注文・配達・返品申請を保存するevent基盤ではない。RAW台帳append前にintentを保存し、結果不明時には対象IDで再照会。台帳反映済みならappendせず、2表の片側だけなら欠けた側のみを回復。台帳readback後は重複した明細payloadをbookから除く。本人分類や過去計上日は変更しない。

共通money modeではカード購入と返金を旧Amazon未照合除外/返金除外から切り出し、Amazon側のカード通知は補足に限定。注文だけのメールで計上しない。非カードの確定金銭は支払元単位、混合の内訳不明は確認。確定返金の月は返金確定日/確定通知日を使い、元の利用日に戻さない。同じ注文の分割請求と複数返金は異なる金銭ID。元購入の関連不明・累積返金超過・旧支出との対応不明は確認へ残す。金銭ID/既存取込IDの一致は厳密に検証し、同日同額は照合候補にとどめる。

money modeはまだ未設定。初期化済みの移行bookなしでは起動を拒否。今後、旧Amazon/カード/レシートの全既存IDと月別/種別別金額、未解決の本人入力を排他snapshotから対応付け、確認例外の受付と旧writer停止を接続してから本番へ切り替える。解析できない確定通知の確認行化、混合払いのleg判断、既存商品の補足は次工程。新規OCR/AI再解析は行わない。

旧計上aliasには`expense_ids`と`expense_amounts`（元明細ごとの金額snapshot）を持たせ、関連付け直前に正式台帳の実在/金額/active状態を再照合する。分割請求を旧購入の全明細へ関連付ける場合も、元明細を変更せず、その請求のfingerprintでaliasを固定する。aliasが古い場合は再計上も上書きもせず確認へ戻す。`8b9691a`のLinux CI `35448972181`成功、alias読戻し追加後の関連32件成功。

## 共通カテゴリ候補の実装（2026-09-19、本番未移行）

`compact_categories.py`の`CompactCategoryMigration.plan(catalog)`はfresh metadataと全gridの数式/入力規則だけを最大26,000セル単位で監査する。通常の台帳・医療・給与のscalar値は読まない。helper自身はアプリ所有markerで確認し、外部セル・named range/metadataに未知の参照があれば停止。カテゴリ操作A:Lと要確認Aの固定IDは全行をboundedに読み、行数上限で選択済み入力を取りこぼさない。

`apply(plan,catalog)`は同じsnapshotを再照合してから、helper縮小、literal候補、参照規則、分類操作C/Dの変換を一つのbatchUpdateにまとめる。支出明細の値はrequestへ含めない。分類操作のチェックや承認snapshot・要確認の本人入力を維持し、読戻しで全入力・固定ID・候補・dropdown参照を検証する。結果不明の自動retryはなく、次の明示実行ではversion 2を読み、既に変換済みの選択を二重変換しない。

このadapter単独はwriter lockや本人入力の凍結を取得しない。Sheetsにcell CASはないので、切替工程が共通lock・本人入力の凍結・native backupを先に成立させる。日常の固定ID修正受付と正式台帳編集保護も同時に切替える。旧UI installer/restoreはversion 2を検出すると停止し、古い巨大matrixへ戻さない。復旧は隔離nativeコピーで確認してから行う。

現行56候補なら4×57=228セル（旧1,001,000セル）になる。inactiveの旧分類はcatalog/正本に残し、新規選択肢からのみ除外する。合成検証では2候補で12セル、1,050選択入力/7,001行参照/途中入力変更/応答不明/replayを確認。実Googleの移行は0件。日常ファイルの候補は同じcatalogを用いる別ファイルの投影であり、IMPORTRANGEは使わない。カテゴリ追加/改名時のhelper同期と、過去反映の旧ホーム由来の年月候補は次工程で接続する。

## 日常修正stage（2026-09-20、本番未有効化）

`run_daily_requests`は`fixed-id-v1`設定時だけ既存親Actionsのall処理前に呼ばれる。別のnative source checkpointには追加せず、既存のcorrections intentを復旧単位にする。manual `scope=daily`は同じlock/main/承認SHA境界内で修正と派生表示だけを実行し、他経路入力・projection bootstrapを拒否、OCR準備も起動しない。projection scopeは従来どおり正本read-onlyでsubmitしない。

sourceの変更月indexを回復→既存queued/pendingを最大20件回復→フォームの新要求を受理→変更前後の月を再生成→日常表示、の順で処理する。フォームには先に反映待ちを表示し、保存成功後にチェック解除・新token・結果/更新時刻を一括ackする。台帳応答不明、intent更新失敗、ack応答不明、表示更新失敗のいずれも保存済み要求と台帳を読戻し、同じ台帳更新を重ねない。保存済み要求をcheckbox解除だけで破棄しない。新しい本人入力は保持し、旧要求のack時に新規要求として勝手に送信しない。

`daily_edit_cutover.cutover_requests`はcompact候補が移行済みのsourceだけを対象に、支出明細A:Mの全行（将来追記を含む）を既存実行SA用に保護し、アプリ所有の旧カテゴリ修正リンクを日常確認フォームへ置換、daily ID hashをsource metadataへ記録する。canonicalの値を変更せず、Drive共有やscopeを拡張しない。runtimeもfresh metadataでこれらの前提を毎回確認する。Googleでは所有者の保護変更能力を除去できないため、所有者による明示的な保護解除は別の管理操作になる。実migrationではwriter/本人入力排他・最新native backupの下で実行し、保護/リンク/markerを読戻してからmodeを有効化する。

現時点のGoogle保護・marker・共有・modeの実変更は0。daily inbox単独の接続を、money例外やcoverageを含む全確認受付の完成とは扱わない。失敗した過去要求の一覧/解決操作も統合確認の次工程に含める。

## Amazon金銭例外の受付（2026-09-20、本番未有効化）

`amazon_money_review.MoneyReviews`はprivate `money-reviews` documentへ固定REQ IDで本人判断を保存する。対象の金銭ID/fingerprint・最新review状態・正式台帳との対応を検査し、queued→pending→applied/failedで回復する。保留・別の確定取引・既存支出対応・返金元指定・自分用チャージを扱う。未確定や混合払いの内訳不足を強制計上する機能ではない。負の旧明細への対応は元購入bindingを持つ移行manifestで扱い、このフォームでは推定しない。

既存支出への対応付けは元の明細集合・全値fingerprintを再読込し、同じ明細集合への分割請求の対応額を合算する。対応付けだけで台帳を更新しない。返金元の台帳金額/activeと、旧計上ID・分割請求IDが同じ正式支出を指す場合の累積返金上限を確認する。日常修正で金額が変わっていた場合は旧bookの金額で押し切らない。

フォームは確認A80:D88とJ81のtokenを使い、通常rendererは値を書かない。新しい日常コピーには初期設置する。既存コピーは`upgrade_requests`がsource binding・marker・予約領域の空欄を確認し、切替時のlock/backupの下でnative一括設置と読戻しを行う。実prototypeへの適用はまだ0件。既存daily stageからmoney mode有効時だけ未完了intentを回復し、新フォームを受理、変更月投影と表示を更新する。原本リンク/金額等はprivate日常表示だけに載せ、親Actionsには件数のみを渡す。

新modeでは旧注文照合候補の読込・再生成を止め、旧本人選択/メモを保持する。旧画面からのAmazonの計上・除外・統合・注文照合を保留し、金銭確認へ案内する。他のレシート承認は維持する。注文同期/専用workflow全入口の停止、未解析確定通知の確認行化、保存済み商品補足、旧対応manifestは引き続き移行前の必須作業。

関連83件でJSON readback、intent保存前後失敗、片側の台帳append後失敗、再実行追加0、入力保持、保留の再取得、旧候補へのアクセス0、通常レシート承認、分割対応と過剰返金の拒否を確認。全回帰2,142件成功後に返金上限と保存失敗の5ケースを追加した。

## 経路・口座別の取込状況（2026-09-20、本番未有効化）

`daily_coverage.py`は設定タブの開始月・終了月・経路・口座・状況を固定REQ IDで受け付ける。最大120か月を一括指定でき、経路と口座の組に固定IDを付ける。月の状況は未確認/取込中/完了/対象外。対象外はその月に利用しない経路を本人が明示するための値で、黙って未取込を完了へ変換しない。経路名・口座名を変えると別IDとなるため、既存のdropdownから選択する。新たな経路を加えた場合、その経路の未確認月は比較から外れる。

本人入力はprivate `coverage` documentに置き、再生成するsummaryへコピーする。送信前の月別状態と反映直前の状態が異なれば失敗として入力を残す。状態保存・summary保存・ackの結果不明は同REQ IDで回復する。既存daily stageのlock/main境界に接続し、新schedulerや会計書込みを追加しない。既存summaryだけに取込状況が存在する場合は先に明示移行が必要で、最初のフォーム送信で上書きしない。

全経路が完了または対象外の月に限り、更新待ちjournalが空で、最新indexにも取引が存在しなければ明示的なゼロ月をsummaryへ作る。初めてゼロ月を確定する操作でのみindexを読み、通常の表示/同じ要求のreplayでは全indexを読まない。完了を取り消した月や新経路の未確認月は、取込状況から導出したゼロ値を撤回する。実取引が後から増えた月の金額は保持する。平均・前年比は両年の共通完了月だけで、進行月を除く。summary紛失後の明示bootstrapでも、別保存の入力から完了状態とゼロ月を再生成する。

入力は設定A10:C19/F11、状況表示は月B22・ページB23と26行目以降の50件。全件数とページ数を表示し、フォーム本文は通常rendererが書き換えない。予約領域の既存値とsource markerを検査するupgrade builderを追加したが、実Googleへの適用は未実施。6タブ/12,630セルのまま。カテゴリ内訳は150件ずつのページ表示へ変更し、301カテゴリでも停止・切捨てせず全件を表示できる。

## 旧Amazon入口の廃止（2026-09-20、本番main未反映）

注文/発送/取消/返品状態・イベント再解析・注文ヘッダ・旧注文照合を扱うCLI 35個を削除し、直接起動する旧Gmail診断2入口も認証前に終了する。専用Actions 20個を削除。`amazon-daily-import.yml`は過去cache/runの参照を保つためファイル名だけ残し、schedule・checkout・secrets・Python実行を持たない案内に置き換えた。旧データのオフライン読解・移行照合に使える関数と回帰テストは保持するが、日常の稼働入口からは呼ばない。旧コマンドを記載した過去の手順書より、この廃止契約を優先する。

唯一の現行Amazon CLI `amazon-gmail-recurring`は`confirmed-v1`未設定なら認証前に停止し、旧注文計上へfallbackしない。main統合時は既存親scheduler/writerを排他停止した切替区間で、money book初期化とmode有効化をセットにする必要がある。旧注文canaryを金銭modeで流用せず、以下の固定金銭ID canaryへ置き換える。

money modeの共通reconciliationはAmazonの注文・レシート・決済を日付/金額/店舗の類似で除外しない。汎用自動計上や低頻度のカードCSV入口に残るAmazonは、確定元/旧計上対応を確認する状態へ送る。注文表は読まず、金銭IDのある確認受付へ移すmanifest/旧確認の解決を後続で行う。通常のカード/レシート照合、銀行の既存資産形成・引落しauthorityは維持する。

廃止CLIの従来実行を要求する23テストを、35入口すべての認証・write前拒否テストへ置き換えた。ドメイン解析の回帰テストは残している。親Actions→実CLI→共有Sheets adapterの合成統合は確定ギフト残高請求へ更新し、イベント/注文ヘッダ増加0、非0金銭記帳とreplay追加0を確認。ここでの結果は合成transportであり、非0本番canary完了の証明ではない。

## 固定金銭IDの限定試行（2026-09-20、本番未実行）

既存の親Actions `scope=amazon_canary`を使い、`canary_source=amazon`または`aupay_card`で確定金額の正式元を選ぶ。scope名は既存のまま、注文番号の対象指定は廃止する。手動dispatch・`confirmed-v1`・main/検証SHA一致・既存production lockを必要とし、通常のrun ledgerとnative checkpointのpending判定を維持する。OCR・日常受付・他source・共通後処理は実行しない。

previewは件数だけを返す。applyの`amazon_target`には、既存SA公開鍵による`AMAZON_TARGET` bindingで包んだ固定`AM-`金銭IDを渡す。平文IDや金額をActionsの公開結果へ出さない。指定IDが見つからない、同IDの金銭情報が競合する、未確定・混合払い・振替・補足のみ・旧計上への対応のみの場合は新規記帳の試行として扱わず停止する。確定1件または同じ記帳済み1件のreplayだけを扱う。

両sourceとも試行ではメール走査checkpointを進めない。対象外の金銭・通常カード取引はそのまま残り、次の通常処理で取り込む。初回は既存MoneyWriterの全列readback後に、金銭bookと正式な取込/支出の固定ID・日付・金額・active状態を確認する。replayでも正式台帳を読み直し、変更・欠落があれば追加ゼロの成功として扱わない。結果不明のappendではnative pendingを保持し、次の親実行から自動的に再試行しない。

親Actions→実CLI→共有Sheets adapterの合成統合で、Amazonギフト残高とカード確定詳細の両経路の非0記帳・イベント/注文ヘッダ増加0・replay追加0・他source state不変を確認した。対象外取引を後続の通常処理で取り込めること、台帳変更時の停止、append結果不明時のpending維持も検証した。実金銭book/移行manifestの初期化と切替後に、非0本番readback・replay追加0を別途確認する。

## カテゴリ候補と過去反映の年月候補の同期（2026-09-20、本番未有効化）

カテゴリマスタだけの変更でも、通常の投影refreshがcatalogを同期する。台帳・index・過去月ファイルを読み直さず、既存ID/改名aliasを保持する。未知の組は新IDとして追加し、マスタから外れた分類はinactiveとする。名称変更だけから改名や統合を推定せず、過去明細を置換しない。固定IDによる明示改名はcatalog APIの契約であり、本人向けの管理入力は別途統合する。

既存daily apply内で`compact_category_sync.sync_choices`を呼び、移行済み4列helperの変更行だけを同期する。本人の選択・チェック・承認snapshotは書き換えない。変更なしは書込み0。候補増加時だけgridを延長し、カテゴリ操作C列/要確認L列の全行を2,000行単位・dataValidationだけのfield maskで読んで参照範囲を更新する。隣接した同じ条件はまとめる。削除時は旧候補の値だけを消し、通常同期でgridを縮小しない。native一括write後に候補と更新した参照を読戻す。結果不明のwriteを同じ呼出し内で自動retryしない。

compact移行後の過去反映の年月候補は、保存済みsummaryの全記録月と現在の全入力行の開始/終了月からリテラル値で作る。旧ホームの5,001行参照・1,000行の入力参照・spill式に依存しない。古い月と本人の選択を保持し、必要時だけ候補用gridを増やす。Z/AAの候補値と入力規則だけを更新し、本人の開始/終了月・checkbox・固定要求を変更しない。1,201か月の候補と1,201行目の選択を合成検証した。移行前の旧UI互換経路は旧契約を保持する。

## 未解析の確定通知を確認へ残す（2026-09-20、本番未有効化）

確定通知に金額・確定日・識別情報が足りない場合、private `money-notices`へ固定MN ID・原本Gmailリンク・短い理由・内容hashと対応状態だけを保存する。金額を0や推定値で埋めず、本文・長い解析・注文工程イベントを保存しない。注文/発送/配達/返品申請・速報は対象外。カード側もAmazonを含む確定詳細の一部または全体を解析できなければ通知参照を残し、取得できた別の確定明細は既存MoneyWriterで処理する。

両runnerは未解析通知を保存・readbackしてから通常checkpointを進める。取得範囲が不完全なら通知/金銭とも保存せず、通知writeの成否不明ではcheckpointを進めない。previewと固定1件canaryは未解析通知を保存しない。原本が同じ再取得では同じMN IDを使い、保留・確認済みを維持する。内容が変われば再確認へ戻し、古い本人要求のsnapshotは拒否する。

未解決通知は全期間の確認一覧へ集約し、既存の金銭確認フォームで保留または「通知を確認済みにする（記帳なし）」を受け付ける。MN通知への対応で台帳や金銭bookを変更せず、既存支出対応・別取引計上・返金指定・振替へ進めない。正しい金銭情報が不足しているケースは低頻度例外として原本を確認する。本人要求は既存REQ inboxのpending/readback/ack経路を使い、入力を残す。

同時に、既存金銭確認のpending snapshotが後の状態更新と同じ辞書を共有し、完了状態の保存が次回まで遅れる点を修正した。要求の保存済み状態が初回からappliedになることと、通知保存前後の失敗・再取得追加0・本人判断保持・台帳書込み0を検証する。実通知の保存や本番checkpoint変更はまだ行っていない。
