# Gate C：既存CAS受付・独立検証・同一Sheets表示の最小設計

2026-11-08。対象：Draft PR #200、`tmoriuchi1401-source/kakeibo-ai`。
調査基準・開始時および継続時のPR headは `3f4f5ab2872ba0c71b6b28fd7ef315fcbbcdcd53` と完全一致。base/mainは `fa7ac9378ef51e4a51258dd6053afe52ed0a2bb8`。想定外差分なし。以降、本設計を追加する文書commitだけを許容し、別のhead変更を検出した場合は停止する。

**設計資料の提出まで。実装開始NO-GO／別途承認待ち。Gate Bは総合NO-GO・停止済み、Gate C実行・本番接続・金融writeもNO-GO。** 到達目標は一般レシートの「検証済み・未記帳」。この資料をGoogle変更、UUID発行、trigger作成、dispatch、main変更、mergeの承認にしない。

## 1. 確認した基準と証拠の範囲

- [iPhone継続仕様](iphone-receipt-review-mvp-spec.md)を全文確認。第10～12節の「確認」1可視タブ・1行1案件・通常セル・原本リンク・メモ保持を維持。Webフォーム、Cloud Run、新DB、常駐サーバー、外部キューを追加しない。
- [Gate B実施記録](receipt-gate-b-event-acceptance-results.md)、[計画](receipt-gate-b-event-acceptance-plan.md)、[CAS改訂](receipt-cas-iphone-revision.md)、`apps-script/receipt-gate-b/`の4ファイルを確認。
- [隔離受入記録](receipt-iphone-isolated-acceptance-approval.md)の最新節ではGate A PASS（SA原本bytesと独立生成原本hash一致）。[CAS probe記録](receipt-isolated-acceptance-preflight.md)の最新節では独立JSONの実HTTP stale 412・並行唯一勝者PASS。古い「未実施」の段落を最新事実として再利用しない。probeは受付用PRIVATEとは別資源で、転用しない。
- `app/receipt_sheet_validation.py`、`receipt_sheet_cells.py`、`receipt_item_correction_review.py`、`receipt_item_correction_plan.py`、`receipt_validation.py`、`receipt_review_snapshot.py`、`pdf_intake_registry.py`、`google_clients.py`、`receipt_local_ocr.py`と関連テストを読取。既存category-submit／category-sheet-requestは接続パターンだけの参考。
- 本調査はrepository・公開公式仕様の読取とMarkdown更新のみ。Googleの現在状態・非公開manifest・実配置コード・資格情報は今回取得していない。既存テストの内容と過去成功記録を確認したが、今回はコード保存・テスト実行・実API受入をしていない。

ローカル保存に失敗した2回：最初の資料一括保存はtoolの64,000-byte上限。次の分割保存は `apps-script/receipt-gate-b/Code.gs` の保存時、自動承認レビューが「Apps Script実装変更でNO-GO違反」と判定して拒否。後者までにAGENTSと3文書・Gate B READMEは保存済み、後続保存は中断。コード保存を別手段で迂回しない。必要なコード・manifest・テスト・iPhone仕様はGitHubの基準SHAから読取済みであり、重要仕様の未確認は以下で個別表示する。

## 2. 最小処理フローと確定点

登録前の準備を含めた順序を固定する。Apps Script／Sheets／JSONの単一トランザクションは仮定しない。

| 順序 | 処理と再利用先 | 成立条件・失敗時 |
|---|---|---|
| P1 | 通常セルから案件単位の入力を再構築。`receiptSnapshot_()`／`operations_from_cells()` | case/source/item対応が一意。イベント行番号では選ばない。入力・機械列・候補・policyを安定読取できなければ停止 |
| P2 | UUID→case/source/operations/context/digestをprivateへ固定。`ValidationJournal.prepare()` | private CAS＋独立read-back完了が先。登録受付ではない。最新ticket管理の追加が必要（4節） |
| P3 | 固定ticketの専用checkboxをSheetsへ投影 | private成功後だけ。入力・メモを変更しない。応答不明なら停止。人のsortと公開処理の競合は未解決（8節） |
| C1 | iPhoneの単一登録イベントからUUIDを抽出。`receiptValidationEdited()` | 指定BOOK/sheet/列・単一セル・既知trigger、同UUIDのready→registerのみ。逆方向は無処理、通常TRUE・oldValue欠落・未知UUIDは拒否 |
| C2 | UUIDからprivate固定ticketを取得し、そのcaseを独立再読取 | `下書き準備済み`かつ最新公開ticket。再構築snapshotの全体一致が必要。古いUUIDで新入力を取り込まない |
| C3 | `accept_prepared()`でprivate CAS：下書き準備済み→受付済み | **受付の唯一の確定点**。同一JSON内でUUID・案件/原本重複と最新ticketを検査。読戻し不明ならdispatchしない |
| C4 | 受付成功した呼出しだけUUIDを1回dispatch | 輸送上の固定refと、業務input `request_id`だけ。明細・原本・case・メモをGitHub payloadへ送らない。拒否・応答喪失・クラッシュなら再送しない |
| C5 | Actions：`execute_accepted_request()`→受付済み→検証中CAS | private journalだけから要求取得。CAS勝者だけ検証。旧`execute_request()`はオフライン回帰用で接続しない |
| C6 | `IndependentReceiptReader`＋既存`review()`／`registration_plan()` | 原本hash/version/ACL、候補・取込・policy・入力を独立再読取し照合。変更・不明は保留。印字意味の照合の限界は6節 |
| C7 | 終端結果を同じprivate journalへCAS・read-back | FINALは修正が必要／保留／検証済み・未記帳。posting_authority=false、financial_write_count=0をschemaで強制 |
| C8 | `result_cache_entry()`→自己完結key/valueを非表示cacheへappend→同じ「確認」で完全一致表示 | private終端確定が先。入力行へ座標書戻ししない。欠落・競合・新旧不一致は表示待ち。表示障害でC3～C7を再実行しない |

P1～P3は既存設計にある背景の下書き準備であり、登録チェックを押してから新snapshotを作って代用しない。商品編集後の背景準備が未完了なら登録不可を示す。PCメニューや利用者によるUUID入力を追加しない。**背景準備の起動・完了表示・公開競合防止は未接続であり、今回完成扱いにしない。**

イベントが複数届いても安全性は「受付最大1件・claim最大1勝者・終端結果は同UUID内で不変」。到達保証・即時実行・分散exactly-onceは主張しない。受付確定後・dispatch前に落ちれば処理が進まない状態を許容し、自動回復writeを追加しない。

## 3. 既存実装と不足機能の対応

以下の10ブロック中7ブロックに再利用できるロジックがある（**70%は機能ブロック数の目安。変更不要率・LOC再利用率・実接続完成率ではない**）。実接続は未成立。既存コードにあるportを実APIへ渡すだけでは完成しない。

| ブロック | 既存資産・関数 | 再利用／不足の差分 |
|---|---|---|
| 1 イベントidentity | `receiptValidationEdited()`、Gate B `receiptGateBClassify_()`／`receiptGateBObserve_()`のshape/target検査 | 純粋ロジック再利用。Gate Cの実BOOK/sheet/列/trigger guardはport未実装。Gate B historyを重複台帳にしない |
| 2 セル→snapshot | JS `receiptSnapshot_()`／`receiptCanonical_()`、Python `operations_from_cells()`／`digest()` | 再利用。実46列のdecoderとUnicode・数値正規化の差分が未実装 |
| 3 準備ticket | `receiptPrepareDraft()`／`ValidationJournal.prepare()` | 再利用＋最新ticket index、準備読取に結び付いたexpected ETag、失効済ticket拒否を追加。公開までの安全性は未解決 |
| 4 CAS受付・重複防止 | `accept_prepared()`／`save()`／`validate_journal()` | 状態機械再利用。transport形式の不一致・同居envelope・実ACL・全層retry=0対応が必要 |
| 5 一度だけworker claim | `execute_accepted_request()`／`transition()` | 再利用。同時claimの強制競合試験を追加。Actions concurrencyは補助で、受付CASの代わりにしない |
| 6 独立validator | `IndependentReceiptReader.__call__()`／`review()`／`registration_plan()`／`preview_item_corrections()` | 再利用。一般・purchase限定。印字内容の意味照合、正規の候補/取込/total_confirmationの供給元は別途確認 |
| 7 終端cache契約 | `project_result()`／`result_cache_entry()`／`cached_result()` | 純粋ロジック再利用。UNKNOWNは永続cache不可。最新submission照合・差分noticeの実表示は未接続 |
| 8 private実adapter | `DriveRegistryIO`のv2 If-Match方式 | API方式のみ再利用。PDF専用config・bytes interfaceをそのまま接続できない。非金融の薄いwrapperが必要 |
| 9 実Sheets読取・投影 | `google_clients.py`の既存Google API構築、Gate B列契約 | 接続は新規。金融`SheetsDB`の構築や本番`sheet_values`を流用しない |
| 10 UUID→Actions実入口 | category-submitのHTTP形式、category workflowのUUID input pattern | 非金融workflow／実行entrypoint／scope・主体の確認が必要。既存category workflowは金融環境・Secretsを読むため転用不可 |

`receiptValidationEdited()`は現在、逆方向のready値をnullで無処理にする。Gate Bの`REJECT_EVENT_IDENTITY`と同じ返却文字列ではないが、受入判定は新規受付0件で揃える。見かけの戻り値統一のための変更は不要。

## 4. private CAS実adapterとsnapshot確定処理の具体的差分

### 4.1 private単一JSONのadapter

現行`ValidationJournal`は`read_versioned() -> (dict, strong ETag)`、`replace_versioned(value, tag)`を要求する。一方、`DriveRegistryIO`は`(bytes, tag)`と`replace_versioned(before_bytes, tag, after_bytes)`。さらにPDF専用の4 ID・owner_digest configを要求する。したがって引数の付替えだけでは足りない。

最小候補は受付用PRIVATEの**1ファイルだけ**に、保護case catalogとjournalを別namespaceで格納するwrapper。既存probeを使わず、別DB・別ファイルは増やさない。

- 外側はschema、固定catalog、journal。catalogには原本identity、正規の候補/取込/policyの参照または承認済み固定データ、そのdigest、既存total_confirmationの出所を保持する。編集Sheetから分類・actor_id・候補原本を信頼して生成しない。
- 内側journalの既存schemaを明示改訂し、`active_prepared`（case→現在の準備UUID）を追加する案。既存strict validatorへ勝手にextra keyを挿入しない。初期化は別承認時だけ。今回のPRIVATEは記録上unconfiguredで、現形式を無条件にmigrationしない。
- loadはACL/parent/MIME/trashを検査、metadata→media→metadataを安定読取、サイズ上限・重複JSON key・非有限数を拒否。catalogとjournalを別々のファイル状態として返さない。
- saveは読取時の**同一ETagの外側bytes**を保持し、catalog完全不変・journal generationの増分・許可遷移を検査して、同じIDへIf-Match PUTを1回。最新envelopeを再読込して旧要求を無条件mergeしない。元catalogを上書きしない。
- 保存後は独立GETのenvelope全体・強ETag更新を確認。別の無関係な更新でread-back比較が失敗しても保守的UNKNOWNで停止。後続GETは照合だけ。
- catalogのcontext digestはcatalog内容に固定し、journal更新やファイルETagを含めない。`private_cases()`の2回比較に外側ETagを使うと並行journal更新で保守的停止が増えるが、安全を緩めるため比較を省略しない。
- Python wrapperとApps Script portは同じschema・canonical化・CAS契約を使う。JS側にも受付CASの薄い実装が必要。Script Propertiesにjournalを二重保存しない。

`DriveRegistryIO._request()`はredirectを抑止するが、既定`AuthorizedSession`は401でrefresh/retryする余地がある。現コードだけで全層再送0とは言えない。[google-auth仕様](https://google-auth.readthedocs.io/en/latest/reference/google.auth.transport.requests.html)も再認証試行を持つ。writeは事前token準備、認証再送・HTTP retry・redirect・resumableを抑止したtransportに限定し、401/timeout時の実送信回数を試験する。既存PDF実装を一括変更せず、受付wrapperへ限定する。

### 4.2 最新ticketとsnapshotの確定

既存snapshotはcase/source/operations/context_digestとそのdigest。`IndependentReceiptReader`は原本・Sheet・catalogを2回照合するが、Sheetsのタップ時snapshotやrevision CASは持たない。

必要な最小追加契約：

1. 準備開始時のprivate ETagを保持し、case/source/item bindingを一意に解決して通常セルを再構築。sheetから取得した数値型、checkbox bool、種別/採否/帰属、空追加枠を明示変換する。Gate BのAK:ATだけで正規の取込・保存候補・policyが存在すると仮定しない。
2. snapshotが安定した時だけ、そのETagを条件にticket追加＋active_prepared更新を同じprivate CASで行う。現`prepare()`はsnapshot取得後に新しくloadするため、このexpected ETag結合がない。遅延した旧準備が新ticketを上書きしないよう追加する。
3. UUIDは一度固定したcase/source/snapshotへ永続対応し、内容の書換え・再arm・再利用禁止。新しい下書き準備は古いPREPAREDを削除せずactive indexから外す。受付時はactive一致も必須。同case/sourceの受付中・検証中・永続UNKNOWNがあれば新準備を受理しない。
4. 初回準備成功後だけcheckbox投影。商品編集で現入力が変わった場合、旧snapshotのまま受付けず停止。新準備が必要でも、不明な受付の再送として別UUIDを発行しない。今回はUUIDを発行しない。
5. C2の再構築とC3 CASの間に人が編集する競合は跨サービス原子性で消せない。C3が確定する対象はprivate固定snapshotだけ。workerの再読取で変更が検出されたら保留。タップ瞬間の入力、すべての中間編集（ABA）、最終読取直後の編集まで一致すると保証しない。
6. `project_result()`の既存optional `submission`照合を実adapterでは必須にし、現在選択されたUUID/source/snapshotに完全一致する結果だけ表示。旧結果は過去snapshotの結果として区別し、新入力へ成功を継承しない。普通のセルにはcanonical digestを計算する実接続がまだない。非同期に更新された古いdigestを「現入力一致」の証明にしない。

Sheet入力は元から未信頼。digestは内容照合であって本人承認ではない。2回readの一致やScriptLockだけで所有者の部分sort・編集を禁止できない。[Sheets batchUpdate](https://developers.google.com/workspace/sheets/api/reference/rest/v4/spreadsheets/batchUpdate)は同一リクエスト内を原子的に適用するが、共同編集の影響を明記する。[ScriptLock](https://developers.google.com/apps-script/reference/lock/lock-service)はscript区間のロックであり人の編集をロックしない。

## 5. 状態遷移・重複・UNKNOWN

| 現在の永続状態 | 許す操作・次状態 | 拒否／停止する操作 |
|---|---|---|
| unconfigured／欠落 | 別承認された初期化のみ | 自動作成・既存probeの転用・イベント受付 |
| 下書き準備済み | 最新ticket＋snapshot一致でCAS→受付済み | 非active ticket・逆方向・別UUIDからの推測受付。準備だけでworker実行 |
| 受付済み | C3勝者によるdispatch 1試行。worker CAS→検証中 | 同UUID再受付、同case/sourceの別UUID、dispatch再送 |
| 検証中 | claim勝者によるCAS→FINAL | 二重claim・二重検証、受付への巻戻し |
| FINAL | queryと対応する終端cache表示 | 同UUIDの結果変更・再実行。完全同snapshot別UUID受付 |
| 結果不明・照合待ち（永続） | GET照合・停止 | 自動復旧遷移・別UUIDの迂回。既存`transition()`にはUNKNOWNからの再開遷移がない |

既存の重複判定は同UUIDの再利用を常に拒否し、別case同sourceを拒否。同caseまたは同sourceの未終端/UNKNOWNをブロックし、FINALでも同snapshotを拒否する。FINAL後の変更snapshotは別の明示登録候補になり得るが、最新ticket規則と安全な再準備が前提。今回その運用を有効化しない。履歴をTTL削除して重複防止を解除しない。既存1000 requests上限に達したら停止し、自動prune機能を追加しない。

UNKNOWNを2種類に分ける。保存失敗等の戻り値UNKNOWNは「永続stateがUNKNOWNになった」とは限らない。`query(run_completed=True)`も、独立したrun完了観測を条件にした一時projectionで、repair writeではない。経過時間だけでは使用しない。永続UNKNOWNへ遷移させた場合は既存状態機械どおり再開しない。

| 故障箇所 | 永続状態の可能性 | 許す後続／禁止する後続 |
|---|---|---|
| prepare PUT/read-back不明 | 未保存またはPREPARED | GET照合、checkbox非公開。別UUIDで自動準備し直さない |
| accept PUT/read-back不明 | PREPAREDまたは受付済み | dispatch 0、GET照合だけ。同UUID再受付・新UUID迂回なし |
| accept成功後、dispatch前のクラッシュ | 受付済み | 受付を保持。自動dispatch回収なし。進行しないケースとして受入 |
| dispatch拒否／応答喪失 | 受付済み、検証中、FINALのいずれもあり得る | 再送0。private/runの独立GET。HTTP成功だけで検証成功にしない |
| claim PUT/read-back不明 | 受付済みまたは検証中 | 当該呼出しは検証しない。以後の再実行・別worker投入を自動化しない |
| validatorの読取不明／不整合 | 検証中 | 既存workerが保留結果を保存できればFINAL保留。保存不明ならUNKNOWN。失敗理由を成功へ補完しない |
| FINAL保存/read-back不明 | 検証中またはFINAL | GETで実FINALが読めれば同結果表示。再検証・再確定PUTなし |
| cache append応答喪失 | private FINALは保持 | まずGETでcache照合。append自動再送なし。同一内容の重複行は許容、異なる値なら表示停止 |

同時受付はprivate同一ETagの唯一勝者だけdispatch。敗者は412等で停止し、reload後の自動retryをしない。workerも同様。ScriptLock・Actions concurrencyが失敗してもprivate CASで守る。観測ログが削除・改変されても受付/claimの件数は増えない設計とする。

## 6. validatorの保証範囲と実行入口

`IndependentReceiptReader`は原本bytesのSHA-256とsource version/MIME/parent/ACL、candidate/import/policy digest、case一意性と入力snapshotを照合する。`preview_item_corrections()`は整数円の行金額合算（数量を再乗算しない）、追加/除外、数量変更、カテゴリ、税/値引きの符号・帰属・内税二重加算、架空調整を独立検査する。`registration_plan()`は元snapshotの完全一致を要求する。

**不足：画像のhash一致は修正内容の印字一致ではない。** 現workerは画像をOCR/意味解析せず、`confirmed`から作るevidenceも入力者申告。合計が一致していても2商品への誤った金額配分や名称取り違えを原本から自動検出できるとは言えない。`total_confirmation.actor_id`もSheetのcheckbox/e.userから捏造できない。

最小の合成受入では、Gate Aで固定した原本hashと、原本から独立確認済みの500/868/1,368等の期待明細を非公開fixtureへ結び付け、worker出力を試験oracleと比較する。表示Sheetから期待値を作らない。この試験oracleだけで汎用の原本意味検証機能が完成したとはしない。一般レシート全体の印字意味をbackendで検証する必要がある場合、既存`receipt_local_ocr.read_tokens()`は再利用候補だが、明細対応・不明時保留の接続は未設計/未検証。新OCR基盤や外部AIを追加しない。厳密な印字一致まで必須の一般運用は、この未達のままGOにしない。

実行入口はUUIDだけを読む非金融CLI→`execute_accepted_request()`。金融`SheetsDB`、writer、authority発行、旧本番CLIを構築/呼出ししない。Google APIは対象BOOK/PRIVATE/SOURCEを固定allowlistし、sourceはGETのみ、PRIVATEだけCAS、BOOKは承認された機械表示領域だけに能力を限定する。資格のscopeだけでは資源単位の権限隔離が成立しないことにも留意する。

GitHubの[workflow_dispatch仕様](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#workflow_dispatch)はdefault branch上の定義を要求する。基準head/base両方のtreeに専用検証workflowはない。**main変更禁止の今回、起動可能な専用Actions入口は作れない。** 将来の最小default-branch差分・実行ref/SHA・資格の供給方法を個別レビューするまでdispatch不可。金融workflowや`pull_request_target`等を使って迂回しない。

既存JSはdispatch portの戻り204を成功とするが、[現行REST仕様](https://docs.github.com/en/rest/actions/workflows#create-a-workflow-dispatch-event)はAPI version 2026-03-10で200＋run情報を掲載する。既存category patternは2022-11-28指定。採用するversion・返却形式を固定し、adapterで正規化して試験する必要がある。現行仕様だけで既存コードの204を安全に使えると推測しない。dispatch権限・PAT保存/失効・Apps Script実scope・SAのActions側利用可否は今回未確認。認証情報を探索・変更して埋めない。

## 7. Gate C受入テスト設計

既存欄のPは `tests/test_receipt_sheet_validation.py`、Jは `apps-script/receipt-validation/Code.test.cjs`、Bは `apps-script/receipt-gate-b/Code.test.cjs`、Dは `tests/test_pdf_intake_registry.py`。以下は既存テストで確認できる内容と追加試験の切分けであり、今回は実行していない。新規試験も、オフライン実装の承認・Google接続の承認・実機操作の承認をそれぞれ得てから行う。

| ID／試験 | 成功条件 | 既存テストで充足する部分 | 新規に必要な部分・証拠 |
|---|---|---|---|
| T01 正常登録 | 対象case受付1件、claim1件、同snapshotの終端1件 | P `test_private_only_accept_and_worker_survive_absent_sheet_uuid_and_display_failure`、J one explicit registration | 実decoder→CAS→UUID→worker→cacheの結合。原本oracle 500/868/1,368、差額0、入力/メモ保持 |
| T02 同一UUID再送 | 追加受付/dispatch/validator起動0 | P `test_same_uuid_worker_reexecution_refused_after_private_only_accept`、J one explicit registration | プロセス再起動・ログ消去を挟んでもprivate履歴で拒否。永続bytesと世代数比較 |
| T03 逆方向 | register→readyで受付/dispatch0、再armなし | B same UUID repeated、J ordinary edits | Gate C結合でready無処理でもprivate不変。旧値/通常TRUE/missing oldValue/複数セルも拒否 |
| T04 観測3イベント連続 | ready→register、register→ready、ready→registerで受付最大1、dispatch最大1、金融0 | Bがこの順序を含む。Jは個別replayのみ | Gate C ports＋private CAS＋workerまで同じ順序を通す新結合。各段でstate/snapshot不変確認。原因特定とは別試験 |
| T05 同時受付 | 同ETagの競合で成功最大1、敗者は安全停止・再送0 | P `test_competing_prepared_uuids_for_one_case_only_one_private_acceptance`／`test_competing_workers_and_weak_cas_and_journal_corruption` | バリアで同時点を強制したJS/Python間・別process競合、受信順反転。実adapterでは送信回数と412確認 |
| T06 同時worker claim | 同UUIDで検証本体に入るworker最大1 | P reexecution／claim response lossは逐次 | 2 workerを受付済みquery直後で同期待状態に揃える試験。claim勝者だけreader/validator呼出し |
| T07 CAS stale | 古いETagで変更不可、勝者bytes保持 | D stale ETag、P synthetic conflict。独立probe実412は既存証拠 | receipt envelope/schema/実upload URLでのtransport試験。client precheckのHTTP_412とserver 412を区別 |
| T08 通信結果不明 | prepare/accept/claim/FINAL/dispatch/cache各箇所で自動再送0、二重確定0 | P ambiguous_accept／terminal_response_loss／readback_failure／worker_claim_response_loss、J network unknown | 新wrapperの前/後commit故障、401 refresh、timeout、redirect、read-back喪失を注入しHTTP送信数計測。UNKNOWNの永続/一時を照合 |
| T09 snapshot一意 | ticketのcase/source/context/operations/digestが一意、準備前/不明なら公開0 | P private_prepared_ticket、ordinary_product_cells、J preparation publishes no checkbox | 実46列、数式値/空欄/bool/文字列数値・小数・Unicode/emoji・JS/Python canonical一致。余分なキー/NaN拒否 |
| T10 最新ticket | 旧PREPAREDや遅延prepareで新入力を取り込めない | **未充足**。現prepareにactive規則なし | active index CAS、expected prepare ETag、旧ticket非削除・拒否、FINAL後の別入力、ABA限界を検証 |
| T11 入力変更 | 独立読取で検出した受付後/検証中の変更は保留。後編集を保持し旧結果を新入力成功と表示しない | P stale_source_candidate_policy、partial_sort_concurrent_edit、result_cache_later_edits | C2直後/C3直後/最終reader直後/表示直前を故障点にした結合。最終読取後の変更は登録時snapshotの結果と区別し、現入力digestの古さを成功根拠にしない |
| T12 validator | 原本・金額・明細を独立照合、不一致/未確認は成功不可 | P mismatch_two_examples(+138/+28)、product_operations、tax_discount、initial_medical、unconfirmed_total。`test_receipt_item_correction_plan.py`も再利用 | 実source GET hash/version/ACL＋候補/取込/policyの出所。印字期待明細は独立oracle。同合計で配分だけ誤った明細は既存の汎用保証外として記録し、意味検証必須なら保留 |
| T13 結果表示 | private FINALとcase/source/UUID/snapshot完全一致、「検証済み・未記帳」 | P result_cache／projection_missing_duplicate／transient_unknown | native lookup・cache全列同時append・同key異値・欠落・部分sort・古いsubmissionの拒否。入出力セル差分とprivate結果比較 |
| T14 cache応答不明 | private不変、再dispatch/再検証0、UNKNOWNをcache永続化しない | P unknown_is_transient／terminal_query_after_transient_unknown、J display failure | 実append前後の応答破棄→GETのみ、同一重複は同結果、競合値なら表示待ち。照会でjournal write0 |
| T15 金融write | 常に0、flagを後からONにしても記帳しない | P `zero_external_writes`がsocket/SheetsDB構築trap、replay_write_flags、no_financial_entrypoints | 新CLI/ports/workflowでwriter構築・金融URL/stateアクセスもtrap。対象外Google write、authority、AI送信、原本移動も0 |
| T16 envelope/ACL | catalog不変、anchorとjournalを同ETagで保存 | D broad ACL／duplicate JSON key／readonly transport | schema/key/size/parent/MIME/owner/SA改変、catalogだけ変更、別namespace欠落を拒否。第4資源作成0 |
| T17 実機sort・行移動 | 別case受付/結果0、入力/メモ移植0、曖昧なら停止 | B event後sort・partial binding、P sort/duplicate/deleteは合成のみ | **別承認のiPhone実機試験が必須**。全行sort、部分sort、行挿入/削除/移動、checkbox公開中sortを分けて記録 |
| T18 実行経路 | 専用非金融workflowだけ、固定ref/SHA、UUID inputのみ | J source strings never enter payload、P no_financial_entrypoints | default-branch条件、API versionと200/204正規化、403/404/401/応答喪失、queue遅延/中断。既存金融workflow未使用 |
| T19 古い結果/イベント遅延 | 新ticket準備/新入力後も旧UUIDが新入力や新結果を上書きしない | P optional submission stale拒否・後入力保持のみ | 実adapterでsubmission必須、終端前後を逆順配信、active indexとlookupの一致。座標write0 |
| T20 進行停止 | accept後dispatch前・claim後validator前のcrashで二重処理なし | P response lossは部分充足 | 各境界でprocess停止。処理未完をUNKNOWN/照合待ちとして報告し、時間によるretry/unlockなし |

各試験の記録には、初期・最終private state/generation、受付/claim/dispatch/validator/金融呼出し数、snapshot一致、対象外write数、UNKNOWNの種別、既存入力・メモ保持を含める。公開PRには実ID/UUID/原本hash/認証情報/詳細payloadを載せず、件数・判定・fixture論理名だけを保存する。ライブCASを伴う試験は今回未実施で、旧probe PASSをreceipt adapter PASSに読み替えない。

## 8. Gate B未達と解決していない安全問題

Gate Bで再利用する事実：iPhoneの商品編集・金額修正、合計1,368/差額0、商品/メモ保持、初回UUID/oldValue/案件binding、正しいhandler、不正/重複イベント拒否はPASS。1回の操作報告に対して、ready→registerのOBSERVED_UNAPPROVED、逆方向のREJECT_EVENT_IDENTITY、再正方向のREJECT_DUPLICATEという**3件の独立trigger実行**を確認済み。

**原因は不明のまま。Gate B総合NO-GOは変更しない。** 配送・同期・利用者追加操作のどれかと決め付けない。T04はこの系列に対する受付耐性であり、原因究明・実機一操作対応のPASSではない。既存停止状態（trigger0件の提示画像、disabled保存報告）と使用済UUID/ログ/商品/メモを保持。今回再確認のためにGoogleを変更しない。

| 未解決 | Gate Cで保証できる範囲 | 解決/検証方法・GOへの影響 |
|---|---|---|
| 1操作→3実行の原因 | 永続CASで系列に対する受付上限1を設計・合成試験可能 | 既存実行/操作証拠の読取照合。新実機試験は別承認。原因解消済み・Gate B PASSとはしない |
| sort・行移動 | UUIDからprivate caseを選ぶため、古いイベント座標で別caseを選ばない | 既知binding不一致は拒否可能。ただし値だけが動く部分sortは整合した別入力に見える場合があり、現ロジックでは全検出保証なし。T17実機必須、合格だけでも任意の部分sortの形式的保証にはならない |
| checkbox公開と人のsort競合 | private先行で未保存ticketを受付けない | case検索後に座標へ規則を書いている間に行が動く競合は未解決。read-backだけではその間のタップや誤った表示対象を排除できない。安全な公開/有効化条件を証明するまで実接続NO-GO |
| タップ瞬間のsnapshot | 登録対象をprivate固定snapshotに限定できる | 単一セルeventには行全体のsnapshotがない（[公式event仕様](https://developers.google.com/apps-script/guides/triggers/events#edit)）。後読取からタップ時値を推測しない。連続編集・ABA・最終read後の編集は保証外として明示 |
| 最新ticket・現入力一致表示 | 新active規則・submission必須照合で古い処理の適用を制限 | 現codeにactive管理なし。新digestを行へ座標書戻しすれば同じsort競合を作る。投影が現在入力を保証する方法は未証明。未確認なら「登録時snapshotの結果」と表示し現入力の成功を主張しない |
| 原本内容の意味照合 | 原本の同一性・計算・保存候補との対応は検証可能 | 印字正解と修正明細の一致は一般化未達。6節のoracleと実運用の保証を分離し、要件を満たさない成功表示は禁止 |
| PRIVATE初期化/実adapter | 旧probeで単一JSON server CAS実測済み | 受付PRIVATEのenvelope・catalog出所・schema migration・JS/Python相互運用は新規レビュー/試験が必要。無承認JSON更新なし |
| Actionsと資格 | 新インフラ不要の見通し | 専用workflowのdefault-branch配置と資格供給は未解決。main/Secrets等を変えず完成できるとは言えない。個別承認まで接続不可 |
| 実配置code/trigger identity/scope | repositoryの純粋検査を再利用できる | 実project全文・trigger UID独立照合・実scope確認は未完。e.user/installerを本人承認へ昇格しない |

## 9. 承認後に必要となる最小変更ファイル案

**以下は将来の変更候補と新設関数の設計名。今回追加・実装していない。** UI仕様の変更、ライブ設定、default-branch配置はコード実装とは別承認。Gate B observerの本体・manifest・ログは変更しない。

| ファイル | 変更する関数／新設候補 | 理由・概算追加行 |
|---|---|---|
| `app/receipt_sheet_validation.py`（既存） | `empty_journal()`、`validate_journal()`、`ValidationJournal.prepare()`／`accept_prepared()` | schemaとactive_prepared、準備時expected ETag契約。既存`transition()`／worker／cacheを再利用。約60～120行 |
| `app/receipt_sheet_validation_io.py`（新規候補） | `PrivateJournalTransport.read_versioned/replace_versioned`、`validate_private_envelope`、`read_sheet_bundle`、`decode_review_cells`、`publish_terminal_cache` | 既存Drive v2方式の薄いwrapper、catalog不変、実46列mapping、metadata正規化、表示先限定。約250～400行 |
| `app/receipt_sheet_validation_cli.py`（新規候補） | `main()`、`run_validation()`、`query_result()` | UUIDのみ、実reader/journal組立、検証/照会の分離。writer経路なし。約60～100行 |
| `apps-script/receipt-validation/Code.gs`（既存） | `receiptPrepareDraft()`、`receiptValidationEdited()` | 最新ticket/expected ETagのport契約。snapshot変換の意味は既存を維持。約20～50行 |
| `apps-script/receipt-validation/Ports.gs`（新規候補） | `receiptValidationOnEdit()`、`receiptDraftEdited()`、`receiptReadCaseById_()`、`receiptReadPrivate_()`、`receiptReplacePrivate_()`、`receiptDispatch_()`、`receiptPublishPrepared_()` | target/shape/trigger guard、単一JSON CAS、下書き準備とイベントの接続、固定UUID dispatch。新サーバーなし。公開競合が未解決なので見積は暫定。約250～400行 |
| `apps-script/receipt-validation/appsscript.json`（新規候補） | manifestのみ | currentonlyに加え外部HTTP/Drive等が実際に必要かscopeと主体を精査。Gate Bのscopeを勝手に拡張しない。約15～25行 |
| `.github/workflows/receipt-sheet-validation-only.yml`（新規候補） | 専用非金融job | UUID入力、固定SHA、debug/成果物へのprivate露出防止。既存金融env/Secretsをコピーしない。default-branchと資格供給が別ゲート。約50～80行 |
| `tests/test_receipt_sheet_validation.py`（既存） | T04/T06/T10/T11/T19/T20等 | coreの同時実行・遅延・新旧入力の穴を補う。約140～220行 |
| `tests/test_receipt_sheet_validation_io.py`（新規候補） | transport/decoder/envelope/CLIの契約試験 | T07～T09/T12～T16/T18、実HTTPを使わないfault injection。約220～350行 |
| `apps-script/receipt-validation/Code.test.cjs`（既存） | 3イベント結合、ports fault/prepare公開試験 | 同時CAS・公開不明・古いticket・version応答正規化。約120～180行 |

将来の概算は**製品/設定7ファイル、追加約705～1,175行、テスト3ファイル約480～750行、計10ファイル・約1,185～1,925行**。既存vendor/依存追加なし、`pdf_intake_registry.py`・金融writer・既存本番workflowは変更しない案。文書/非公開承認manifestの整理は別。公開競合・原本意味照合・資格供給の解決前なので、この範囲で必ず実装可能とは確約しない。新機能を足して未解決を隠さず、超える場合は設計差分として止める。

## 10. 推奨順序と実装GO／NO-GO

1. **先に残す設計ゲート**：checkbox公開/部分sort、現入力と登録時snapshotの表示区別、原本意味照合の保証範囲、Actions default-branch/資格供給を確定。新サービスは提案しない。確定できない点はNO-GOのまま。
2. **別途実装承認後のオフライン最小差分**：private envelope＋active ticket＋expected ETag、実46列decoder/canonical化を先に実装・契約試験。既存validator・claim・cache関数の書換えを最小化。
3. **同じくオフライン**：JS portsとCLIのモック結合でT01～T16/T18～T20。3イベント・同時受付/claim・UNKNOWNの各境界を先に合格させる。金融接続trapを維持。
4. **Google/Actionsの個別接続承認後のみ**：固定PRIVATE/BOOK/SOURCEと実scope/資格を照合し、専用入口の承認差分を確認して隔離合成のadapter受入。旧probeや既存金融workflowを転用しない。
5. **実機の個別承認後のみ**：iPhoneの編集→登録→同案件結果、sort/行移動/後編集保持を検証。Gate B未達の扱いを再判定。未記帳を最終状態に固定する。

**見通し：新規インフラなしで、既存の非金融ロジックを中心に組み立てる余地は大きい。ただし実接続とsnapshot公開の安全条件は未完成。現時点の実装開始はNO-GO／承認待ち、実行は技術的未解決もありNO-GO。** Gate Bの原因不明を解消済みにせず、Gate B総合NO-GOを維持する。今回の文書提出後も、自動で次段階へ進まない。

今回のSTOP維持：Gate Cコード実装、Apps Script変更/実行、UUID発行、trigger作成、iPhone再試験、Google Sheets/JSON書込、Actions dispatch、Secrets/Variables/main/validated SHA変更、金融writer接続/write、追加インフラ、PR mergeはすべて0。
