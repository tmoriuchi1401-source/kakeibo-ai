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

PCブラウザ・iPhone実機に接続できていないため、calendar選択、保存、
再オープンの実操作は未検証。SheetsアプリとSafariの差も未確認。
標準pickerがiPhoneで十分か確認できるまで、独自UIは追加しない。
検証のための架空の日付を本人入力欄へ保存しない。
