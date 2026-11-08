# PR #200：Gate Bの実施記録

**Gate B総合NO-GO・停止済み。承認された新UUID試験で初回value/oldValueと案件bindingは一致したが、利用者の1回操作報告に対しログは3件。原因は未確定。専用trigger 0件の画像とconfig disabled保存報告を受領し、最終GETでログ3件の保持を確認した。** 基準commit `54de3271eaf423848545bc540cefdf043ae286e7`、再試験計画commit `f5babab0da3e5533351235157cfe32e3cfcdd97f`。Gate A/CAS/原本hashの再試験は行わない。

## 実BOOKへ適用した範囲

- 指定BOOKのID・単一parent・owner＋既存SA writer ACL・「確認」1タブ・初期26列を再確認。26,000セルをbounded読取し、既存入力・入力規則・注記/formatがないことを確認してから適用した。
- 承認されたA:ATへ見出し＋一般合成案件1行＋登録不可退避行1行を設定。A列/見出し固定、商品1・商品2・空き追加枠、カテゴリ/種別/帰属/採否dropdown、原本照合checkbox、UUID専用登録checkbox、原本リンクとメモ欄を設定した。
- 46列へ拡張し、機械用AK:ATを非表示。同BOOKに非表示`_gate_b_events`を1タブ追加。独立metadata GETで可視タブ「確認」1つ、非表示ログ、固定行/列を確認。機械列の非表示はGoogle出力xlsxのcolumn属性でも確認した。
- 独立CellData GETでD/E/F=1,368/1,230/138、Iのcustom BOOLEAN checked/uncheckedの同UUID対応、商品カテゴリ/原本照合規則、空き追加枠を照合した。実UUIDや機械identityを公開しない。
- **API検算（iPhoneではない）**：数量1を保持して商品Bの行金額だけを730→868へ変更し、独立GETでE/F=1,368/0を確認。合計式は行金額だけを足し、数量列や乗算を含まない。後で行金額を730へ復元し、数量1・E/F=1,230/138・メモ未変更を独立GETで確認した。ログは見出しだけでイベント0件。
- 初期API設定時点のGoogle側書込みは、このBOOKへのbatchUpdate成功3回（設定一括、金額検算、初期値復元）。後日の承認済み再試験で4セル更新を1回追加した（下記）。PNG/private JSON/旧CAS probe/本番/既存project/定期処理への書込み0。Secrets/Variables・main/validated SHA・Cloud Run・金融write・dispatch・AI送信・原本移動0。

## ローカル実装・検証

新規`apps-script/receipt-gate-b/`に観測専用handlerとscope限定manifestを保存。実ID/UUIDは非公開の配置版だけに入れ、公開templateは未設定・disabled。既存Apps Script code・本番workflowは変更しない。handlerは入力/結果案件行を書かず、同BOOKの非表示ログにだけ観測をappendし、registration/validator/dispatchを呼ばない。

新Node合成17件成功・失敗0。通常TRUE、欠落oldValue、別UUID/oldUUID、複数セル、他BOOK/sheet/列、sort、重複/削除/部分binding、二重タップ、後の入力/メモ保持、append/flush不明、lock競合、disabled/trigger欠落、ログ構造不一致を検査。既存Nodeイベント回帰13件も成功。関連Python75件（新Node harnessを含む）成功・失敗0。既存CIのpytest discoveryを使い、workflow差分は0。

xlsxのローカルrenderで見出し・合計・商品/追加枠の配置を確認した。export先ではcustom checkboxが文字列/数値として表示され、空欄にもrendererの代替表示が出た。対応するnative CellDataが空欄/BOOLEANであることは独立確認済みで、exportの描画を理由にnativeセルを変更していない。この時点のGoogleネイティブ描画とiPhone操作性は未検証であり、後続の実機結果は以下に分けて記録する。

## 手動設定と初回iPhone操作の追記（2026-11-08）

利用者は対象BOOKの「拡張機能→Apps Script」から開いた初期projectを使用し、配置用コード/manifestの保存を報告した。read-only `receiptGateBPreflight`のエラーなし完了とtrigger 0件、その後の専用onEdit trigger 1件作成をそれぞれ報告。preflight/trigger設定とも新しい認可画面は出なかったとの報告であり、実認可scope画面を観測した証拠とは扱わない。project IDは非公開manifestへ保存したが、trigger UIDおよび実配置コードの独立UI確認は未完了。

iPhone操作前の独立GETでは商品B=730、合計1,230、差額138、専用チェックが既知UUIDのready値、ログ0件だった。有効化して保存後、iPhoneで商品B修正・メモ保持・専用チェック1回だけを行うよう案内した。利用者は「iPhoneで1回チェック済み・値とメモ保持」と報告した。

その後の独立CellData GETで商品B=868、合計1,368、差額0、メモの存在、専用チェックのregister値と既知UUID・案件機械bindingの一致を確認した。メモ本文は監査証跡へ転記しない。ただし、**3回の独立読取で非表示ログは見出しのみ・イベント0件**。利用者による実行履歴の「完了」報告だけでは、登録セルのイベント到達・value/oldValueの対応を証明できない。

続いて利用者から提示された実行履歴画像では、4件とも種類「トリガー」・関数`receiptGateBPreflight`・ステータス「完了」だった。意図した`receiptGateBObserveEdit`の実行証拠はなく、専用triggerの関数設定が不一致。preflightは読取専用でイベントを記録しないため、ログ0件と整合する。チェック操作が4件中のどの実行に対応するかは推測しない。observer実行・oldValue一致は引き続き未証明であり、誤設定を合格扱いしない。

この状態はUNKNOWNとして停止し、同UUID/別UUIDの再送、チェックの解除・再タップ、手動handler実行、合成イベントによる補完、再armを行わない。実機sortや異常系は再現しておらず、既存のオフライン成功と区別する。今回の追記に伴うGoogle側writeは0件。入力・メモはそのまま保持し、Gate C・dispatch・本番接続へ進めない。

既存コードの合成試験とCIは変更していない。設定実装commitのCIは全3ジョブ成功、Python合計4,931件成功・失敗0（一般4,215、sensitive 716）、OCRジョブも成功。これらは実機UUIDイベント成功の代わりにならない。

## 承認された新UUID試験の実施結果（2026-11-08）

利用者は`f5babab`の隔離観測試験1件を承認し、変更範囲の確認でIとAR:ATを指定した。旧UUIDは使用済みのまま保持し、新UUID v4・新run ID・固定bindingを非公開に準備。既存handler本体・manifest scope・テスト・workflowは変更していない。ローカルで新configの固定binding一致・旧UUID拒否を確認したが、これはオフライン確認であり実機証拠に数えない。

### 設定と独立読戻し

- 利用者の専用project config全体画像でenabled=false、新UUID/run ID/bindingと配置版の一致を確認し、続けて保存済みとの報告を受領した。Apps Script APIによる取得ではなく、画面と利用者報告による証拠。
- BOOKの指定ID・単一parent・owner/既存SA writerだけのACL、可視「確認」/非表示ログの構造を再確認。案件IDと固定原本identityで一意に対象を照合した。
- disabled設定・trigger 0件の準備段階で、同BOOKへのbatchUpdate **1回・2 requests**でIとAR:ATの4セルだけを更新。独立CellData GETで新ready値・custom checkbox規則・新UUID/run/bindingとconfigの一致を確認。変更前後の差分は4セルだけで、メモ・原本リンク・商品欄・その他セル・既存ログは完全一致した。跨サービス原子性は仮定していない。
- 利用者提示のtrigger一覧画像で、1行・`receiptGateBObserveEdit`・Head・スプレッドシートから/編集時を確認。誤設定のpreflightではない。利用者はその後enabled保存済み・新規認可なしと報告した。実認可scope画面を観測済みとは扱わない。
- 操作直前の独立GETで、新UUIDのready状態・固定binding一致、商品B868/合計1,368/差額0、メモ存在、ログ0件を再確認してからiPhoneのチェック1回を案内した。

### 実機操作報告と3件のログ

利用者は「iPhoneで1回チェック済み」と報告した。独立GETでは同run・同sheet・単一Iセル・同一の有効な形式のtrigger UIDで、次の3件を観測した。

| 順序 | 同UUIDの値の方向 | 保存された判定 |
|---|---|---|
| 1 | ready → register | `OBSERVED_UNAPPROVED` |
| 2 | register → ready | `REJECT_EVENT_IDENTITY` |
| 3 | ready → register | `REJECT_DUPLICATE` |

初回のevent.value/oldValue、要求UUID、run ID、案件ID、sheet/単一セル形状と固定machine bindingはすべて期待値に一致した。3件のtrigger UIDは同一で、形式検査も成功した。ただし、trigger一覧UIからUID自体を独立取得して照合した証拠はない。

**1回操作報告と3件の発生との対応・原因は未確定。** 実ログの方向は確認できるが、人が3回操作した、iPhoneが重複配送した、同期が原因だった、などと推測しない。後続2件は既存のidentity/duplicate検査で拒否された。この挙動を「意図的な実機二重タップ試験成功」へ読み替えず、予定した1回観測試験はNO-GOで停止した。観測成功は本人承認・登録受付・検証済み・会計authorityを意味しない。

### 停止・保持・書込み範囲

3件を検出後、再タップ/自動再送/新UUIDの再発行をせず、専用config disabled保存と今回のtrigger1件だけの削除を案内した。利用者から「disabled保存済み」と、フィルタなしでtrigger0件を表示する画像を受領。config disabledは利用者の保存報告、trigger0件は提供画像による確認であり、Apps Script APIによる独立列挙ではない。

停止後の最終独立GETではログが引き続き3件で、保存した観測時ログの全セルと一致した。案件は一意、machine binding一致、商品B868・合計1,368・差額0、メモは観測時と一致し、チェック値はregisterのまま保持した。チェック解除やログ消去を停止方法に使っていない。この新UUIDも使用済みとして保持し再利用しない。

Workからのこの再試験のGoogleデータwriteは指定BOOKへの4セル準備batch1回だけ。BOOK内には観測ログ3行が残る。利用者は承認範囲の専用project configと専用triggerだけを操作した。対象外データwrite・private JSON/PNG/旧CAS probe変更・金融write・dispatch・AI送信・原本移動・本番変更は0。Cloud Run・main・validated SHA・Secrets・Variables・既存project/定期処理は変更していない。

次の最小作業は、停止状態を維持したまま、既存の実行履歴と利用者の操作状況を読取/聞取りで照合し、3件の発生原因と未確認点を整理すること。今回の承認はこの1試験までであり、新しい実機操作・trigger再作成・再有効化・Gate C接続には進まない。原因調査のための新ログ機能や権限追加も行っていない。

## 当初の再試験案とUI操作の制約（履歴）

### 誤設定削除後の読取確認と再試験案（承認前の記録）

利用者は誤設定の`receiptGateBPreflight` trigger 1件を手動削除したと報告。提示された専用projectのtrigger一覧画像はフィルタなしで「0個のトリガー」を表示している。この確認は利用者提供画像によるもので、Apps Script APIによる独立列挙ではない。

続いてconnector metadata GETで「確認」1可視タブと非表示ログの構造を確認し、`_gate_b_events!A1:M200`の全2,600セルを読取。期待する13列見出しが一致し、観測イベントは0件だった。同時に商品B=868、合計1,368、差額0、メモ存在、旧UUIDのregister値保持を確認した。今回の読取ではメモ本文を証跡へ転記せず、前回との本文一致を新たに立証したとは扱わない。

旧UUIDは観測未成立の使用済み試験として終了し、再利用しない。新UUIDを使う場合も受付UNKNOWNの再送ではなく、backend未接続の独立したGate B観測runとする。**以下は再試験の提案のみ。新UUID生成・BOOK更新・config更新・trigger作成・有効化・再操作は今回行っていない。**

| 対象 | 別途承認後の最小変更 |
|---|---|
| 非公開の試験証跡 | 旧UUID/run/bindingと失敗理由を保持し、使用済みとして扱う。新UUID v4・新run ID・対応固定bindingを用意し、旧UUIDと異なることを確認 |
| 同じ合成案件のI列1セル | checked=`register:<新UUID>`、unchecked=`ready:<同UUID>`のcustom checkbox規則とready値だけを設定。旧UUIDを解除して再利用しない |
| 同案件のAR:AT 3セル | 新UUID・新run ID・新runの固定bindingへ変更。AK:AQの案件/原本/item identityは保持 |
| 既存専用projectのprivate config | `enabled=false`で新runId・ticket.uuid・ticket.machineの対応要素を更新。BOOK/sheet/log ID・case ID・handler本体・manifest scopeは維持 |
| 同専用projectのtrigger | `receiptGateBObserveEdit`／Head／スプレッドシートから／編集時を**1件だけ**作成。保存後の一覧で関数名・種類・件数を確認。preflightを選ばない |

実施順序は次のとおり。

1. 承認後、trigger 0件、想定外の実行/入力/構造変更なしを再確認。対象は行番号ではなく案件IDと固定原本identityの一意一致で探し、重複/変更があれば停止する。
2. 専用configをdisabledで準備し、trigger 0件の間に上記IおよびAR:ATだけを更新。同一BOOK内の一括更新を使い、その後の独立GETで規則・ready値・machine bindingとconfigの一致を確認する。private configとSheetsの跨サービス原子性は仮定しない。
3. `receiptGateBPreflight`は必要なら**エディタからのread-only確認だけ**に使う。trigger対象にしない。実際のコード/config・宣言scopeを照合し、新たなOAuth要求が出たら実scope/主体/用途を提示して認可前に停止する。
4. 正しい専用trigger 1件を作成し、一覧で関数名を確認してからobserverのenabled=trueを保存する。すべての確認完了まで利用者はチェックに触れない。必要な認可が未成立ならtriggerも有効化しない。
5. iPhoneで既存の868/1,368/0とメモ保持を確認し、新UUIDの専用チェックを1回だけ操作。商品金額を730へ戻す必要はない。既存の商品編集実機成功は再利用し、このrunはUUID/oldValue観測を目的とする。
6. 同BOOKのログで新run・新UUID・同UUIDのready→register・案件・trigger UIDと`OBSERVED_UNAPPROVED`を照合する。本人認証/登録受付/検証済みを意味しない。ログ欠落・複数観測・不一致・不明は停止し、再タップや自動再送をしない。sort実機試験はこの1回の成功へ含めず、未実施のまま区別する。

商品欄・数量・カテゴリ・メモ・原本リンク・計算式・既存ログ・退避行を更新対象に含めない。新project/新タブ/新Drive資源・追加scope・Secrets・JSON更新は不要。停止時は今回の専用trigger 1件だけを削除し、同projectのenabled=falseを保存する。実行中の処理取消しを保証せず、ログをGETで照合し再送しない。

**次の承認対象は上記の「同BOOK内の新UUIDによる独立観測run」1件のみ**。今回は読取と計画文書だけでGoogle側write 0、金融write 0、本番変更0。Gate B/C・本番接続はNO-GOを維持する。

Computer UseがWindows上の現在ブラウザーURLを十分な確度で判定できずUI操作を停止した。別のUI経路でその判定を迂回せず、その後の専用project配置・trigger作成は上記の利用者手動報告として記録した。Sheets connectorでの独立確認と、手動報告の証拠範囲を分ける。

manifestの**宣言scope**は`https://www.googleapis.com/auth/spreadsheets.currentonly`、予定実行者は既存owner、用途は当該合成BOOK内のevent観測ログだけ。**実際の認可要求scope/実行identityは観測できていない**。今後認可が要求された場合も、実表示を提示して認可前に停止する。

| 項目 | 判定 |
|---|---|
| 合成Sheets設定 | PASS（API独立読戻し・iPhone修正後合計確認） |
| 専用project設定 | PASS（private config画像一致・保存報告。handler本体の実配置API確認は未完了） |
| onEdit trigger | PASS（正しい関数の1件を画像確認。その後停止・現在0件の画像確認） |
| iPhone商品編集 | PASS（利用者報告＋868/1,368/0の独立GET） |
| UUIDイベント | 初回一致、全体FAIL（同runに3件。1回操作報告との対応が未確定） |
| oldValue一致 | PASS（初回は同UUIDのready値） |
| sort時の案件誤認防止 | 未実施（合成は成功） |
| 入力・メモ保持 | PASS（4セル準備更新前後の完全一致、観測後/停止後も保持） |
| trigger UID | 同一UID・形式検査PASS。実trigger UIのUID独立照合は未完了 |
| 対象外write | 0件 |
| Gate B総合 | **NO-GO・停止済み** |

上記の承認前計画は履歴として保持する。最新状態は新UUID試験の3件検出によるNO-GO・停止済み。使用済みUUIDの再送やチェック解除で補完しない。Gate C・本番接続は別承認のまま。

補足：数量を2へ変える追加検算案は自動承認レビューが「指定ケースの数量と異なる」として拒否し、Googleへ送信されていない。レビューを迂回せず、明示された数量1で行金額だけを変更する安全な検算と、数量参照のない実式の照合を行った。

実ID・UUID・メール・原本hash・認証情報・詳細event値は、repo外の非公開manifest/証跡/配置版にのみ保持する。public PR/CIログには転記しない。
