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
3. 作成済みnative日常試作（6タブ/12,630セル/ownerのみ）を既存実行主体へ必要最小限で共有。固定ID修正の実装を本番stageへ接続し、既存受付を一本化する。coverageの経路/口座入力とカテゴリ操作を統合する。現状は表示writerのみopt-in接続、submitは未接続。入力保持/競合/結果不明復旧は合成検証済み。
4. 共通の小さいカテゴリ候補へ全参照元を置換。旧巨大helperの参照解消を確認してUIだけ縮小。正本は削除しない。
5. Amazon旧イベント蓄積・全工程同期を停止し、確定決済・確定返金へ両経路を同時移行。カード確定の正式元、非カード/残高/混合の扱い、金銭ID、旧計上IDとの固定対応、分割請求/複数返金/経路横断の重複防止を実装。現在のAmazon・決済の旧挙動はまだ変更していない。
6. 新旧writer排他の下で最終backup/差分照合/移行を行う。隔離コピーで復元。既存CI・Linux・10万件の実transport計測・失敗復旧・入力保持を確認。
7. 検証済みmain/承認/実行SHAを一致させ、非0件の限定本番反映・読戻し・replay追加0を確認。通常運用へ切替。実機スマホ未確認事項は一度にまとめて報告。

Goalは未完了。銀行収入方針、Payroll分離、Medical/給与privacy、対象外フラグは変更しない。追加OCR/AI呼出し、新規サービス、ローカル定期実行は追加しない。

## 戻し方（現段階）

本番コード・台帳値・共有・フラグは未変更。projectionのfolder Variableも未設定なので、現段階の実装を取り消すのに台帳復元は不要。変更前コピーは保持する。実移行の戻し方は、切替manifestとisolated restore検証に基づいてこの文書を更新する。投影ファイルは正本ではなく、欠落時に明示bootstrapで再生成できる。catalogはID保持のためバックアップ対象とする。

## 日常試作の確認記録（2026-09-19）

nativeコピー1ファイルを作成し、コピー内の旧31タブを6タブへ置換。3batch計142requestsで初期表示/プルダウン/入力フォーム/幅・高さを設定した。元台帳・バックアップは変更していない。6タブ/12,630セルのmetadataと作成セル・dropdownを読戻し、Google desktopでホームと確認フォームを実視認して文字切れを修正した。340pxはシート列幅合計であり、スマホ実機の検証済みを意味しない。実データ表示と本番writerは未有効化、試験取引の記帳0。

`DailySheets`は表示所有範囲を比較して変更セルのみを送る。フォームは固定ID/要求tokenで別管理し、表示更新からsubmitを呼ばない。修正時はDrive intent保存→対象固定IDと最新fingerprint再確認→指定列のみRAW更新→読戻し。結果不明時はpendingを維持し、既に同じ結果なら追加書込みなしで回復。本人が処理中に入力を変更した場合は新しい文字を保持し、旧要求に対する結果だけを確認する。正式台帳の自由編集保護と既存修正受付の統合は未実施。

全回帰2,050件成功。その後の10万買い物/20万明細の通し検証で、正本→保存集計→日常renderer/adapterの金額・件数を確認。日常は13月ファイルのみ、正本/index読込0、再表示の値書換え0。API transportはfakeのためGoogle実速度の数値として使わない。実ファイルID/リンクは`.private/migration-baseline.json`（Git対象外）へ記録した。
