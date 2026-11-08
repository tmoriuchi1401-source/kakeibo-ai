# PR #200：Gate Bの実施記録

**Gate B総合NO-GO。利用者のiPhone商品修正・専用チェック1回と入力保持を確認したが、独立読取の観測ログは0件。UUIDイベント・oldValueは未証明。** 基準commit `54de3271eaf423848545bc540cefdf043ae286e7`。Gate A/CAS/原本hashの再試験は行わない。

## 実BOOKへ適用した範囲

- 指定BOOKのID・単一parent・owner＋既存SA writer ACL・「確認」1タブ・初期26列を再確認。26,000セルをbounded読取し、既存入力・入力規則・注記/formatがないことを確認してから適用した。
- 承認されたA:ATへ見出し＋一般合成案件1行＋登録不可退避行1行を設定。A列/見出し固定、商品1・商品2・空き追加枠、カテゴリ/種別/帰属/採否dropdown、原本照合checkbox、UUID専用登録checkbox、原本リンクとメモ欄を設定した。
- 46列へ拡張し、機械用AK:ATを非表示。同BOOKに非表示`_gate_b_events`を1タブ追加。独立metadata GETで可視タブ「確認」1つ、非表示ログ、固定行/列を確認。機械列の非表示はGoogle出力xlsxのcolumn属性でも確認した。
- 独立CellData GETでD/E/F=1,368/1,230/138、Iのcustom BOOLEAN checked/uncheckedの同UUID対応、商品カテゴリ/原本照合規則、空き追加枠を照合した。実UUIDや機械identityを公開しない。
- **API検算（iPhoneではない）**：数量1を保持して商品Bの行金額だけを730→868へ変更し、独立GETでE/F=1,368/0を確認。合計式は行金額だけを足し、数量列や乗算を含まない。後で行金額を730へ復元し、数量1・E/F=1,230/138・メモ未変更を独立GETで確認した。ログは見出しだけでイベント0件。
- Google側の書込みはこのBOOKへのbatchUpdate成功3回（設定一括、金額検算、初期値復元）だけ。PNG/private JSON/旧CAS probe/本番/既存project/定期処理への書込み0。認可・Secrets/Variables・main/validated SHA・Cloud Run・金融write・dispatch・AI送信・原本移動0。

## ローカル実装・検証

新規`apps-script/receipt-gate-b/`に観測専用handlerとscope限定manifestを保存。実ID/UUIDは非公開の配置版だけに入れ、公開templateは未設定・disabled。既存Apps Script code・本番workflowは変更しない。handlerは入力/結果案件行を書かず、同BOOKの非表示ログにだけ観測をappendし、registration/validator/dispatchを呼ばない。

新Node合成17件成功・失敗0。通常TRUE、欠落oldValue、別UUID/oldUUID、複数セル、他BOOK/sheet/列、sort、重複/削除/部分binding、二重タップ、後の入力/メモ保持、append/flush不明、lock競合、disabled/trigger欠落、ログ構造不一致を検査。既存Nodeイベント回帰13件も成功。関連Python75件（新Node harnessを含む）成功・失敗0。既存CIのpytest discoveryを使い、workflow差分は0。

xlsxのローカルrenderで見出し・合計・商品/追加枠の配置を確認した。export先ではcustom checkboxが文字列/数値として表示され、空欄にもrendererの代替表示が出た。対応するnative CellDataが空欄/BOOLEANであることは独立確認済みで、exportの描画を理由にnativeセルを変更していない。**Googleネイティブ描画とiPhone操作性は未検証**。

## 手動設定と初回iPhone操作の追記（2026-11-08）

利用者は対象BOOKの「拡張機能→Apps Script」から開いた初期projectを使用し、配置用コード/manifestの保存を報告した。read-only `receiptGateBPreflight`のエラーなし完了とtrigger 0件、その後の専用onEdit trigger 1件作成をそれぞれ報告。preflight/trigger設定とも新しい認可画面は出なかったとの報告であり、実認可scope画面を観測した証拠とは扱わない。project IDは非公開manifestへ保存したが、trigger UIDおよび実配置コードの独立UI確認は未完了。

iPhone操作前の独立GETでは商品B=730、合計1,230、差額138、専用チェックが既知UUIDのready値、ログ0件だった。有効化して保存後、iPhoneで商品B修正・メモ保持・専用チェック1回だけを行うよう案内した。利用者は「iPhoneで1回チェック済み・値とメモ保持」と報告した。

その後の独立CellData GETで商品B=868、合計1,368、差額0、メモの存在、専用チェックのregister値と既知UUID・案件機械bindingの一致を確認した。メモ本文は監査証跡へ転記しない。ただし、**3回の独立読取で非表示ログは見出しのみ・イベント0件**。利用者による実行履歴の「完了」報告だけでは、登録セルのイベント到達・value/oldValueの対応を証明できない。

続いて利用者から提示された実行履歴画像では、4件とも種類「トリガー」・関数`receiptGateBPreflight`・ステータス「完了」だった。意図した`receiptGateBObserveEdit`の実行証拠はなく、専用triggerの関数設定が不一致。preflightは読取専用でイベントを記録しないため、ログ0件と整合する。チェック操作が4件中のどの実行に対応するかは推測しない。observer実行・oldValue一致は引き続き未証明であり、誤設定を合格扱いしない。

この状態はUNKNOWNとして停止し、同UUID/別UUIDの再送、チェックの解除・再タップ、手動handler実行、合成イベントによる補完、再armを行わない。実機sortや異常系は再現しておらず、既存のオフライン成功と区別する。今回の追記に伴うGoogle側writeは0件。入力・メモはそのまま保持し、Gate C・dispatch・本番接続へ進めない。

既存コードの合成試験とCIは変更していない。設定実装commitのCIは全3ジョブ成功、Python合計4,931件成功・失敗0（一般4,215、sensitive 716）、OCRジョブも成功。これらは実機UUIDイベント成功の代わりにならない。

## UI操作の制約と残る確認

### 誤設定削除後の読取確認と再試験案（未適用）

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
| 専用project設定 | PASS（利用者の配置・preflight完了報告。実コード独立確認は未完了） |
| onEdit trigger | 未実施・現在0件（誤設定はFAILとして保持。削除後0件を利用者画像で確認） |
| iPhone商品編集 | PASS（利用者報告＋868/1,368/0の独立GET） |
| UUIDイベント | FAIL（ログ0件、観測未証明） |
| oldValue一致 | 未実施（実イベント未取得。合成は成功） |
| sort時の案件誤認防止 | 未実施（合成は成功） |
| 入力・メモ保持 | PASS（利用者報告＋修正値・メモ存在を独立GET） |
| 対象外write | 0件 |
| Gate B総合 | **NO-GO** |

次の最小一手は、上記の限定した再試験案に対する承認を待つこと。使用済みUUIDの再送やチェック解除で今回の観測欠落を補完しない。Gate C・本番接続は別承認のまま。

補足：数量を2へ変える追加検算案は自動承認レビューが「指定ケースの数量と異なる」として拒否し、Googleへ送信されていない。レビューを迂回せず、明示された数量1で行金額だけを変更する安全な検算と、数量参照のない実式の照合を行った。

実ID・UUID・メール・原本hash・認証情報・詳細event値は、repo外の非公開manifest/証跡/配置版にのみ保持する。public PR/CIログには転記しない。
