# PDFページ確認: 支払日の入力

支払日セルは標準の `DATE_IS_VALID` validation（strict）と
`yyyy/mm/dd` number formatを使用する。空欄は許可し、直接入力も残す。
入力欄は隣の「支払日」ラベルと同じ行に配置する。独自カレンダー、
新しいシート、入力済み日付の一括書換えは追加しない。

現在の14ページcanaryの入力セルはB7（p1）、B42（p4）、B56（p10）、
B70（p14）。ラベル列108pxと入力列208pxで、技術列C:Pはhidden。
セルには編集用の色と日付入力のヒントを設定している。

## 保存・互換性

再表示時に既存の日付をRAW文字列へ戻さないため、UI publisherだけが
有効なISO／スラッシュ日付・date・整数date serialをSheets date serialへ
変換する。1899-12-30からの暦日の差を使い、timestampやUTC変換を使わない。
不正な既存値は推測・消去せず、表示更新前に停止する。

Apps Scriptのdisplay value取得とrunnerの既存日付解釈は変更しない。
会計処理、確定処理、authority、digest、identity、privacyは変更しない。

## 検証結果と未確認事項

実シートの4セルは空欄のまま。設定のread-backと既存セル値の一致を
確認した。合成テストでは月初・月末・年跨ぎ・うるう日、旧形式、
不正値、空欄、暦日の往復、UI再表示時のnumeric storageを検証する。
タイムゾーン設定UTC／Asia/Tokyo／America/Los_Angelesでも暦日は同じ。

Google公式のPC操作は、入力済み日付のダブルクリックまたは`@date`。
空欄セルの1回タップでpickerが開くとは保証しない。
https://support.google.com/docs/answer/12319513?hl=en

本人のiPhoneでは標準カレンダーが表示されなかったとの確認を受けた。
スマホの正式運用は `yyyy/mm/dd` の直接入力とし、カレンダー表示を
完了条件にしない。PC pickerの実操作とSheetsアプリ／Safariの差は未確認。
独自カレンダーUIは追加しない。
検証のための架空の日付を本人入力欄へ保存しない。

## 円の金額欄

旧 `#,##0.########` 表示では整数にも末尾の小数点が残り、Apps Scriptの
既存入力完了チェックと合わなかった。円の表示は `#,##0` に統一する。
空欄または正の整数だけを受け付けるstrictなCUSTOM_FORMULAを同時に設定し、
小数を表示上だけ丸めて確定できる状態にはしない。
既存入力が正の整数／空欄であることを確認してから表示設定だけを適用する。

再表示では既存 `_money` validatorで有効な本人入力だけをnumeric valueに
戻す。過去の末尾小数点表示も金額を変えず保持でき、小数・不正値は表示更新前に
停止する。Medical parser／writer／確定処理／authorityは変更しない。
