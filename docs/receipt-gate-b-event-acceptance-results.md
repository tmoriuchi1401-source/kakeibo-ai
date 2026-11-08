# PR #200：Gate Bの実施記録

**Gate B総合NO-GO。合成BOOKのAPI設定とオフライン実装まで完了。専用bound project・trigger・認可・iPhoneイベントは未実施。** 基準commit `54de3271eaf423848545bc540cefdf043ae286e7`。Gate A/CAS/原本hashの再試験は行わない。

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

## 停止理由と未完了

Computer UseがWindows上の現在ブラウザーURLを十分な確度で判定できず、このターンのUI操作を停止した。別のUI経路でその判定を迂回せず、Google Sheets connectorで許可済み設定とローカル実装/検証だけを進めた。専用projectは未作成、trigger 0、新project認可画面は未到達。

manifestの**宣言scope候補**は`https://www.googleapis.com/auth/spreadsheets.currentonly`、予定実行者は既存owner、用途は当該合成BOOK内のevent観測ログだけ。**実際の認可要求scope/実行identityはまだ観測できていない**。初回認可を既存projectの認可で代替せず、認可が要求された時点で実表示を提示して停止する。

| 項目 | 判定 |
|---|---|
| 合成Sheets設定 | PASS（API独立読戻し・金額検算。native/実機表示は未確認） |
| 専用project設定 | 未実施（配置用コードだけ準備） |
| onEdit trigger | 未実施・0件 |
| iPhone商品編集 | 未実施 |
| UUIDイベント | 未実施 |
| oldValue一致 | 未実施（合成は成功） |
| sort時の案件誤認防止 | 未実施（合成は成功） |
| 入力・メモ保持 | 未実施（実機未確認。合成/今回API差分では保持） |
| 対象外write | 0件 |
| Gate B総合 | **NO-GO** |

次の具体的操作は、PCのChrome通常ウィンドウで対象の合成BOOKを開き、URLがアドレスバーに表示される状態にすること。その後、専用bound projectを1件だけ作成・コード配置し、認可要求時に停止する。現在のcheckboxは認可/trigger待ちであり、iPhoneのチェック操作は設定完了後に案内する。

設定完了後の最小実機手順は、iPhone「確認」→原本を見る→商品BのU2（全行sort後は案件IDで探す）730を868へ→E/Fの1,368/0→メモと修正値保持→専用チェック1回→非表示ログの同UUID/oldValueを照合。iPhoneで発生していない境界は合成と記録し、欠落/UNKNOWNは再送せず専用triggerだけを停止する。Gate C・本番接続は別承認のまま。

補足：数量を2へ変える追加検算案は自動承認レビューが「指定ケースの数量と異なる」として拒否し、Googleへ送信されていない。レビューを迂回せず、明示された数量1で行金額だけを変更する安全な検算と、数量参照のない実式の照合を行った。

実ID・UUID・メール・原本hash・認証情報・詳細event値は、repo外の非公開manifest/証跡/配置版にのみ保持する。public PR/CIログには転記しない。
