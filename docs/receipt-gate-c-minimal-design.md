# Gate C：既存CAS受付・独立検証・同一Sheets表示の最小設計

**最新：18節は `835a4bf` 基準のcatalog不足解消案。対象caseの6項目はA取得0・B候補1・C取得不能5。新規の合成データfixtureは必要だが、本人確認/actorはfixtureで代替しない。既存原本・生成関数を再利用し、permit再利用防止の責任と必須/省略可能作業を具体化。今回の変更は本Markdownだけ。実接続NO-GOを維持。**

**最新：17節で `bab822e199c533830a1ca1b6c1cfa42b6e81dd1d` 基準の実接続前条件を確定。停止latchの再生成時消失、既定認証層によるPUT最大3送信をmockで確認。信頼catalogは必須情報不足。追加は診断テストと設計記録のみ。Gate B総合／Gate C実環境／本番NO-GOを維持。**

**最新：14節に第2工程の46列decoderを記録。独立提供の期待bindingと照合する純粋Python関数と合成テストだけを追加。新規82件、既存Python219件、Node16件成功。値だけの部分copy/sortは検出保証外。Gate B総合／Gate C実環境／本番NO-GOを維持する。**

**最新：13節の純粋ロジック修正で、ETag取得順・expected_tag引渡し、MVP数量、名称規則をPython/JSで統一。許容例のsnapshot/digest一致と不正入力の両側拒否をローカル試験済み。実adapter・Google接続は未追加。Gate B総合／Gate C実環境NO-GOは維持。12節は修正前の監査記録として保持する。**

**最新：12節の契約監査でETag引渡し・準備順序・数値canonical化・名称正規化の不一致を確認。既存ローカル回帰は成功したが、言語間契約の成立とは判定しない。修正案の提示までで停止し、製品コードは変更していない。Gate B総合／Gate C実環境NO-GOを維持。**

**第1工程のオフライン実装記録は末尾11節。Python coreとmockテストだけを追加承認に基づき更新した。以下1～10節は基準commit時点の設計・未達記録として保持する。Gate B総合NO-GO、Gate C実環境・本番接続・金融write NO-GOは継続。**

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

## 11. 第1工程：Python coreのみのオフライン実装（2026-11-08）

利用者の追加承認により、基準 `072267c0258009fb7dfd18346505bb570c22dc6d` から既存 `app/receipt_sheet_validation.py` と `tests/test_receipt_sheet_validation.py` だけを実装変更。本節以外の設計記録は履歴として保持する。実46列decoder、private envelope/Drive adapter、Apps Script ports/manifest、CLI、workflowは作成・変更していない。

### coreの差分と再利用

- `empty_journal()`は新journal schema `receipt-sheet-validation-journal-v2` と `active_prepared`（case→最新PREPARED UUID）を返す。要求purposeは従来の `receipt-sheet-validation-only-v1` のまま。
- `prepare(request, *, expected_tag)`はexpected_tag必須。呼出し側は**private load→独立snapshot読取→prepare**の順にする。coreで最新loadの強ETagと一致しなければPUT前に拒否し、その後の競合は既存transport CASで拒否。自動reload/retryなし。
- ticket追加とactive更新は同じCAS・generation増分1回。古いPREPARED履歴は削除/変更せず、activeから外す。同UUIDの再準備は禁止。requestのdeepcopyで呼出し元の後変更から固定snapshotを隔離する。
- `accept_prepared()`は従来の状態・snapshot digest検査にactive一致を追加し、受付への遷移とactive解除を同じCASで保存。旧UUID・旧snapshot・再送は受付不可。
- 従来重複判定を共通の `_check_duplicates()`へ移し、prepare・既存2受付入口で再利用。同caseまたは同sourceの未終端/UNKNOWN、FINALの同snapshot、別case同sourceを拒否。`accept()`は旧オフライン回帰入口のままで、受付時に当該caseの準備ticketを失効させる。
- `transition()`の許可遷移、worker claim、独立reader/validator、終端cacheロジックは再利用。transitionへの変更は旧schema更新禁止のload指定だけ。新writer・検証アルゴリズム・イベントhandlerは追加していない。

### 互換性とmigration条件

旧schema（`schema=receipt-sheet-validation-only-v1`、schema/generation/requestsだけ）はstrict検証・load/query・結果表示を維持する。新コードの更新操作は `receipt_validation_migration_required` で停止する。旧writerはv2を理解しないため、読み書き可能な混在運用にしない。v2のactive欠落/未知UUID/別case参照/消費済参照は不正として拒否し、修復しない。

**自動migration・migration実行ツールは追加していない。Google上のJSONも未変更。** 将来migrationするには、対象・実行主体・停止状態を別途承認し、旧writer/trigger停止、元bytes/強ETagの独立照合、全requestsのUUID/state/snapshot/result保持、generation増分、同一ファイルCAS＋read-backをレビューする。旧PREPAREDの新旧順は推測せずactiveを空にし、旧UUIDは再armしない。受付済み/検証中/UNKNOWNを消去・FINAL化・新UUIDで再送して移行を成立させない。新しい準備は既存の未解決重複ブロックを維持する。実adapter/schema envelopeの承認なしにこの手順を実施しない。

### オフライン検証結果と制限

Python 3.14.7、pytest 9.1.1、合成fixture・既存mock CASだけで、関連5モジュール **156成功・失敗0・4件除外**。変更前の同じ主要3モジュールは85成功、変更後に40ケース追加して125成功、既存の原本候補/validator関連2モジュール31件を加えて156成功。ネットワーク接続は禁止fixture、対象core試験は金融`SheetsDB`構築もtrap。金融writeは0。

追加ケースは正常準備/受付とsnapshotコピー、古いUUID/同UUID再送、stale/weak/missing ETag、遅延prepare、同ETagのprepare/受付/source別名受付/claim競合（barrierで強制）、未終端/UNKNOWNのcase/source重複、FINAL後の重複と新入力、保存前/後/競合/read-back喪失の単一試行、3イベント系列、旧schema照会/更新拒否、active index破損を検査した。3イベントはPython側の合成受付port試験であり、実Apps Script handler・iPhoneの証拠ではない。

実行対象は `test_receipt_sheet_validation.py`、`test_receipt_item_correction_plan.py`、`test_pdf_intake_registry.py`、`test_receipt_review_snapshot.py`、`test_receipt_autonomous_reading.py`。除外した4件は既存の `test_javascript_capture_to_python_worker_hash_and_snapshot_match`（3パラメータ）と `test_ordinary_product_cells_need_no_typed_ids_hashes_or_delimiters`。NodeでApps Scriptソースを評価するため今回の実行禁止に合わせて除外し、テスト本体は変更していない。全体CI/Node回帰/Google実API試験は今回実行対象に含めない。`git diff --check`成功。

残る境界：coreだけではexpected_tagを取得した実時刻やタップ時のSheets内容は証明できない。受付には独立再構築した最新digestを渡す必要があり、既存workerも変更snapshotを保留する。保存応答UNKNOWNは永続UNKNOWNへ自動変換せず例外で停止し、GET照合だけを許す。保存前失敗など永続痕跡のない不明をcore単体で記憶する仕組みはないため、呼出し側の自動再送/新UUID迂回禁止は引き続き必須。

checkbox公開とsortの競合、現入力と旧結果の表示整合、原本印字の意味照合、Actions default-branch・資格供給、実adapter・実機sort/行移動は未解決のまま。第1工程の合成成功を実接続GOにしない。Google/Apps Script/trigger/実UUID/dispatch/Secrets/Variables/main/validated SHA/金融writer/本番データ/mergeへの変更・実行は0。次工程は別途承認待ちで停止する。

## 12. Python／Apps Script契約監査（2026-11-08、調査・ローカル試験のみ）

基準はPR #200の `e0b9ac8e02a432ff008a3a7ec0dd53dbbc0b1190`。開始時headは完全一致、Draft・未merge、mainは `fa7ac9378ef51e4a51258dd6053afe52ed0a2bb8`。ローカルcheckoutのHEADは以前の設計commitを指していたが、core・関連テスト・本資料・Apps Script source/testのGit blobを基準commitのtreeと照合し、作業ファイル内容の一致を確認してから調査した。本監査の変更は本Markdownだけ。

### 12.1 ETagとprepareの契約差分

| 契約 | Python core | Apps Script／既存Node fixture | 判定 |
|---|---|---|---|
| prepare引数 | `ValidationJournal.prepare(request, *, expected_tag)`。expected_tag省略はTypeError、stale/weak/nullは保存前に拒否 | `receiptPrepareDraft(candidate, ports)`は`ports.prepare(request)`を1引数で呼ぶ。ETag引数なし | **不一致**。現在の呼出しをPythonへ直接結び付けられない |
| 入力読取順 | 呼出し側がloadでETag取得→独立snapshot読取→prepare。同関数内のloadは取得済ETagの再照合であり、最初の取得ではない | candidateを引数で受けた後snapshotを計算→fixture UUID取得→prepare。ETag取得portの呼出しはない | **不一致**。後付けの最新ETag取得では遅延した旧snapshotの準備を防げない |
| 最新ticket | v2 journalのactive_preparedとrequestsを同じCASで保存。accept_preparedでactive一致必須 | Node harnessのprepareはrequestをローカル変数へコピーするだけ。acceptPreparedもローカルprepared.stateだけを更新する | **未接続／試験の不足**。現Node成功はv2・active・expected ETagの試験ではない |
| 完了の意味 | CAS成功＋独立read-back後だけ正常return。不明時は例外、retryなし | prepareがthrowすればcheckbox ruleを返さない点は一致。ただしmockはCAS/read-backをモデル化していない | エラー時停止の純粋な制御だけは再利用可能。保存完了の保証は未検証 |
| 要求形式 | request_id／purpose／snapshot。purposeは旧来のvalidation-only v1のまま | 同じ3要素とpurpose文字列 | 対応。journal schema v2とrequest purpose v1を混同しない |

参照：[Python prepare](../app/receipt_sheet_validation.py#L243)、[JS prepare](../apps-script/receipt-validation/Code.gs#L159)、[Node fixture](../apps-script/receipt-validation/Code.test.cjs)、[Python準備fixture](../tests/test_receipt_sheet_validation.py#L426)。実port/binding実装は存在せず、今回も追加しない。

一時的なローカル観測fixtureで既存JS関数を呼んだtraceは `snapshot_read, snapshot_read, fixture_uuid, prepare_args=1`。ETag読取spyは呼ばれなかった。expected_tagを要求するstrict mockは `expected_tag_missing` で停止した。これは監査用mockのエラー名であり、実Python bridgeやGoogleサービスの実行結果ではない。既存JSのprepare試験は1引数を受け入れるstubなので、この不一致を検出せず成功する。

### 12.2 snapshot canonical化とdigest

Pythonの`app/page_receipt_model.py:digest()`はsorted keys・compact separators・ensure_ascii・allow_nan=falseのJSON bytesをSHA-256化。JSの`receiptCanonical_()`はsorted keys・ASCII escapeを行うが、数値文字列表現はJSON.stringifyに依存する。通常セルの数値はPython `operations_from_cells()`とJS `receiptSnapshot_()`で再構築される。**数学的な値の一致だけではcanonical bytesの一致にならない。**

既存のWorld／ordinary_cells／IndependentReceiptReaderを使い、同じ合成candidateを既存JS `receiptSnapshot_()`へ渡す一時ローカルprobeで以下を確認した。probeはstdin/stdoutと既存ソース読取だけで、repositoryへ実装や新規テストファイルを追加していない。

| 合成入力 | snapshot内容の比較（digestを除く） | Python数量表現／JS数量表現 | digest |
|---|---|---|---|
| 数量2・日本語/emoji名称 | 一致 | `2`／`2` | 一致 |
| 数量0.125・日本語/emoji名称 | 一致 | `0.125`／`0.125` | 一致 |
| 数量0.00001 | 一致 | `1e-05`／`0.00001` | **不一致** |
| 数量0.0000001 | 一致 | `1e-07`／`1e-7` | **不一致** |
| 数量10の21乗 | 一致 | `1000000000000000000000`／`1e+21` | **不一致** |
| 名称の前後にU+0085 NEL、数量2 | **不一致** | `2`／`2` | **不一致** |
| 名称の前後にU+FEFF BOM、数量2 | **不一致** | `2`／`2` | **不一致** |

最後の2件はcanonical serializerだけの問題ではなく、Pythonのname.strip()とJSのname.trim()が扱う文字集合の差。NELはPython側で除去されJS側で残り、BOMはJS側で除去されPython側で残る。通常セルの現チェックは上表の数量を一律に拒否していないため、実入力範囲外と推測して除外できない。旧delimited parserの数量範囲は通常セルと同一ではない。

不一致をdigest省略や比較緩和で回避しない。PythonとJSで同じ入力を別snapshotと判断する可能性があり、現契約では正しい入力でも拒否／保留になる。新しい意味照合・writer・schema migrationで埋めない。本監査は上記fixtureでの再現であり、全数値/Unicodeの同値性を網羅証明したものではない。

### 12.3 除外4試験の安全確認と実行結果

事前に以下を読取確認した後だけ実行した。

- 対象は `test_javascript_capture_to_python_worker_hash_and_snapshot_match` の3パラメータと `test_ordinary_product_cells_need_no_typed_ids_hashes_or_delimiters` の1件。すべて既存の合成World・in-memory CASを使い、Node subprocessへ合成JSONだけをstdinで渡す。
- `Code.test.cjs`のimportsはNode標準のfs/vm/crypto/assert/testだけ。fsはCode.gsとstdinの読取、結果はstdout。vmにはGoogleサービス・require・process等を注入しない。評価するCode.gsにも実サービス呼出し・installer・token保存はない。
- `dispatch`は名前だけで、実体は`state.sent`配列へのpush。acceptPrepared/prepare/publishReceiptもメモリ操作か故障注入だけ。実UUIDを発行せず固定のオフラインfixture値を返す。Google identity・本番データ・金融writerを使わない。
- Python試験は既存のsocket接続禁止とSheetsDB構築trapを維持。Nodeの安全性はPythonのsocket monkeypatchが子processを守ると仮定せず、別途上記のsource/path監査で確認した。NODE_OPTIONSを当該テストprocessで解除し、pytest追加pluginの自動読込も無効化した。OS全体のnetwork isolationを測定したという主張ではない。

環境：Python 3.14.7／pytest 9.1.1／Node v22.23.2。

| 実行 | 結果 | 証拠の限界 |
|---|---|---|
| 以前除外した4件だけを選択 | **4成功・失敗0**（98件は選択外） | legacy変更/追加/除外と通常セルinteger例のsnapshot/digest一致。prepare(expected_tag)接続は通らない |
| `node --test apps-script/receipt-validation/Code.test.cjs` | **13成功・失敗0** | 純粋なJS制御・mock portsのみ。実ETag・active index・Googleとは未接続 |
| 11節と同じPython関連5モジュール、今回は除外なし | **160成功・失敗0** | 先の4件を含むため164件と重複集計しない。依存google-genaiのPython非推奨警告1件あり |
| 上表の境界probeと順序trace | 数値3例・名称2例の不一致、ETag欠落を再現 | 監査用の期待された不一致であり、既存160/13件の失敗へ混算しない |

今回の成功は「既存ローカルfixtureは安全に実行でき、その範囲で一致した」という証拠。第1工程で残した4件の未実行は解消したが、ETag順序・全入力domainのcanonical互換性の未達は解消していない。

### 12.4 修正対象と最小差分案（未実施・ここで停止）

| 対象 | 次にレビューする最小差分 | 今回の判断 |
|---|---|---|
| `apps-script/receipt-validation/Code.gs:receiptPrepareDraft()` | 準備開始時ETagを明示保持し、同じ準備内で「ETag取得→case IDからcandidate独立読取→snapshot→prepare」を定義する。既存candidate引数だけでは取得順を証明できないため、case IDから既存readCaseById portを使う契約案を優先。JSのprepareへexpected_tagを明示し、Python bindingではkeyword引数へ対応させる。prepare直前のETag取り直しは禁止 | **コード変更しない**。port契約だけの小修正候補。実adapter/decoder/Google呼出し追加には広げない |
| `apps-script/receipt-validation/Code.test.cjs`のprepare/acceptPrepared fixture、`tests/test_receipt_sheet_validation.py` | 1引数を黙って受けるstubをstrictにし、expected_tag・読取順・v2 active index・stale/UNKNOWN拒否を既存CASの契約に照合する。現4試験は受付snapshotの確認が中心なので、準備経路の契約試験を別に足す | **テスト変更しない**。成功済みfixtureを契約成立の代用にしない |
| `app/receipt_sheet_cells.py:operations_from_cells()`とJS `receiptSnapshot_()`／必要ならreceipt専用canonical境界 | 両言語で認める数量の精度/範囲と整数化、名称前後文字の扱いを同じ規則に固定し、上表の境界を既存言語間試験へ追加。最小の安全案は既存delimited数量範囲等へ揃え、表現の未保証入力を保留にすること。ただし通常セルの許容範囲を狭める変更になるため、採用範囲は別途判断が必要 | **未決・修正しない**。任意floatの一般canonical実装や全用途共通digestの変更を追加せず、影響範囲を限定して再レビュー |
| `app/page_receipt_model.py:digest()`、既存validator/CAS | 多数用途共通のdigestをJS都合で一括変更しない。比較省略・許容誤差・既存結果の再hash/migrationもしない | 今回変更不要。既存の厳密比較とfail-closedを維持 |

ETag/順序の修正候補はJS既存関数・mock契約試験の数十行規模と見込むが、数値/文字列domainの判断前に修正全体の確定行数は出せない。現在の不一致を認めたまま新adapter/decoder/Apps Script実装へ進まない。**調査・文書記録までで停止。Gate B総合NO-GO、Gate C実環境NO-GO、金融write 0。** Google変更・Apps Script実サービス実行・実UUID発行・trigger作成・Actions dispatch・Secrets/Variables/main/validated SHA変更・mergeは0。本記録はCI自動起動を抑止する文書commitだけでPR #200へ保存する。

## 13. 3契約の最小修正（2026-11-08、オフラインのみ）

基準は `37fa555816200601a4b06b645deac8f845ba88c5`。開始時PR headは完全一致、Draft維持。今回の追加承認に従い、既存のreceipt専用純粋ロジック・mock・関連テストと本資料だけを変更した。新規ファイル、実port、46列decoder、CLI、workflow、インフラは追加しない。

### 13.1 準備契約

`receiptPrepareDraft(caseId, ports)`へ変更。事前取得済candidateを引数で渡す旧呼出しは拒否する。順序は **readVersionedで強ETag取得→readCaseById(caseId)→snapshot/digest→UUID fixture→prepare(request, {expected_tag})→正常return後だけcheckbox rule**。case/sourceの取り違えも拒否する。

- readVersionedの戻りはPython loadと同じ論理上の `[journal, tag]`。tagは非空のquoted strong ETagで、weak・欠落・CR/LF混入を拒否。prepareに渡すtagを再取得しない。
- prepare portは**同期的に既存CAS＋独立read-backを完了し、失敗/UNKNOWNならthrow**する契約。Promise/thenableではruleを返さない。実bindingは存在せず、今回も追加しない。
- Node mockはexpected_tag必須、staleをsave前に拒否、ticket/active indexを同じin-memory journalに保持する。before/after/read-back故障でも試行は1回。etag読取spyと順序traceを検査した。
- 言語間試験はJSから出力されたrequest/optionsを既存Python `ValidationJournal.prepare(request, **options)`へ渡し、同じsnapshotのaccept_prepared→既存workerまで実行する。ここにあるのはfixture経由のローカル契約試験であり、Google/backend transportのadapterではない。

### 13.2 receipt専用の数値・名称規則

Pythonの既存 `receipt_sheet_cells.py`へ小さい共通関数を置き、JS側にも同じ規則を置いた。通常セル、旧delimited形式、論理operationsの入力で使い、snapshotを生成する前に整合させる。論理operationsでもcase/source/context_digestを落とさない。既存の共通digestとJS receiptCanonical_のserialization本体は変更しない。

| 規則／入力 | 両言語の結果 |
|---|---|
| 数量：正の有限number、整数部7桁以内、小数部3桁以内 | 最短10進表記を検査。丸め・許容誤差・掛算再計算なし。Pythonの整数値floatは従来どおりintにする |
| 2（2.0を含む）、0.125、0.001、9,999,999.999 | 許容。snapshot内容・digestが完全一致 |
| 0.00001、0.0000001、10の21乗、0、負数、NaN/Infinity、bool、0.1234、10,000,000、2.0000000000000004 | 拒否。prepare/CAS/UUID fixtureへ進めない（非有限値はnative helperでも試験） |
| 名称前後の通常空白、TAB/改行、NBSP、全角空白等の明示した共通Whitespace集合 | 前後だけ除去。内部空白を統合しない |
| NEL U+0085、BOM U+FEFF、残存C0/C1制御文字、孤立surrogate、空名称 | 明示拒否。黙って文字を落として別名にしない |
| 日本語・emoji（ZWJ結合を含む）、アクセント文字 | そのまま保持。NFC/NFKC・大小変換なし。`é`と`e`＋結合アクセント、`商品 A`と`商品A`は区別する |

旧形式はLF/CRLFで行分割し、PythonだけがNEL等を改行として消費する差も避けた。旧形式の空欄edit名称は従来どおり「元名称を維持」であり、空の商品名への変更ではない。通常セル/明示的nameフィールドの空名称は拒否する。この既存の空欄操作を新しく禁止しない。

### 13.3 互換性・回帰と安全境界

事前のrepository調査では、レシート関連の既存正常fixtureにMVP範囲外の正当数量を発見していない。既存プレビューもmin/step=0.001。今回指定された範囲外例の拒否は意図した制限であり、未知の実データを不正と認定したものではない。実データの読取/監査は行っていない。

許容範囲内の6例（2.0、0.125、0.001、最大値、日本語/emoji/結合文字・通常空白等）について変更前ソースと比較した。Pythonの通常セルoperations・旧形式snapshotは各6例で一致し、JSの通常セル/旧形式snapshotは12例すべて**digest込みで変更なし**。基準commitまでこれら元ソースが不変であることはGit blobで確認済み。既存160件の正常/異常回帰も成功した。既存の正当データを壊す事例は確認していないが、この合成比較で実データ全件互換を主張しない。

保存済journalのsnapshot/resultは再正規化・再hash・migrationしない。今回の正規化は新たな入力captureだけに適用する。将来、旧snapshotにMVP範囲外数量や禁止文字が見つかった場合は書換えや自動再受付で補完せず、保留して影響を報告する。

| ローカル試験 | 結果 |
|---|---|
| 従来の関連Python5モジュール | **219成功・失敗0**。既存160＋追加59ケース。通常セル/旧形式/論理operationsの許容・拒否、ETag付き実core連携を含む |
| 既存Node receipt fixture | **16成功・失敗0**。従来13件の意味を維持しprepare fixtureを強化、3件追加（ETag、取り違え/非同期、数値/名称） |
| 構文・whitespace | Python compileall、git diff --check成功 |

試験環境はPython 3.14.7／pytest 9.1.1／Node v22.23.2。既存のnetwork禁止・金融SheetsDB構築trapを維持。Nodeは既存のread-only fs、空vm、crypto、配列/JSON操作だけで、NODE_OPTIONSを当該processから外し外部plugin自動読込を止めた。UUIDは既存固定fixtureだけ、金融write・外部通信・Google操作は0。依存google-genaiの非推奨警告1件は従来どおり。

**判定：今回のMVP範囲・試験済入力では3契約は一致。次のオフライン実装レビューへ進むことは可能だが、その実装・実接続の承認ではない。Gate B総合NO-GO、Gate C実環境NO-GOを維持して停止する。** 任意の数値/Unicode/その他データ型全体のcanonical化を保証しない。実portのCAS/read-back・schema/ACL確認、checkbox公開とsort競合、原本印字の意味照合、実機sort/行移動、専用Actions入口・資格供給は未解決。Google実API、Apps Script実環境、trigger、Secrets/Variables/main/validated SHA、mergeへの変更、Actions dispatch、金融writeは0。PR #200だけへ保存し、CI起動はcommitで抑止する。

## 14. 第2工程：46列decoder（2026-11-08、純粋ロジックのみ）

基準 `e1e020d3b69052147aa52e6945d85296da8f791d` と開始時PR headは完全一致、Draft維持。必須資料（本書、Gate B計画/実施記録、receipt_sheet_cells.py、receipt_sheet_validation.py、receipt-validation/Code.gs）とiPhone継続仕様を確認。作業ファイルのblobも基準treeと照合した。

追加は `app/receipt_sheet_decoder.py`、`tests/test_receipt_sheet_decoder.py` と本節だけ。既存validator・CAS状態機械・共通digest・Apps Script・既存テストは変更しない。Google読取を含む実API呼出し、private JSON更新、CLI/workflow/実port/インフラ追加はない。

### 14.1 APIと列契約の根拠

- `decode_receipt_row(row, *, expected)`：厳密に46要素のlistを受け、既存 `operations_from_cells()` とJS `receiptSnapshot_()`へ渡せるcandidateを返す。case/source/draft.items/original_items/categories/context_digestに加え、独立検算したreceipt_total/corrected_total/differenceと、未変更のメモ/原本表示を返す。
- `decode_receipt_rows(rows, *, catalog)`：A列のcase IDで期待情報を選び、case ID→candidateのmapを返す。行番号で案件を確定しない。case重複・未知case・同source IDの複数caseを拒否。全46列が一緒に動く行sortは結果の対応を変えない。
- expectedは**rowから生成せず独立に渡す**。必要要素はcase_id/source/original_items/categories/context_digest/machine/receipt_total/kind_label。将来private catalogと独立原本・保存候補・policyを照合した値を渡す契約だけを定義し、catalog adapterや永続schemaは作っていない。テストでは完全合成の既存Worldを再利用する。

| 列 | decoderの扱い・根拠 |
|---|---|
| A:I | Gate B計画の案件ID、種類、原本表示、支払総額、表示合計、差額、状態、メモ、観測用登録セル。A/B/Dを独立期待値と照合。Bの実表示文字列は公開資産で一意に固定されていないため、kind_labelを明示して完全一致照合する。テストの「一般」は合成値で、実BOOKの表示を新しく定義したものではない |
| J:R／S:AA／AB:AJ | 3つの9列group：名称・数量・行金額・大分類・小分類・種別・帰属・採否・原本照合。Gate B計画の列順と一致 |
| AK:AN | Gate B fixtureのmachine[0:4]と同じcase/source ID/version/hashの並び。expected.sourceと矛盾がないことを検査し、rowも完全一致させる |
| AO:AQ | 同fixtureの2既存item ID＋new:追加枠IDの3 slot。decoderは既存item 1～3件の包含、一意ID、非既存slotのnew:形式を検査。項目数を46列の外へ拡張しない |
| AR:AS | 同fixtureの試験UUID/run。期待vectorとの一致だけを検査し、新しい登録要求や本人承認にしない |
| AT | Gate Bが64桁hexとして検査する固定binding。**hash算式・意味は公開資産から確定できないため不透明値として期待vectorに固定**。context_digestや新snapshot digestをここへ割り当てない |

列順の根拠は[Gate B計画](receipt-gate-b-event-acceptance-plan.md)と[Code.test.cjsのmachine fixture](../apps-script/receipt-gate-b/Code.test.cjs#L15)、10セル完全一致の根拠は[Gate B classify](../apps-script/receipt-gate-b/Code.gs#L46)。実配置の非公開見出し/式を取得済みとはしない。AK:ATすべてを固定期待vectorと比較し、いずれかの欠落/変更を拒否する。

通常セルの `_binding` は既存どおりdigest([case_id, trusted_source])で作る。sourceのMIME/classification/transaction_kind、原本真正性、context、カテゴリpolicyを編集SheetやATだけから認定しない。C/G/H/Iはauthorityに使わず、IのTRUEやregister表示だけで受付を作らない。返却candidateにもrequest_id/posting_authorityを生成しない。

### 14.2 固定した入力型・変換・検算

| 入力 | 許容／拒否 |
|---|---|
| row全体 | 46個のnative scalar（str/int/float/bool/None）のlistだけ。数式error文字列を数値へ解釈しない。NaN/Infinity、配列/オブジェクト値は拒否 |
| 金額D/E/Fおよび商品行金額 | NUMBERのintまたは整数値floatを、丸めずintへ変換。BOOLEAN・文字列数値・通貨記号・カンマ・非整数float・非有限数・JS安全整数範囲外は拒否。金額上限/符号等の会計規則は既存validatorへ残す |
| 数量・名称 | 13節のreceipt_quantity/receipt_nameをそのまま再利用。数量の文字列や空欄は使用中の商品では拒否。MVP精度/範囲、名称のNEL/BOM/制御文字拒否、日本語/emoji保持も維持 |
| 大/小分類 | 期待policyにある完全一致pairだけ。片方欠落、他のpair、余計な空白を自動補正しない |
| 種別・採否 | 商品→product、外税→tax、値引き→discount。採用/除外だけを許す。未知値や英語の代替値を推測採用しない |
| 帰属 | 空欄(None/空文字)または既存previewの「指定なし」は帰属なし。商品1/2/3だけを物理slotからitem identityへ結合。数字1等は拒否。空slot・除外商品・税/値引き自身への帰属や、商品に帰属先を付ける指定は拒否 |
| 原本照合 | 使用中はnative boolだけ。文字列TRUE/FALSE、数値1/0、欠落は拒否。変更/追加/除外に照合が必要かは既存operations_from_cellsが判定 |
| 空の追加slot | 名称/数量/金額がNone/空文字、帰属なし、カテゴリは両方空または正しいpair、種別は空/商品、採否は空/採用、照合は空/Falseだけを許す。未知値は拒否。未使用と判定済みのslotだけを既存helper用の不活性セルへ変換し、操作/合計に含めない。入力rowは変更しない |
| 機械列・期待情報 | 固定textで完全一致。versionの文字列を数値へ変えない。item重複/欠落、expectedと機械列のcase/source不一致、無効contextは拒否 |

合計は採用された非空slotの**行金額だけ**を加算し、数量を再乗算しない。Dは独立expected.receipt_totalと照合。再計算合計とD−合計をそれぞれE/Fに照合し、違えば拒否する。合計/差額0だけでは原本一致・検証済み・登録受付を認定しない。税配賦・内税・架空調整などの判断は既存validatorが別に行う。

### 14.3 試験結果と残る限界

| 試験群 | 結果・確認内容 |
|---|---|
| 新規decoder試験 | **82成功・失敗0**。2商品＋空枠、730→868、1,368/差額0、追加/除外/外税/値引きと帰属ID、数量2/0.125/境界、native整数float、Unicode、不正数値/型/カテゴリ/controls、全機械列の変更/欠落、case/source/item重複・欠落、行sort/部分identity copy、メモ/入力不変を含む |
| Python/JS連携（新規82件に含む） | decoder出力→既存Node fixtureでJS snapshot/digest→既存IndependentReceiptReaderの再構築snapshotとの完全一致→既存mock CAS/worker。正常修正は「検証済み・未記帳」、未修正は「修正が必要」。金融writeは0 |
| 既存Python関連回帰 | **219成功・失敗0**。第1工程までの5モジュールを変更せず実行。新規82件と分けて集計（合計301件） |
| 既存Node回帰 | **16成功・失敗0**。既存ソース・fixtureの変更なし、純粋ローカル実行のみ |

環境はPython 3.14.7／pytest 9.1.1／Node v22.23.2。既存network禁止と金融SheetsDB構築trapを維持。既存固定のオフラインUUIDだけを使い、実UUIDを発行しない。新規moduleにIO/サービス呼出しはない。依存google-genaiの非推奨警告1件は既存回帰で継続。構文・whitespace検査も実施する。

**検出限界**：全行sortはcase mapで同一対応を維持し、A/AK不一致・別案件の機械列copy・重複は拒否する。一方、identityを残したまま名称/金額などの値だけをcopy/sortすると、正当な商品編集と観測上区別できない。照合checkboxも入力者申告なので、これを暗号学的bindingにしない。そのような入力がdecoderでは操作候補になることを専用試験で明示し、原本意味検証/実機sortの解決を主張しない。期待catalog自体をrowから捏造すれば独立性が失われるため、将来のcatalog/原本/候補/policyの独立照合は必須。

ATのhash算式、実列の値型/数式/表示captionの独立取得、private catalog adapter、受付連携、checkbox公開中の競合、原本印字の意味照合、iPhone実機sort/行移動は未達のまま。**第2工程はオフラインdecoderの受入まで。Gate B総合NO-GO、Gate C実環境NO-GO、本番NO-GOで停止する。** Google実API・Apps Script実環境・private JSON・trigger・Secrets/Variables/main/validated SHA・金融writer・merge・追加インフラへの変更、Actions dispatch、金融writeは0。保存先は既存Draft PR #200だけとし、文書を含むcommitでCI自動起動を抑止する。

## 15. 実BOOK読取結果と商品枠・private catalogの設計補足（2026-11-08、文書のみ）

基準は `9a657788152adb9f351bf93ed85dd8c70fc2b40f`。開始時のPR #200 headと一致、Draft・未merge。**本節は利用者が提示した読取専用照合結果を確認済み事実として記録する。今回の作業ではGoogleへ再接続せず、実セル・原本照合チェック・private JSON・decoder・Apps Scriptを変更していない。** 14節の「実列の値型/表示caption未確認」は、以下の提示範囲について更新する。全列の見出し・全値・全数式・binding生成根拠まで独立確認できたという意味ではない。

### 15.1 実BOOKとdecoderの契約差分

| 確認済みの実BOOK事実 | 現decoderとの関係／残条件 |
|---|---|
| 合成BOOKの「確認」は46列 | 46要素listの契約と一致。API未接続。将来のreaderは省略された末尾空欄を扱う契約と列構造を明示し、46列を超えるデータを切り捨てない |
| E2/F2は数式、有効値1,368/0 | decoderに渡すのはnativeな有効数値。式文字列やformatted表示「1,368」は渡さない。decoderは行金額からE/Fを独立再計算するので、数式の存在/表示だけを信頼しない。式そのものの許可一覧・全参照範囲は今回未確認 |
| 金額・数量はnative NUMBER | int/整数値floatの円変換と数量規則に整合。数量の精度/範囲制限は継続。全ての可能な値が許容されるという保証ではない |
| 原本照合はnative BOOLEAN | 使用中slotのstrict bool契約と一致。文字列TRUE/FALSEへの変換は不要・禁止 |
| I2はUUID付きcustom checkbox | decoderは表示値をauthorityに使わない。イベントvalue/oldValueの検査とprivate最新ticket照合が別に必要。既存Gate B UUIDをGate Cの準備/受付へ移植しない |
| AT2は64文字の固定文字列、セル内に数式なし | 固定text照合という扱いと整合。ただしdecoderはlowercase hex64も要求する。今回の事実だけでは文字集合まで再確認済みとせず、過去Gate Bのhex検査記録と区別する。生成算式・入力対象は15.2の未確認事項 |
| B2は「一般（完全合成）」 | trusted expected.kind_labelはこの完全一致値にする。14節のテスト値「一般」は合成fixture用であり、実BOOK用期待値ではない。decoderのコード変更は不要、将来catalog設定の差分 |
| 商品Bは868円、AA2はfalse | 数値は変換可能だが、trusted元候補730円との差分には原本照合申告が必要。現状態を検証済みとしない（15.3） |
| 合成private JSONはunconfigured、catalog/journalなし | decoderのexpectedや受付記録を実供給できない。既存ファイルがあるだけではValidationJournalに接続不能。欠落を空catalog/empty_journalで補わず停止 |

全46列のheader・AK:AQの全identity・dropdown候補・原本リンク・退避行の扱いまで今回追加確認したとはしない。登録不可の退避行を実caseとしてcatalogへ作らず、将来readerで明示的に区別する。行番号を案件identityにしない。取得された数値や機械列を使って自分自身の期待値を生成しない。

### 15.2 AT bindingの出所と検証の限界

[Gate B実施記録](receipt-gate-b-event-acceptance-results.md)の再試験では、非公開manifest/配置用configへ新runの固定bindingを用意し、disabled・trigger 0件の間にIおよびAR:ATの4セルを更新、その後configとの一致を照合している。したがって**現在確認できる出所はGate B試験の事前設定値**であり、ATセルが現在の商品編集から式で生成するdigestではない。商品Bの変更をATが自動拘束しているとは扱わない。

公開handlerはmachine[9]のhex64形式と機械vector全体のconfig一致を検査するだけ。公開fixtureの反復文字はテスト用代替値で、生成算式の証拠ではない。今回の確認で確定したのも「固定文字列、セル内数式なし」までである。

未確認なのは、生成に使った関数/版、canonical化、hash入力項目、UUID/run/case/source/itemとの拘束範囲、作成時証跡との照合である。将来は既存の非公開manifest・配置準備記録・生成手順をread-onlyで追跡し、再計算可能なら元記録と照合する。証拠がなければopaque値のままとし、算式を推測したり再生成して置換しない。実値・UUID・原本hash・認証情報を公開資料へ転記しない。

AT、通常セルの `digest([case_id, source])`、catalog由来context_digest、request snapshot.digestは別の契約である。ATをいずれかの代用品やprivate受付authorityにしない。既存decoderの全machine期待値比較は整合検査に限り、Sheetからの期待値採取では真正性を証明できない。

### 15.3 AA2=false時の安全停止

trusted保存候補が730円、現在の商品Bが868円と独立確認できることが前提。元候補を現在値868円から捏造すると差分が消えるため、その供給は禁止する。

| 段階 | 必要な停止動作と既存根拠 |
|---|---|
| 準備前の実接続guard | 現privateはunconfiguredなので読取/設定検証で停止。無断初期化・UUID発行・prepare・checkbox公開・dispatchへ進まない |
| decoder→snapshot準備 | `operations_from_cells()`は730→868のeditかつconfirmed=falseを `receipt_cells_original_check_required`で拒否。JS `receiptSnapshot_()`も同規則で拒否し、`receiptPrepareDraft()`ではsnapshot処理がuuid/prepareより先。E/F一致で例外を免除しない |
| 既存ticketの受付前再読取 | 最新入力からsnapshotを再構築できない、または固定snapshotと不一致ならaccept前に拒否。古いticket・既存チェック状態で回避しない。実portは未実装 |
| 受付後のworker再読取 | 原本照合欠落/変更等でreaderが失敗すれば成功を表示しない。既存workerの保留保存を使用し、保存結果不明ならUNKNOWNで停止。再検証/再確定の自動再送なし |

「falseは常に全商品を拒否」という規則ではない。既存helperは**元候補から変更のない既存商品**には照合申告を要求しない。今回の商品Bは変更済みなので拒否対象。trueにすること自体も入力者申告にすぎず、原本真正性・本人認証・total_confirmation・記帳authorityの証明にはならない。今回はAA2を操作せず、実環境でこの停止経路を試験したとも扱わない。

### 15.4 private catalogに必要な情報と既存取得元

以下は既存reader/reviewの契約へ対応する設計。既存の取得形式があることと、**今回の合成caseの信頼できる実データが確保済みであることを分ける**。合成private JSONにはどれも格納されていない。

| 必要情報 | 再利用する既存取得元・検査 | 現在の不足／禁止する代替 |
|---|---|---|
| case ID、source ID/hash/version/MIME、normal/purchaseの目的 | Gate Aの非公開作成manifest・生成原本の独立hash記録と、既存SAによるmetadata/media/ACL/parent確認。IndependentReceiptReader.checked_metadataとraw bytes SHA-256検査 | 現在版の対応をadapterで再検証する必要あり。解析hashやSheet機械列だけを原本hash/分類の根拠にしない。bytes hash一致は印字内容の意味照合の完成ではない |
| 正規の取込txとimport_digest | 既存「取込データ」の12列、reconciliation.parse_import_rows、receipt pipelineの取込commit形式。case/source/支払総額/要確認理由とdigestを拘束 | 合成BOOKに対応する正規取込記録の存在/保管場所は未確認。現在のD/E/Fや修正値から逆算して生成しない。本番取込を合成caseへ流用しない |
| 元候補全行、candidate_digest、original_itemsと安定item ID | 既存「解析候補明細」の11列、receipt_review_snapshot.candidate_rows/verified_candidate_range、receipt_item_correction_planの元候補再構築、review.preview.original_items | 合成730円候補の承認済み保存物は未確認。元画像868円と意図的誤読730円を区別した既存合成作成記録が必要。AO:AQのIDだけで正規候補と認定しない |
| カテゴリpair、承認済み商品ルール、policy_digest | 既存「カテゴリ」masterと「カテゴリ自動分類ルール」、category_rules.parse_rules、readerのdigest([categories, rules])検査 | 合成受入用の固定policy/版/参照元の確定が必要。dropdownや編集後の商品カテゴリからpolicyを作らない。既存SheetsDBは形式の参照先であり金融clientを起動しない |
| 支払総額とtotal_confirmationの出所 | reviewが要求するsource/receipt_total/actor_idの厳密な既存確認契約。原本総額照合の独立記録と、信頼できる本人確認済み保存元 | この合成caseで条件を満たす保存元は未確認。AAの申告・IのUUID・ACLやtrigger実行者からactor_idを生成しない。欠落は既存reviewの未確認/修正が必要を維持し、検証済みへ昇格させない |
| decoder期待値とcontext | source/元候補/policyから得るoriginal_items/categories、trusted支払総額、kind_label、固定machine vector、catalog anchorのdigest | kind_labelは今回の確認値を使える。残るmachineの出所/slot対応を既存manifestと照合。decoder expectedと既存reader anchorは同じobjectではなく、context_digestはdigest(anchor)。ETagやjournal世代やATを含めない |
| 保存境界とjournal | 既存PRIVATEの固定ID/parent/ACL確認記録と、ValidationJournal v2（generation/requests/active_prepared） | 外側envelopeとcatalog不変性の実adapterは未実装。unconfigured→configuredは別の明示承認。過去状態を勝手にmigration/初期化しない |

`IndependentReceiptReader`は現状、imports/candidates/categories/rulesをsheet_valuesから受け、private anchorの3 digestと照合する。新たな非公開catalogへのデータ同居を採る場合も、元データの独立出所と同じ検証が必要で、単なるanchor追加で供給済みにはならない。編集「確認」rowからの再構成を独立取得と呼ばない。既存テストWorldの固定actorや合成データは試験用であり、実catalogの認証証跡ではない。

### 15.5 3枠超過時の暫定保留と最小拡張案

**現行46列は合計3明細slot**。商品1/2/3という表示でも、外税・値引きも各1slotを消費する。初期fixtureは既存2商品＋空追加枠1つであり、「既存3商品に加えて追加枠1つ」ではない。3つの元明細があれば追加枠は残らない。

暫定方針は、全元明細と追加・除外対象を一意に表せないcaseを**案件全体として保留・受付対象外**にする。先頭3件への切捨て、複数商品合算、別case/別行への分割、メモへの退避で処理を通さない。合計が合っても全明細不足なら停止。既存decoderの元ID包含/最大3件検査は補助であり、readerが事前に元候補を3件へ切った場合は検出不能。独立catalog/元候補全件から容量を確認するのが将来adapterの必須条件。今回は実BOOKの保留表示も書き換えない。

将来の最小案は、**同じ「確認」1タブ・1行1案件のまま9列groupを右方向へ反復追加**すること。PCメニュー・別詳細タブ・Webアプリ・新DBは不要。A:I、原本往復、入力/メモ、固定案件列/見出しは保持する。案件ごとに列位置を変えず、レイアウト版ごとの共通slot上限Nを明示し、対象の全元明細と必要追加枠を収容する。Nの具体値は原本件数分布とiPhone横scroll評価後に決める未確定事項で、今回3→任意上限の変更はしない。

変更が必要になる最小範囲は、列契約/見出し・表示合計式/入力規則、46列decoderの版別group/target解決、機械領域の商品ID対応とprivate期待binding、関連合成試験。AK:ATの固定座標は追加groupと衝突するため、機械領域を明示移設・拡張してlayout versionとslot ID対応を拘束する設計が必要。古い46列を新列数で誤解釈せず、旧版は明示読取または安全停止とし、入力を自動migrationしない。既存operations_from_cells/JS receiptSnapshot_の最大100明細group、planner/journalの最大100 operationsも上限設計の制約であって、「100商品対応済み」の意味ではない。

slotの表示番号と恒久item IDを区別し、列追加/行移動後も新規ID再利用・別商品の入力継承を防ぐ。新旧入力の不変性、全件合計/税値引き帰属、Python/JS snapshot/digest、超過拒否、行全体sort・部分sort、iPhone検索/原本往復/横scroll/入力保持の受入が必要。値だけのcopy/sortの検出限界は14節どおり残る。**これは設計案まで。UI変更・列拡張・移行の実装は事前の別承認を要する。**

### 15.6 次のprivate CAS adapter実装に必要な条件

1. **先にデータ契約を固定**：unconfiguredファイルとjournal v2を区別する外側envelope、catalog/anchor/decoder expectedの対応、全元候補の供給元、policy/total_confirmationの不足時停止、opaque ATの扱いをレビューする。未確保データは欠落を模したmockで停止を試験し、勝手な初期値を設計しない。
2. **次に薄い非金融adapterをオフラインで検証**：DriveRegistryIOの既存If-Match方式とValidationJournal.prepare/accept_prepared/transitionを再利用する差分だけ。bytes↔dictのstrict parse、catalog完全不変、同一外側ETag、ETag取得→独立snapshot読取→expected_tag prepare、CAS後の全体read-back、全層retry/redirect=0とUNKNOWN停止をmock transportで検査する。実初期化を含めず、同居catalog/journalの単一CASに限定する。
3. **追加すべき受入条件**：native数値/BOOLEANと数式有効値の引渡し、実kind_label、730→868かつfalseのprepare前拒否、trusted元候補欠落/改変・全明細3枠超過・全machine不一致の拒否、旧ETag/同時CAS最大1勝者・応答喪失時送信1回、catalog不変、金融write 0。今回これらの新テストや実接続は実行していない。
4. **実環境は別ゲート**：不足する信頼データとAT provenanceを確認し、unconfigured初期化/migrationの具体内容・対象・read-back・停止方法を提示して別承認を得る。ATの算式を解明できなくても独立catalogとopaque固定照合で安全性が成立するかをレビューし、未証明なら停止。Gate Bの3イベント原因未特定、checkbox公開/入力競合、private実CAS、実port/Actions入口・資格、iPhone sort安全性は未解決のまま。

今回の成果は本節の**文書追記だけ**であり、adapter開始や実環境再開を承認済みにしない。コード/テスト/Googleセル/原本照合/private JSON/Apps Script/trigger/Secrets/Variables/main/validated SHA/金融writer/インフラ/mergeへの変更、Actions実行・dispatchは0。**Gate B総合NO-GO、Gate C実環境NO-GO、本番NO-GO、金融write 0件を維持する。** PR #200だけへ文書commitを保存し、CI自動起動を抑止してリモート読戻しを確認する。

## 16. 第3工程：単一private JSON adapter（2026-11-08、オフライン実装）

基準 `1a9da6a9c3821a9b5fdf906c4808d57f40b13aa5` と開始時PR #200 headの一致、Draft・未mergeを確認。15節、ValidationJournal、DriveRegistryIO、IndependentReceiptReaderとiPhone継続仕様を確認した。既存core/decoder/validator/PDF adapter/Apps Scriptと既存試験のblobは基準treeと一致する。

今回の変更は **`app/receipt_private_adapter.py`、`tests/test_receipt_private_adapter.py`、本節の3ファイルだけ**。非金融adapterは注入されたmock bytes IOを使い、Google client/資格/HTTP transportを作らない。実JSONの初期化・migration・Google接続は行わない。

### 16.1 外側schemaとcatalog契約

外側は厳密に `{schema: "receipt-private-envelope-v1", version: 1, catalog, journal}`。journalは既存 `receipt-sheet-validation-journal-v2` のまま。schema/versionの追加は新しい外側envelopeだけで、既存journal schemaは変更しない。unconfiguredや旧schemaからこの形式を自動生成・migrationする関数はない。旧journalはcoreのread-only互換性を維持したまま、このadapterでは接続を拒否する。

catalogは `{cases: {case_id: entry}}`。entryは以下の6項目を過不足なく保持する。これは**合成fixtureで検証した保存契約**であり、実データを確保/承認/保存したという意味ではない。

| 項目 | 内容と既存ロジックの再利用 |
|---|---|
| anchor | source、candidate_digest、import_digest、policy_digest、total_confirmation。既存IndependentReceiptReaderが要求するanchorと同型 |
| import_row | 正規取込の12列全体。既存parse_import_rowsとreviewでcase/source/総額/要確認理由を照合 |
| candidate_rows | 元候補11列を全件、順序を保持して保存。review→既存verified_candidate_range等で候補ID/解析hash/行順/総額と対応。4件以上は切り捨てず停止 |
| categories / rules | 完全なカテゴリpairと承認済みルール行。parse_rules/valid_ruleを再利用し、不正でparseから落ちたルールを黙認しない。policy_digestと照合 |
| decoder | 固定machine vectorとkind_label。original_itemsはreviewから、receipt_totalはtxから導出し、context_digestはdigest(anchor)。既存46列decoderで全元明細の期待値・数量/名称/カテゴリ/slot IDを検査 |

catalog digestは**ファイルやSheetから学習せず、独立した信頼設定としてconstructorへ必須指定**する。毎回のGETと保存前後で同じdigestを照合する。JSONの数値/配列型を保持し、catalog自体を再正規化・書換えしない。欠落/変更/重複source/不整合は停止する。total_confirmationもこのadapterの接続前提として必須にし、未確認情報を埋めない（既存reviewの未確認時挙動は変更なし）。原本bytesは格納せず、source hash/version/MIME/purposeを保存し、実bytes/ACLとの照合は既存IndependentReceiptReader側に残す。

`read_catalog()`はcatalogと**journalと同じ外側ETag**を返す。`private_cases()`は既存reader用のanchor mapと同ETag、`decoder_expected(case_id)`は既存decoderの期待値を返す。返値はdeepcopy。context digestにはjournal/ETag/ATを混ぜない。AT/試験UUID/runは固定machine期待値としてのみ扱い、登録受付authorityに転用しない。

### 16.2 CAS保存・状態遷移・UNKNOWN

| 段階 | 実装した処理／停止条件 |
|---|---|
| IO binding・metadata | io.configのfile_id/folder_idと固定設定を照合。fileとparentそれぞれのID/MIME/単一owner/owner＋既存SA writerだけのACL/非trash/version/強ETag、fileの単一parentを検査。metadata→media→metadataで変更があれば停止 |
| strict bytes読取 | UTF-8 bytes、上限1 MiB、重複JSON key（入れ子含む）、NaN/Infinity/指数overflow、BOM、不正Unicode surrogate、schema/version/型の不整合を拒否。例外は固定StateErrorへ置換しraw API detailをchain表示しない |
| 受付port | `read_versioned() -> (journal dict, outer strong ETag)`。既存ValidationJournal.prepare(expected_tag)/accept_prepared/transition/load/saveをそのまま使用 |
| 許可遷移 | `_transition()`はbefore journalをメモリ内で既存ValidationJournalへ渡し、提案された1操作を再適用して結果全体を比較。prepare追加、最新ticket受付、既存transitionだけを許し、世代飛ばし・履歴削除・snapshot変更・active indexだけの操作・旧状態への巻戻し・UNKNOWN再開を拒否。旧acceptによる準備省略は接続しない |
| 条件付き保存 | 読取時の外側bytes/ETagを保持。保存直前の独立GETで同じbytes/ETagを確認し、catalog不変の外側JSONを `io.replace_versioned(before_bytes, tag, after_bytes)`へ**1回だけ**渡す。新しい状態へのmerge、ETag取り直し再送、無条件PUTは行わない |
| 独立read-back | metadata付き独立GETで外側bytes全体が提案と完全一致し、ETagが強く更新されたことを確認。既存core.saveが追加で行うloadにも同じ確認値を拘束し、その間の変更もUNKNOWN停止 |
| UNKNOWN | 書込みを止めてから保存を試みる。保存前後の通信/読戻し不明、CAS競合等で失敗したinstanceは恒久GET-only。照合GETが成功しても書込みを再開しない。自動retry/repair/新UUID迂回はない。既存coreが返すUNKNOWNと永続journal状態は同一視しない |

ioのbytes signatureとmetadata projectionは既存DriveRegistryIOのDrive v2 If-Match方式を再利用する。**実DriveRegistryIOを構築/実行したわけではなく、PDF用configや既定認証retryを安全と認定したわけでもない。** 注入IOは固定対象への原子的If-Match・送信1回・retry/redirect無効を満たす契約。今回証明したのはmockでの呼出し/競合だけであり、実HTTP 412・認証refresh時の再送抑止は未実測。既存PDF/金融transportは変更しない。

### 16.3 専用合成試験と回帰

| 群 | 結果 |
|---|---|
| 新規adapter専用 | **67成功・失敗0**。正常prepare→accept→claim→FINALと既存reader/cache連携、同一外側ETag、catalog不変/返値不変、stale拒否、prepare/accept/claimの強制同時CAS最大1勝者、JSON/schema/旧版/unconfigured/必須情報/元候補/カテゴリpolicy/機械列/parent ACL/transport対象の拒否 |
| 障害・未確認境界（上記67件に含む） | 保存前/後応答喪失、読戻し失敗/不一致/ETag未更新、core追加読戻し競合、metadata/media競合、後続GET成功後もPUT増分0。730→868かつconfirmed=falseは既存decoderでprepare前に拒否。元候補4件は容量超過拒否。金融writer構築trap・network禁止を維持 |
| 既存Python関連5モジュール | **219成功・失敗0**。既存core/PDF adapter等を変更せず回帰 |
| 既存decoder | **82成功・失敗0**。変更なし |
| 既存Node receipt fixture | **16成功・失敗0**。read-only fs/vm/crypto等の純粋ローカル試験を再確認して実行。Apps Script実行ではない |
| 構文・書式 | 新規Pythonのcompileall、git diff --checkを確認 |

Pythonは合計368件（新規67＋既存219＋decoder82）。Python 3.14.7／pytest 9.1.1／Node v22.23.2。plugin自動読込を止め、Node実行processのNODE_OPTIONSを除外した。既存google-genaiの非推奨警告1件は継続。fixtureの既存固定UUID/合成ID/hashだけを使用し、実UUID発行・本番データ・実原本の使用はない。

### 16.4 未解決事項と次工程の条件

- **UNKNOWN停止の寿命**：書込み禁止latchはadapter instance内。GETで解除するAPIはないが、instance再生成/別processによる再送を永続的に禁止する実行hostの契約は未接続。クラッシュ後に新instanceでprepareし直す自動回復は禁止。将来hostが非公開障害記録とjournal/実行証跡をGET照合し、別の明示再開判定までwrite経路を閉じる必要がある。UNKNOWNを記録するための追加PUTは今回作らない。
- **信頼境界**：catalog digestの独立設定元、実catalog/元候補/取込/policy/total_confirmationは未確保。private ACLと正しい固定IOが前提で、資格を持つ外部操作がjournalをrollbackしてからprocessを作り直すケースを単一JSONだけで検知できるとは主張しない。adapter自身が要求する巻戻しは専用試験で拒否する。
- **実接続差分**：Drive v2 metadataとreaderの既存metadata形式の整合、実IOの固定ID/ACL/parent/強ETag・bytes対応・HTTP If-Match・全層retry=0の証明、実資源の初期化/migration内容の査読・別承認が必要。今回はlive client/CLI/workflow/Apps Script portを追加していない。
- **UI/入力**：実BOOKのAA未確認、AT生成provenance、値だけのcopy/部分sort、checkbox公開と入力変更の競合、原本印字の意味照合、Gate Bの3イベント原因未特定は未解決。3明細上限と1タブ方式を変更しない。

推奨順序は、(1)本オフライン差分レビュー、(2)信頼catalog供給元とUNKNOWN時host停止契約の確定、(3)mockで実IO契約差分の検証、(4)初期化/Google試験対象・副作用・停止手順を提示して別承認、の順。今回の成功を(4)のGOにしない。

**第3工程はオフラインadapterの受入まで。Gate B総合NO-GO、Gate C実環境NO-GO、本番NO-GO、金融write 0件を維持して停止する。** 実Google通信/書込み、private JSON初期化/migration、実UUID発行、Apps Script/trigger、Actions dispatch、Secrets/Variables/main/validated SHA、金融writer、mergeへの変更は0。保存先は既存Draft PR #200だけ。commitでCI自動起動を抑止し、リモート読戻しを確認する。

## 17. 実接続前の必須条件と最小接続計画（2026-11-09、調査・オフライン診断）

基準はPR #200 head **`bab822e199c533830a1ca1b6c1cfa42b6e81dd1d`**。開始時のGitHub headとの完全一致、Draft・未merge、base/main `fa7ac9378ef51e4a51258dd6053afe52ed0a2bb8`を確認。AGENTSとiPhone継続仕様を全文確認した。今回は本節と `tests/test_receipt_gate_c_preconnect_audit.py` のみ追加・更新し、製品実装・既存PDF adapter・金融処理を変更しない。

結論：既存private journalとActionsを再利用できるが、**現状のまま実接続はNO-GO**。永続UNKNOWN自体の拒否と、保存結果不明という呼出し結果のUNKNOWNを区別する。後者を含めたprocess間の停止保証、catalogの真正な供給、送信一回の実transportは未成立。以下の必須条件は設計であり、実装・設定・初期化・実行の承認ではない。

### 17.1 UNKNOWN・再生成・同一UUIDの扱い

追加診断は既存合成fixtureとbytes mockだけを使う。保存前例外ではjournalが変化しない状況を作り、同じadapterのGET後はwrite禁止のまま、新adapterでは同UUIDのprepare/accept/claimが再試行できることを確認した。claim未保存の場合、`execute_accepted_request()`の次回呼出しがvalidatorまで進むことも確認した。これは**欠陥境界の再現**であり、回復の受入試験ではない。

保存済みで応答だけ失われた場合はGETでPREPARED/受付済み/検証中を発見でき、同段階の再適用は拒否された。しかし、保存済みPREPAREDを次回acceptしたり、受付済みを次回claimしたりすることまではcoreが禁止しない。停止理由の外部観測なしに「次の段階だから安全」として進めない。永続UNKNOWNから検証中への遷移は新instanceでも拒否された。

| 事象 | 次回起動時の読取・停止条件 | 禁止する動作 |
|---|---|---|
| prepareの応答不明、UUIDがGETで見つからない | 未送信・未保存を断定できない。既知UUIDを障害対象として保持し、checkbox非公開で停止 | 同UUID再prepare、別UUIDへの自動置換、空journal生成 |
| prepare応答不明、PREPAREDが存在 | UUID/source/snapshot/catalog/世代を照合するだけ。公開済みか不明でも停止 | GET成功による公開・accept再開 |
| accept応答不明、PREPAREDまたは受付済み | dispatch 0のままGETと独立run照合 | accept再送、未dispatchに見える要求の自動回収 |
| dispatch応答不明、受付済み/検証中/FINAL | journalとActionsの該当run/attempt/checkoutを別途GET。runが見つからなくても未受信の証明にはならない | 同UUID自動再dispatch、run一覧の検索結果だけによる再送 |
| claim応答不明、受付済みまたは検証中 | 当該呼出しのvalidator 0。次回もGET-only | 新instanceで再claim、検証中を受付済みに戻す |
| FINAL保存応答不明 | GETの完全一致FINALだけ既存cacheへ表示可能。不一致・非終端なら照合待ち | 検証やFINAL PUTのやり直し、UNKNOWNの永続cache化 |
| 永続UNKNOWN・履歴欠落・rollback疑い | 同case/sourceを停止。GETと人による調査のみ | UNKNOWN解除、TTL削除、履歴書換え、別UUID迂回 |

**既存資産だけで無条件には保証できない理由**：CASが守るのは保存された状態と同時更新。PUT未保存でprocessが消えると、「要求は試したがUNKNOWNだった」という事実は同じJSONに残らない。UNKNOWNを書き込む追加PUTも失敗し得るため解決にならない。Actions concurrencyは直列化であり、順番に到着する再dispatch・新run・再起動を消費済みにしない。[Actions concurrency仕様](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/control-workflow-concurrency)

最小追加条件は、新DBではなく**実行hostの起動時GET-onlyと、runごとの一回限りの明示許可**。初期の隔離受入はこれで限定する。

1. あらゆる入口・process起動・adapter再生成はGET-onlyを既定にする。前回成功した一般的なenabledフラグを次回起動のwrite許可にしない。書込み能力を取得する前に固定PRIVATE、catalog pin、全未確定要求、前回Actions runをGET照合する。読めない・実行証跡が欠ける場合は停止する。
2. 許可は非公開の承認記録にrepo/workflow/実装SHA、run_id、run_attempt=1、UUID、case/source/snapshot/catalog digest、許す段階と保存前ETag/bytesのdigest、期限を結合する。承認を同じJSONから自己発行しない。新runや別processへ引き継がない。run_idが確定した後の手動審査で一つのprocessだけを許す。GitHub environmentのrequired reviewer等は候補だが、利用可否・bypass制限・既存設定は未確認。単なる承認済みenvironment名だけではこのbindingを満たさない。
3. 1 run＝1 worker process、restart supervisor・step retry・matrix・子workerによる再開なし。`run_attempt != 1`は資格取得前に拒否。別run_idの同UUIDも許可不一致で拒否。承認後の同run内で異常が出ればそのrunの許可は消費済みとし、processを作り直さない。起動しなかった可能性があっても自動再利用しない。[run_attempt仕様](https://docs.github.com/en/actions/reference/workflows-and-actions/contexts#github-context)
4. Actions run/attemptと非公開承認・障害記録を保存し、retention切れや削除で履歴がなくなったらfail-closed。private内容・原本・actorをpublic logs/artifactsへ出さない。`finally`や終了時artifact uploadの成功を安全性の必須前提にせず、異常終了・cancel・timeout・記録不能はすべて未確定として扱う。
5. Apps Script側の準備/受付PUTとdispatchにも同じ停止契約が必要。ScriptLockやPropertiesのbooleanだけでは成立しない。未接続の自動入口を開かず、初期受入は人が監督する固定一回に限定する。常時自動受付に拡張する前には、同じprivate journalへの**送信前の消費記録**（operation/run bindingと未解決保持）または同等の独立した永続permitが必要。marker保存そのもののUNKNOWNもGET-onlyで停止し、process再生成で再試行しない。既存strict schemaにはないため、その自動化は今回の範囲外。

この最小案の保証は、許可済みhostと単一process契約の遵守を前提にする。資格を持つ任意の別プログラムやownerによるjournal/承認記録のrollbackまで防ぐとは主張しない。**既存Actions設定だけで実装済みとは判定しない**。run履歴検索・concurrency・`run_attempt=1`だけを組み合わせても、新規runへの自動再送は止まらない。

明示再開の必要条件は、(a)全writer/trigger/queued runが停止し生存workerがないこと、(b)当該UUID・前後bytes/強ETag・世代・catalog・run/attemptの独立照合、(c)既存UNKNOWNと未確定要求を保持したまま処置を非公開記録へ固定、(d)ownerによる対象・段階・副作用を指定した新たな明示判断、(e)安全条件と独立read-back経路の再確認。GETでFINALが判明した場合は表示だけで終える。永続UNKNOWNは既存core上再開不能であり、今回その解除機能を作らない。証拠不足なら同案件を隔離し、別UUIDでも進めない。サービス再開の判断と、そのUUIDの再実行許可を混同しない。

### 17.2 信頼catalog供給元の読取監査

対象は既存の完全合成caseのみ。PR本文・設計/承認文書・コメント（0件）、既存ローカルprivate証跡と設定要求を読取。Googleへの追加GETはしていない。確認したprivate資料は `gate-a-original-reconciliation-private.json`、`gate-a-sa-readonly-evidence-private.json`、`gate-a-downloaded-private.json`、`gate-b-manifest-private.json`、`gate-b-setup-requests-private.json`、`gate-b-book-write-evidence-private.json`、Gate B完了/再試験記録。実ID・メール・原本hash・各資料全文は公開文書へ転記しない。以下は保存済み証跡の事実であり、現在のGoogle状態の再証明ではない。

| 必須情報 | 読取で確認できた出所 | 欠落・採用条件 |
|---|---|---|
| 原本hash・version | Gate Aの独立生成原本hash提供記録とSA取得bytesの一致。PNG versionは保存記録上4、MIME/58,010 bytes/parent/exact ACLも記録あり | 原本identityの過去証拠はある。実接続時にmetadata→bytes→metadataを再GETし、承認済み独立hash/versionと一致すること。原本hashを解析hashに置換しない |
| 元候補730円 | 承認文書に「原本868円、意図的誤読候補730円」。Gate B setup要求と成功記録にUI初期値730円、合計1,230円、差138円 | **正規候補11列全件の保存物は未確認**。UIに730があった事実はcandidate/import/analysis hashの証拠ではない。商品ID・行順・名称/数量/種別・候補digest・解析hashを含む承認済み合成fixtureが必要 |
| 正規取込情報 | UI設定とmanifestのcase/source対応はある。既存parserは12列import rowを要求 | **このcaseの正規12列rowと保存元・版/解析hashが欠落**。UIの総額・日付・caseから逆算して生成しない |
| カテゴリpolicy | Gate Bの入力規則に大分類/小分類の候補がある。既存parser/adapterはcategoriesのpair一覧と18列rulesを照合 | **このcase用に承認されたpair一覧・ルール全件・版・policy digestの独立記録なし**。独立dropdownの組合せや現在の商品分類をpolicyと認定しない。rules=[]も根拠なしに補わない |
| total_confirmation | 既存契約は完全なsource＋整数receipt_total＋64hex actor_id。原本hash照合は総額の本人確認とは別 | **当該source/version・総額1,368円の本人確認保存物が欠落**。AA=true、Gate A PASS、登録UUID、UIイベントを代用しない |
| actor_idの正当な出所 | 既存 `auth-session/generic_review.py` 等は検証済みGoogle issuer/subのdigestを使う。`shared_login.py` は検証済みactorとsession/requestを照合する | **この合成caseの確認に結合された検証済みactorレコードなし**。64hexの形だけでは本人性を証明しない。メール、ACL owner、SA、trigger実行者、GitHub actorから生成しない。既存の有効な確認証跡を取得できる場合だけ再利用。なければ合成用本人確認記録の出所・検証手段を別途確定する。旧Web/OIDCホストは再稼働しない |
| catalog digestの独立固定 | constructor必須pinと各GETの照合は実装済み | **承認済み実catalogもpin設定元もない**。元データの独立出所を検査した後、既存 `page_receipt_model.digest(catalog)` のcanonical JSON（型・配列順保持）をオフライン計算。版・source・各出典・実装SHAとともに別の非公開承認記録へ固定し、既存の保護された実行設定へ明示配布する。実PRIVATEや編集BOOKから実行時に学習しない。設定の作成/変更は別承認 |

最小追加データは、**合成専用の正規候補/取込/policyの承認済み保存一式、本人確認とactorの検証証跡、独立catalog pin**。実BOOKを逆算する代わりに「未確保」で停止する。既存Gate B manifestの機械vectorは設定時のprovenanceに使えるが、使用済みGate B UUID/runを新しい受付ticketにしない。ATは固定値の整合比較に限り、catalog digestや本人authorityに代用しない。合成UIの作成記録を正規取込済みと改名しない。catalog生成時も全候補3枠超過は案件全体保留とする。

**ATについて15～16節の未確認事項を更新**：既存 `gate-b-retest-01-private.json` のbinding_derivationに `sha256(UTF8 compact JSON machine[0:9])` の記録を発見。保存済み初回manifestと再試験configのmachine vectorからそれぞれ再計算してAT一致を確認した（実BOOKの値から期待値を生成していない）。これは機械列の生成式の出所であって、正規候補・policy・actorの真正性の証拠ではない。実接続時の完全一致検査と、その元manifestが承認対象であることの確認は引き続き必要。保存済みGate A取得PNGのhashも独立生成原本hash記録と再計算一致した。原本の現在状態や印字意味・本人の総額確認を再証明したとはしない。

### 17.3 DriveRegistryIOとHTTP送信回数

`app/pdf_intake_registry.py` の実装は**変更せず読取**。追加診断ではfake credentialsとrequests `BaseAdapter.send`のmockを使い、AuthorizedSessionより下のPreparedRequest送信数を計測した。実TCP/TLS・Google APIを使わない。Python 3.14.7、google-auth 2.56.3、requests 2.34.2での結果。requirementsは下限指定のため将来の実runtimeで版と挙動を再確認する。

| 項目 | 実装・mock結果 | 接続前に必要な差分/証明 |
|---|---|---|
| Drive v2条件付PUT | `/upload/drive/v2/files/{id}?uploadType=media&fields=id,etag`、bytes body、`If-Match: "old"`、application/jsonを確認。メソッド内のPUT呼出しは1回 | 固定PRIVATEだけに限定。resumable/create/無条件PUTへのfallbackなし。実HTTP成立は今回未実施。[v2 files.update仕様](https://developers.google.com/workspace/drive/api/reference/rest/v2/files/update) |
| 412 | HTTP 412は`StateError('HTTP_412')`。PUT前のbytes/ETag不一致も同じエラー名 | server 412はmock送信1回、local staleは追加送信0を区別。エラー文字列だけで実API CAS合格としない |
| 401 refresh | **既定AuthorizedSessionは401→200で同じPUTを2回、401→401→200/401で3回送信**。401→412も2回。body/If-Matchは同一 | 既存PDF既定clientを直接接続しない。レシート専用transportで事前token準備、`max_refresh_attempts=0`・`refresh_status_codes=()`相当を固定。401なら停止。token endpointのrefreshとDrive PUT再送を別に計測。[google-auth仕様](https://google-auth.readthedocs.io/en/latest/reference/google.auth.transport.requests.html) |
| HTTP retry | ローカル既定HTTPS HTTPAdapterはRetry(total=0, read=False)。IO自体にはloopなし。ただし任意sessionを注入可能で、IOはretry設定を検査しない | 専用sessionの全mountでconnect/read/status/other/redirectを0、urllib3 retry・上位job retry・proxy経路を含め査読。資格refresh無効だけで全層0と認定しない |
| redirect | `_request()`の`allow_redirects=False`。301/302/303/307/308は各1送信で停止 | 専用sessionでも固定host/IDとredirect=0を維持。別hostへcredentials/bodyを追送しない |
| timeout/deadline | 通常1 requestへ20秒。begin_request後は45秒deadlineの残時間と20秒の小さい方。期限切れは送信0。Timeout/ConnectionErrorは停止 | requests timeoutは総wall-clock上限ではなく、認証再帰にもIOの残時間再計算はない。host全体の上限・単一process終了・再起動禁止も必要。timeoutは未保存の証明にならない。[requests timeout仕様](https://requests.readthedocs.io/en/latest/user/advanced/#timeouts) |
| 強ETag/read-back | 弱/欠落/無引用tagはPUT0。read_versionedはACL＋metadata→media→metadata。replace_versionedは事前GETするが**PUT後のGETは実行しない** | 既存ReceiptPrivateAdapterが全体bytes、catalog不変、新強ETag、metadataを独立GETし、core.save追加GETも照合。PUTの200/返却etagだけで確定しない。v2→reader metadataはparentsのobject→ID list、labels.trashed→trashedを明示変換 |

zero-refreshを明示した注入sessionでは、401/412/429/500/503、各redirect、timeout/接続例外がそれぞれPreparedRequest送信1回で停止した。**これは専用実transportの完成証明ではない**。mockはurllib3内部の実socket送信やネットワーク中継を測っていない。実資格取得やGoogle書込みはなく、既存CAS probeの過去成功も今回のレシートtransportの認証再送抑止を証明しない。

### 17.4 最小の追加実装ファイルと資源・資格

以下の名前は将来の最小差分候補。今回は追加しない。既存core/decoder/planner/validator/cacheを優先再利用し、新DB・常駐server・Cloud Run・新しい金融経路は作らない。

| ファイル | 必要な差分 |
|---|---|
| `app/receipt_private_drive_io.py`（新規候補） | 固定PRIVATE/FOLDER専用の薄いbytes IO。既存DriveRegistryIOのv2方式を再利用し、PDF用inbox/processed設定を偽IDで満たさず専用構成にする。強ETag、exact ACL、metadata投影、全層retry/redirect=0、事前token準備。PDF adapter本体は変更不要 |
| `app/receipt_sheet_validation_host.py`（新規候補） | 17.1の起動GET-only/一回許可/同UUID再送拒否とrun照合。既存ReceiptPrivateAdapter＋ValidationJournal＋execute_accepted_requestに接続。実46列read、catalogの正規行をreader portへ渡す処理、v2 metadata正規化、自己完結cache appendを小さなportとして同居。金融SheetsDBを作らない |
| `apps-script/receipt-validation/`の専用port（将来追加候補） | 既存Code.gsの純粋イベント/snapshot処理を維持し、実BOOK/sheet/trigger/最新ticket/全層送信一回/停止契約を満たすportのみ接続。Apps Script輸送で再送0を証明できない段階ではPUT/dispatchを有効にしない。Gate B projectへそのまま上書きしない |
| `.github/workflows/receipt-sheet-validation.yml`（新規候補） | 非金融のUUID-only入口。固定checkout SHA、専用concurrency・cancel=false、run_attempt=1、run-bound許可、単一process、timeout、最小Secrets。category-sheet-requestは金融設定を読むため転用しない。default branchに登録が必要なworkflow_dispatchはmain凍結中に利用可能と仮定しない。[workflow syntax](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax#onworkflow_dispatch) |
| 専用mockテスト | host再起動・別run/attempt・許可欠落/失効・記録欠落/削除・全段階の応答不明・HTTP下層送信数・catalog pin・metadata変換・cacheと入力保持。今回の診断テストはhost guard未実装の境界を残している |

Google対象は既存隔離**BOOK 1冊、SOURCE PNG 1件、PRIVATE JSON 1件と既存親FOLDERの固定allowlist**。実IDは既存非公開manifestにあるものだけ。旧CAS probe・本番BOOK・金融台帳は対象外。PNG/FOLDERはGET-only、PRIVATEは別承認後の単一CAS対象、BOOK変更は別承認後のticket/結果cache領域に限定。既存入力・メモ・原本リンク・可視1タブ・3明細上限は維持。非表示cacheの追加が必要なら同じBOOK内の差分として明示し、第4Drive資源を自動作成しない。

資格は既存SAとownerの既存共有範囲を確認して再利用する方針。独立readerはdrive.readonly/spreadsheets.readonly、PRIVATE PUTはdrive、BOOK投影はspreadsheetsのtoken能力が必要。ただしOAuth scopeはfile単位の制限ではないため固定allowlistをhost/IO双方で強制する。既存SAが持つ他資源の資格能力を安全とみなさず、金融Secretsをjobへ渡さない。SA秘密鍵やowner OAuthを取得/公開/新設しない。

GitHub側のworkerはcontents:read、run照合にはActions:read相当が必要。将来のdispatch用資格は対象repoのActions:write相当と保管/失効先の確認が必要で、既存category tokenの流用を認めない。private pin/permitの保管先・アクセス者、Apps ScriptのURL Fetch/Drive/Sheets scope、専用project/triggerの対象と所有者は未確定。既存Secrets/Variables/environmentを今回作成・変更しない。mainを変えず既存金融workflowへ迂回して実行する案は採らない。

### 17.5 段階計画・独立読戻し・GO条件

| 段階 | 最小作業と独立確認 | 現在の判定 |
|---|---|---|
| 0：今回 | 既存記録読取、故障mock、設計追記 | 完了。Google通信/変更なし |
| 1：不足資料の確保 | 17.2の候補/取込/policy/本人確認/actor出所を確認し、独立pinと承認済み非公開catalogをオフラインで固定。欠落は欠落として停止 | **未完了・接続NO-GO** |
| 2：専用IO/hostのオフライン差分 | 17.1の新run・再生成停止と17.3の全層送信1回を含むmock受入。権限・固定allowlist・一回permit・現在のschemaとの差分をレビュー | 未実装、実環境GOではない |
| 3：PRIVATEだけの限定実CAS | 既存unconfigured bytes/version/ETagを独立GETし、catalog＋空journalへの初期化内容・1回の変更・失敗時扱いを事前レビューして**別承認**。現在は初期化禁止。承認後もstale 412、唯一勝者、応答破棄は固定件数/別々の許可で測る。自動restore/retryはしない | **NO-GO** |
| 4：BOOK/Apps Script/Actions導線 | Gate B未特定3イベント、最新ticket公開競合、値だけ部分sortの限界と対象制限、AA照合、17.2のmanifest/AT対応、実port、default-branch登録/資格の問題を確認・解決してから、別承認された範囲で接続。iPhoneは同案件の入力保持と「検証済み・未記帳」まで | **Gate B総合／Gate C実環境NO-GO** |
| 5：本番・金融write | 今回の対象外。独立した将来の判断が必要 | **本番NO-GO** |

独立read-backは書込み応答・書込みsession内cacheを使わない新規GETで行う。PRIVATEはmetadata/ACL/parent→raw media→metadataを取得し、保存前bytes/ETagと提案全体bytes/新強ETag・generation・全UUID履歴・catalog pinを比較。SOURCEは独立基準hash/versionを使いmetadata→bytes→metadataを照合。BOOKは明示UNFORMATTED_VALUEで46列のcase/source/machineと入力snapshotを再構築し、数式の有効値/型も照合する。結果cacheはFINALのcase/source/UUID/snapshotに完全一致した場合だけ同じ「確認」へ表示し、投影失敗で再検証しない。runはGitHubのauthoritativeなrun/attempt/job情報を別に取得し、時間経過や画面の成功文字列から完了を推定しない。

停止条件は401/412/timeout/redirect/429/5xx、ACL/parent/MIME/version/強ETag変化、catalog/pin不一致、未確定履歴・証跡欠落、permit不一致、原本/入力の変更、読戻し不明。新規write/dispatchを閉じてGET照合へ移る。明確な412でもETagを取り直して自動再送しない。rollbackはjournalを過去bytesへ戻す操作ではなく停止・隔離を基本とし、履歴を消す復旧は提案しない。trigger停止/資格失効など実環境の停止操作もその時点の対象を指定した承認に従う。今回それらの操作は行わない。

**実接続GOは、17.1～17.4の必須条件が実装・検証済みで、段階3/4の対象・変更bytes/件数・資格・停止方法・独立読戻しが明示承認された場合に限る。現時点の判定はNO-GO。** 設計文書更新やmock成功ではこの条件を満たさない。

### 17.6 オフライン検証と変更境界

追加診断 **31件成功**。401認証再送、HTTP status/redirect/timeout、弱ETag、local/server 412の違い、process再生成時の停止消失、保存済み段階のGET照合、永続UNKNOWN拒否を含む。既存adapter/decoder/core/PDF registryの関連回帰325件と合わせて **356件成功・失敗0**。socket.connect/create_connectionと金融SheetsDB構築をtrapし、fake credentialsだけを使用した。

実行コマンド（Windowsローカル、plugin自動読込OFF）：

```text
python -m pytest -q tests/test_receipt_gate_c_preconnect_audit.py tests/test_receipt_private_adapter.py tests/test_receipt_sheet_decoder.py tests/test_receipt_sheet_validation.py tests/test_pdf_intake_registry.py --tb=short
```

Google API読取/書込み、private JSON初期化/migration、Apps Script実行、trigger作成、Actions dispatch/再実行、本番接続、金融write、Secrets/Variables/IAM/ACL、main/validated SHA、PR mergeはすべて0。public公式仕様の読取とGitHub PR/commitの読取を行った。PR #200には調査用テストと本設計だけを `[skip ci]` commitで記録し、CI起動や実接続の承認にしない。**Gate B総合NO-GO、Gate C実環境NO-GO、本番NO-GOを維持する。**

## 18. catalog不足の分類・最小fixture案・再利用防止の責任（2026-11-09、文書のみ）

基準commitは **`835a4bfa039efed7153247db1f17f4f4d6d417ff`**。開始時PR #200 headと一致、Draft・未merge、main/base `fa7ac9378ef51e4a51258dd6053afe52ed0a2bb8`を確認した。実装・接続は次の承認まで行わない。本節は17.2の「未確保」を取得可能性と解消方法に分けたもので、取得していない証跡を取得済みに変更しない。

### 18.1 調査の範囲と打切り

- Gate A/Bの既存private作業領域のテキスト資料38件を対象に、候補/取込/catalog/policy/total_confirmation/actorの保存物と作成経緯を読取。`gate-a-downloaded-private.json`は217 bytesのunconfigured記録でcatalogなし。既存PNGのhashを再計算し、独立生成原本hashの保存証跡と一致した。Google上のPRIVATEを今回取得・変更したわけではない。
- 以前の合成UIのprivate資料46件も限定確認。`phase4-source-attestation.json`の原本集合は今回の対象SOURCEと一致せず、その本人/案件対応を転用しない。pytestの一時fixture・node_modules・資格保存領域は正規証跡に数えない。
- 作成元履歴「Cloud Run不要のレシート検証基盤構築」「家計簿画像の確認方法」の取得できた範囲、およびPR #200コメント0件を確認。作成元会話の追加ページ取得はtool errorとなったため、再探索を繰り返さず未取得とした。会話の評価文や「PASS」はcandidate rowや検証済みactorの原記録の代用にしない。
- Google Driveスキルで、`receipt-validation`、receipt/PR200に限定した`catalog`、同範囲の`合成`の3検索を読取専用で実施。前2件は0件、最後は既知の合成BOOKのみ。候補・取込・確認証跡の保存元は得られなかった。検索は網羅一覧ではなく、Drive全体の不存在を証明しない。実BOOKの値は期待値構築に使用しない。
- 任意の追加情報として、利用者に既存非公開保存先だけを照会した。Bの追加調査は**保存先が指定された1件を読み、case/source/version/承認の結合を確認する一回**に限定する。場所が分からないまま金融台帳・認証資格・他案件の履歴を広く探さない。回答なしを承認と解釈しない。

分類は **A＝対象caseへの正規証跡を取得可能、B＝具体的な既存供給候補はあるが対象caseの承認/結合が未確認、C＝この限定調査で正規証跡を取得不能**。Cは全世界での不存在を断定する区分ではない。コードの形式例と、対象caseへの承認済み実データを区別する。

### 18.2 不足情報と解消方法の一覧

| 対象caseに必要な項目 | 分類 | 調査結果・出所/版 | 最小の解消方法・検証 |
|---|---|---|---|
| 正規候補11列全件 | **C** | Gate B setup/成功記録は商品B=730円を設定したUI証跡のみ。候補ID・取込ID・解析hash・全行順を含む保存物は未取得。既存 `candidate_rows()` とtest scenarioは基準commitにあるが、対象caseの証跡ではない | 新規合成fixtureとして意図的誤読を明示し、既存generatorで11列を生成。全行・版・解析payloadのhashと出典を固定し、`verified_candidate_range()`で照合。UIから逆生成しない |
| 正規取込12列 | **C** | 対象caseの12列原記録なし。manifestはcase/source対応だけ。Sheets設定要求は取込commit記録ではない | 同じ新fixtureに12列を明示定義し、`parse_import_rows()`で既存形式へ渡す。「新規合成定義」と記録し、過去の取込日時/解析結果を装わない |
| カテゴリpair・承認ルール | **B** | `config/categories.tsv`の既存版は取得可能（最終変更commit `f0cedbe812385c70b33c1f8ed5d567b98e3b3f35`）。ただし「食料品/消耗品」と、Gate B UI/testの「食品/洗剤」等は一致しない。対象case用の承認済みpair/18列rulesは未取得 | 既存の合成policy承認保存先が分かる場合だけ1件照合。取得できなければ、既存testの最小pair集合と「この合成fixtureのルール0件」を**新規に承認**する案を採る。分類名の自動読み替えや、承認なしのrules=[]補完はしない |
| total_confirmation | **C** | source/hash/versionと総額に結合した本人確認保存物なし。Gate Aはbytesの真正性確認、Gate Bは編集イベント観測であり、総額本人確認とは異なる | 新fixtureでも本人が原本・総額・対象版を確認した正規記録が必要。AA、UIの総額、会話の一般的な「進めよう」、Gate A PASSから生成しない |
| 検証済みactor | **C** | この確認行為に結合した検証済みissuer/sub・actorの原記録なし。test Worldの `actor_id="d"*64` は架空fixture。既存auth-sessionコードは検証パターンであって対象caseの証跡ではない | 有効な既存検証済みactor/確認記録が指定された場合だけ結合を検査。なければ本人確認方法の具体案を別途承認するまで接続停止。ownerメールのhash、SA、GitHub承認者、`e.user`で既存actorを捏造しない。旧OAuth/Cloudホストを再開しない |
| 独立catalog pin | **C** | 対象caseの承認済みcatalog自体が未成立。実pinと独立配布記録なし。digest関数/constructor照合だけは既存実装 | 上のデータと真正な確認記録が成立してから `page_receipt_model.digest(catalog)` をオフライン計算。fixture版/出典/承認記録と別の非公開承認記録へ固定し、実行hostへ独立配布。PRIVATE/BOOKから起動時に学習しない。今はpin値を作らない |

**対象6項目の取得判定はA 0件、B 1件、C 5件**。Aを増やすために別caseやtestの架空actorを対象証跡へ昇格させない。補助証跡としては次のAを再利用できる。

| Aとして取得可能な補助資産 | 出所・版 | 検証方法/限界 |
|---|---|---|
| 独立生成原本hashと保存済み取得bytes | `gate-a-original-reconciliation-private.json`（2026-11-07T23:51:16Zの記録）＋`gate-a-sa-readonly-evidence-private.json`、PNG version4の過去証跡 | 保存PNGのSHA-256再計算一致。実ID/hash値はprivateのまま。現在のGoogle版、印字内容や総額本人確認を証明しない |
| 730円のUI作成経緯・機械vector/AT | `gate-b-setup-requests-private.json`＋成功記録、初回manifestと再試験01 config | 全vectorから17.2記載の式でAT再計算一致。UI経緯の証拠に限り、正規候補・未使用の登録UUID・本人確認には転用しない |
| 11/12列の形式と生成/検証関数、catalog strict validator | 基準commitの `receipt_review_snapshot.py`、`reconciliation.py`、`receipt_private_adapter.py`、関連tests | 既存関数のまま新fixtureのデータ検証に使える。テストの成功やvalidな形式だけでは承認/本人性を証明しない |

### 18.3 新規fixtureは必要か

**必要。新規に必要なのは承認済みの合成データ一式であり、追加の原本画像・BOOK・DB・サーバーではない。** 既存SOURCE PNGを独立hash/version確認の上で再利用する案を優先する。ただし旧Gate Bのcase/item IDをそのままcatalogに入れることはできない。

既存 `verified_candidate_range()` は `取込ID == "receipt:" + source_id`、候補IDは `取込ID + ":001"` 等を要求する。Gate B manifestはイベント試験用case IDと `product-a` 等のslot IDであり、この正規候補形式の証跡ではない。新fixtureでは既存生成規則どおりにcase/item identityを定義し、decoderのmachine/ATも同じ承認済み定義から作る。既存coreにGate B専用のID例外を追加するより小さい。実ID・新UUID・fixture bytesは今回発行/生成しない。

最小案は一つの非公開fixture版に、(1)元の原本参照、(2)独立に定義した合成解析payload、(3)候補11列全件、(4)取込12列、(5)カテゴリpairと明示的ルール集合、(6)真正な本人確認/actor証跡への参照、(7)catalog/decoder bindingをまとめる。候補列は既存HEADERSの「候補ID・取込ID・解析hash・行番号・商品名・数量・金額・大/小カテゴリ・備考・レシート合計」をそのまま使う。取込列は既存 `sheets.py` の12列を使う。解析hashは新しい合成解析payloadのhashとして定義し、原本bytes hashと区別する。意図的誤読730円は新fixtureの定義であり、過去にOCRで取得された証拠と称さない。日付/店舗/名称等は原本または新しい合成定義に明示し、実BOOKから逆算しない。

本人確認・actorはfixtureによる代替対象にしない。`ReceiptPrivateAdapter._expected()` はtotal_confirmationとactor形式を必須とするため、現契約のまま本人証跡がなければ実catalogは成立しない。架空actorは純粋mockの中だけに留め、live用pin/JSONへ入れない。本人証跡なしで「検証済み・未記帳」へ進むための緩和も実施しない。継続仕様12.2の検証要求と本人会計authorityの分離は維持し、確認方法の変更が必要ならその契約差分を先に承認する。

将来のBOOK適用では旧Gate B試験行/入力/メモ/使用済みUUIDを保持し、同じ「確認」タブに新fixtureの独立case行を追加する案を優先する。既存の退避/保留行を再利用しない。新caseの表示/機械列・登録ticketの更新範囲は適用前に具体化する。新case名だけで同sourceの未確定要求を回避せず、旧試験が未記帳・dispatchなしであった記録とPRIVATE履歴を独立照合する。3明細以内の一例に絞り、枠拡張は今回の必須条件にしない。

### 18.4 run-bound permitの再利用を誰がどう防ぐか

17.1のrun bindingだけでは一回性は完成しない。**ownerが発行範囲を決め、固定SHAのhostが各起動で強制拒否し、runner内の排他的開始markerが同じrun内の二重起動を拒否する**。レビューを通ったというフラグをworkerが何度でも使える設計にしない。初期の隔離一回受入について、既存CAS probe helperの `marker.open("x")` と既存のexclusive file/fsyncパターンを再利用する。helper自体や金融capability storeを起動・転用しない。

| 責任者/境界 | 最小の強制条件 |
|---|---|
| owner（permit発行・再開判断） | 取得済み非公開計画に対象UUID/段階、catalog/snapshot、PRIVATE、repo/workflow/実装SHA、run_id/attempt=1/job、期限を結合して一回だけ許可。UNKNOWNが残る同UUID/同case/sourceへ新permitを発行しない。許可済み・開始済み・停止の記録を保持し、紛失時は停止。通常iPhone操作を恒久的な取引ごとの手動承認へ変える案ではなく、初回隔離受入の実行管理 |
| 固定SHAの起動wrapper/host（実装必要） | 既定GET-only。Actions提供のrun/attempt/workflow SHAとauthoritativeなrun/job情報を照合し、permit本文やユーザー入力のrun番号を信じない。別run/別job・attempt>1・期限外・署名/承認出所不明・履歴欠落はwrite資格取得前に拒否。environment承認だけでこの検査を代用しない |
| 同一jobの起動wrapper（実装必要） | checkout/再生成で消さない同一runnerのprivate作業領域にpermit単位の `.started` を**排他的新規作成**し、内容flush/fsync完了後だけ資格取得/最初の副作用を許す。markerが存在/破損/作成不能ならGET-only。markerを削除/resetするコードやcleanupを作らない。二重processは同じpathを競合し一つだけが進む。adapterの新instanceへwrite能力を再付与しない |
| Actions job設定（実装/設定必要） | 単一job・単一worker process、restart/step retry/matrixなし、専用concurrency cancel=false。同runのrerunはattempt検査で拒否、新runはbinding不一致で拒否。同一attemptを別runnerへ自動移送/復元して再実行する運用は認めず、runner喪失・marker喪失はUNKNOWN停止。ephemeral runnerのmarkerをrun間の永続台帳とは呼ばない |
| private journal/core（既存再利用） | UUIDのprepare/accept/claim・FINAL・同case/sourceの未確定拒否とIf-Match CASを維持。永続UNKNOWNは再開不可。保存不明を記録するための追加PUTや、GET成功によるmarker解除は行わない |
| ownerとhost（障害後） | GETとrun/attempt/markerを照合して状態を確定。FINALなら表示だけ。未確定なら停止継続し、同UUIDの自動再送0。ownerもmarkerの削除・過去journalへのrollbackを復旧手順にしない。新permitの判断は既存要求の再実行許可と区別する |

`run_attempt`はrerunで増え、rerunは元のSHA/ref/権限を再利用するため、SHA一致や実行者一致だけでは拒否できない。[Actions context](https://docs.github.com/en/actions/reference/workflows-and-actions/contexts#github-context)、[rerun仕様](https://docs.github.com/en/actions/how-tos/manage-workflow-runs/re-run-workflows-and-jobs)。environmentのrequired reviewersは資格解放前の審査に利用可能だが、同一permitの消費台帳ではない。[environment仕様](https://docs.github.com/en/actions/how-tos/deploy/configure-and-manage-deployments/manage-environments)

この方式は、ownerと固定host、単一jobのrunner lifetimeを信頼する**初回一回受入の最小契約**。管理者によるmarker削除/履歴rollbackや、同一attemptを別machineで偽装実行する者まで防ぐ保証ではない。そのような再配置を許す構成、Apps Scriptを含む複数writerの常時自動受付、processの自動引継ぎには、同じ既存PRIVATEにoperation/permitの消費をCASで保持する最小schema追加等の独立レビューが必要になる。現v1 envelope/journal v2には勝手にkeyを追加できない。新DB/新サーバーを提案せず、金融 `SqliteCapabilityStore` のDBも追加しない。**その拡張を済ませたことにはせず、該当運用はNO-GO**。初回受入を超えた通常運用の安全性をlocal markerだけで認定しない。

### 18.5 最小実装の残作業（今回実装しない）

区分は最初の隔離iPhone導線を「検証済み・未記帳」まで通すためのもの。本番/金融writeの必須条件を削る区分ではない。

| 区分 | 残作業 | 既存再利用/最小差分 |
|---|---|---|
| **必須** | 新規合成fixtureの定義・承認、真正なtotal_confirmation/actorの確保、独立pin | 18.2～18.3。既存candidate_rows/parse_import_rows/parse_rules/digest/strict validatorを使う。actor不明なら接続不可 |
| **必須** | 非金融の薄いDrive IO | 17.3の固定ID・v2 If-Match・401再送/HTTP retry/redirect抑止・独立GET。既存PDF adapterは変更しない |
| **必須** | run-bound host/wrapperの一回性 | 18.4のrun/job照合、exclusive marker、起動GET-only、UNKNOWN停止。既存journal/adapter/claimを再実装しない。再起動/二重process/別run/期限切れ/記録不能をmockで拒否 |
| **必須** | Sheets実read・ticket準備/受付・結果投影のport | 46列decoder、JS snapshot、ValidationJournal、IndependentReceiptReader、FINAL cacheを接続するだけ。背景準備/checkbox公開の競合・入力保持も受入対象。自動Apps Script portの再送停止を証明できなければ開かない |
| **必須** | UUID-onlyの非金融Actions入口と資格経路 | 既存category workflowの形式を参考にし、金融env/Secretsは転用しない。main凍結下で新workflow_dispatchが利用できるとは仮定しない。default-branch登録の個別承認がない間は該当入口を停止 |
| **必須** | 対象の初期状態/ACLと独立read-back、実CAS・iPhone導線の段階受入 | 別承認後だけ。既存Gate A/CAS過去証跡とUI合格部分を再利用し、未測定の差分だけを試験。Gate Bの3イベント/公開競合/部分sortの対象制限は未解決のまま通さない |
| **省略可能（今回の一例）** | 追加PNG/BOOK/第4Drive資源、新しい認証Web、DB/サーバー、OCR再解析 | 既存PNG/BOOK/PRIVATEと意図的誤読fixtureを使う。新しいOAuth/本人確認基盤の構築を自動的な次作業にしない |
| **省略可能（初回一回受入）** | 自動復旧、失敗run回収、無人permit発行、runner間の引継ぎ | 機能を持たずUNKNOWNで止まる。常時自動運用まで許す場合の永続消費設計は省略不可、別工程 |
| **省略可能（3明細以内）** | 商品枠拡張・新UI・新formatter/planner/カテゴリengine | 確定済み1タブ/1行1案件を維持。既存正規化、金額/税/値引き/重複検証を再実装しない |
| **省略可能** | 過去の全件回帰/端末UI採用試験のやり直し | 文書だけの今回は新テスト不要。後の実装差分に対応するmockと、未完了の実接続/iPhone受入は必須 |

### 18.6 次に承認が必要な操作

次の最小承認単位は **「既存原本を使った新規合成fixtureのオフライン作成・policy確定・既存validatorでの検証」**。非公開データ定義、版と出典、候補/取込ID、解析hash、カテゴリpairと明示的rules=0の是非を対象にし、旧Gate B行を正規証跡へ改名しない。本人確認/actorの保存先が得られなければ、その部分は未成立として残す。fixture作成承認は本人確認の完了でも、架空actor利用の承認でもない。

続いて必要なのは、真正な本人確認をどう取得・検証するかの具体的な承認、および18.4/18.5の**オフラインだけの薄いIO/host/port実装**の承認。これらが揃ってから、別段階でPRIVATE初期化bytes・対象ID・変更回数・read-back/停止手順を承認する。BOOKの新case行/ticket/cache、Apps Script project/trigger/認可、Actions入口/dispatch資格/必要な設定はそれぞれ具体化して承認する。現在禁止されているmain変更・本番Secrets変更・dispatchを、この計画で黙示的に許可しない。

今回の成果は①A/B/C一覧と解消方法、②新fixture必要/真正actor代替不可の判断、③必須/省略可能な最小残作業、④上記の次承認単位。**本Markdown一件のみ更新**し、fixture生成・製品/テスト変更・Google write・Apps Script実行・trigger・Actions dispatch・金融write・本番設定/Secrets・main変更・mergeは0。Drive追加操作は上記metadata検索のみで、原本/実BOOK/PRIVATEの新規内容取得はしていない。Gate B総合・Gate C実環境・本番NO-GOを維持し、実装・接続は次の承認まで停止する。
