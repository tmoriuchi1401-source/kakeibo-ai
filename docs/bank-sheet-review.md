# 管理Spreadsheetの銀行確認

## 統合先

「カテゴリ操作」の既存3ブロックの後に、任意の第4ブロック
`■ 4. 銀行取引をまとめて確認` を置く。既存3ブロックだけを含む過去の受付も有効。
カテゴリの大／小カテゴリと、銀行の経済的用途は別のルール正本を使う。

表示はA～F。各グループの主行は条件・代表原本リンク／件数・金額・期間／用途／
収入分類／今後の自動判定／反映checkbox。下の補助行のC・Dで、自己口座間振替の
場合だけ相手銀行・口座aliasを入力する。既知の銀行は選択でき、他行は銀行名を
入力できる。新しい相手口座関係は補助行Eの「自分の口座と確認」が必要。主行Fの
反映だけでは口座所有の確認にならない。口座番号は入力しない。
確認した銀行・aliasの関係は受付UUIDを伴う銀行確認結果へ保存する。同じ受付の
後続グループや後の受付では、そのactiveの確認結果にある関係を再利用できる。
別の独立した口座登録画面を増やさない。用途以外の摘要一致や同額振込から所有者を
推測しない。将来の自動判定は、引き続き主行Eで「登録する」を選んだ場合のみ。
原本リンクにはPDFページと原本行の表示を付ける。原本リンクは意味の証明ではない。

## グループと固定snapshot

`bank_review_groups.make_groups` は銀行、alias、方向、normalized description、
原文摘要、取引種別、counterparty、現在の分類・未確定理由・収入判断・既存ルール
との一致を保持する。自動入金は金額で分け、用途不明の同名振込で金額差が5倍以上
ある場合も金額で分ける。金額は用途の根拠にしない。単発も通常のグループ。

表示ID（R01等）は人間向け。固定group keyは完全一致条件のSHA256で、既存取込の
transaction identityを変更しない。固定snapshotは原本identity・hash・日付・
金額・摘要・alias・PDF SHA256・原本位置と、表示時の取込／収入記帳状態を含む。
条件または対象・記帳状態が変わった場合、以前の回答やcheckboxは引き継がない。
セル上限を超えるsnapshotは切り捨てず停止する。

`candidates_from_pdf` は既存classifierの結果と厳密な記帳証拠を使う。
`bank_income` ラベルだけの取引は記帳済みとみなさない。
記帳済み完全一致でも明示operator reviewは残す。未記帳の確定利息は人間の用途
判断候補に混ぜず、別のsettlement対象として扱う。

## 受付の共通部分

上部B1からの既存Apps Script、UUIDのみのActions dispatch、隠し受付の固定12列
snapshot、claim・不確実writeを自動再試行しない構造を再利用する。
第4ブロックは第3ブロックの取引として解釈しない。
古いカテゴリ受付の実行後も、新設された銀行欄と未提出回答はそのまま保持する。
実行指示後の編集は固定keyで保持し、受付済みの判断へ混ぜない。

銀行回答の受付は `BANK_REVIEW_ENABLED=true` の明示設定が必要。
無効時に銀行の反映がチェックされた受付は、カテゴリや銀行を書き込む前に停止する。
本番UIは対応する受付・再処理backendの配置とauthority確認後に有効化する。

## 更新・ホーム・回答後の再処理

`bank_review_refresh` は本番原本と3台帳を再読込し、第4欄の候補を更新する。
用途未確定と、用途確定済みの記帳待ちを別の件数で表示する。既存明細と原本・
取込・収入分類が完全一致した候補だけを処理済みとして外す。bank_incomeという
取込ラベルだけで候補を消さない。未記帳の確定収入はグループ数と別に件数表示する。
未送信の用途・収入分類・未来登録・送金元入力は保持し、原本または対象が変われば
checkboxを解除して再確認を表示する。正本・原本・台帳・UIの直前再照合と表示の
read-backが必須。不確実な表示更新を自動再試行しない。

既存の所有ホームに「分類・ルールを整える」、支出カテゴリへのリンク、銀行確認
件数のリンクを置く。第4欄の位置をmarkerから検索し、他ブロックの行数が変わっても
リンクを追従させる。用途確認・記帳待ち・収入未記帳・不一致がすべて0なら銀行リンク
を空欄にする。既存の月選択・集計・他シートは対象にしない。独自のホーム見出しなら
自動変更を保留する。native数式の計算とスマホ実表示は本番設置時に確認する。

共有受付のmetadata保存とUI結果mergeの後、`bank_review_replay` が既存の
production_flowの銀行runnerだけを実行できる。`BANK_REVIEW_REPLAY_ENABLED=true`
の追加の有効化が必要で、未設定は再処理無効。受付UUIDがCATEGORY_REQUEST_IDと一致し、
main・validated SHA・dispatch・専用workflow・両銀行flag・旧scheduler停止の全条件を
確認する。専用workflowは既存と同じkakeibo-production lockを保持する。
他sourceやグローバル記帳stageは起動しない。

銀行runnerは既存の暗号化state binding、production ledger、bank-recurring SQLite、
standing authority、監査キー、income enabled、window/account/row boundをそのまま使う。
この接続は古いPDFへの書込許可を増やさない。回答全件がheldなら再処理を起動しない。
再処理完了後は親ledgerのreadyを読戻し、件数だけを受付結果へ渡す。原本保留が残れば
受付statusはreview。失敗・応答不明の再処理は自動再試行しない。用途判断は保存済み
のまま、台帳と原本を再読込して記帳待ちの表示を更新する。

Sheetで支出と承認済みの既存duplicateに実支出明細がない／不一致の場合は、事前診断
も`unresolved_expense`として保留する。最終archive再照合だけで停止する状態を避ける。

Sheetで振替・返金・対象外と承認されても、既存の金融明細やそのidentityへのリンクが
あれば`unresolved_nonposting`として保留する。取込ラベルだけでなく、取込行が欠けた
孤立明細や異なる明細IDに残ったリンクも確認する。bank_income／auto_expenseの旧
ラベルや既存値不一致も競合。needs_review等の旧statusは未処理のままとし、一律に
無視しない。この保留があるPDFの新規writeと移動は止める。通常の取込・明細は変更
しない。native表示では既存review statusを記帳待ち、金融内容の競合を要確認に分ける。

振替・返金・対象外として保存済みの表示行がInbox評価から消えた場合、消失だけでは
表示を処理済みにしない。captured snapshotの原本IDだけを上限付きで直接読込み、
bank_processed内・Inbox外・処理済み日時あり・未削除のPDFで、実バイトのSHA256が
固定原本と完全一致していることを確認する。ダウンロードの前後と表示更新直前にも
メタデータを再照合する。用途保存内容、現台帳の金融競合／未処理status、未送信入力
も再確認し、すべて満たした行のみ外す。原本消失、hash不一致、未提出の変更は行を
残して旧反映checkboxを解除する。processed全体をスキャンしたり、原本を移動したり
する経路ではない。

## 銀行専用ルール正本

シート名 `銀行自動分類ルール`。18列：

1. rule ID
2. 銀行
3. 口座alias
4. 入出金方向
5. normalized description
6. 原文摘要
7. transaction kind
8. counterparty
9. 金額条件（空欄は金額不問、値があれば完全一致）
10. 判定結果
11. 収入分類
12. 自己口座相手銀行
13. 自己口座相手alias
14. 承認group key
15. 承認日時
16. revision
17. active
18. 適用件数

判定結果はexpense／income／transfer／reimbursement／other_nonwrite／needs_review。
収入分類は既存の給与／賞与／利息／その他確認済収入だけを使う。
PDF名・Drive IDの特例ルールは作らない。
「今回のみ」は未来ルールにしない。同条件の同じactiveルールは追加登録しない。
同じ意味のinactiveルールの再開時だけrevisionを増やす。
既存activeルールと異なる意味への変更はheldにし、自動上書きしない。

「銀行確認結果」は13列の別正本：decision ID、固定group key、snapshot digest、
固定snapshot、用途、収入分類、相手銀行、相手alias、未来登録の承認、受付UUID、
承認日時、revision、active。今回のみの回答は、ここに固定した原本transaction
との一致にだけ適用する。将来ルールは同じ条件と意味を承認したactiveの確認結果
が存在することも必須。同じ受付や同じ回答の再送で正本を二重appendしない。

回答の保存ではlive原本・台帳を2回評価し、選択groupの対象と記帳状態、既存JSON
ルールが変わっていれば保留する。正本も直前に再読込し、2種類の銀行正本への
更新を1回のnative batchUpdateでまとめる。全正本のread-backが一致することを
確認する。不確実な応答を自動再試行しない。記帳やDrive移動はこのmetadata保存
の成功と混同しない。

## 既存JSONルール

確認済みinternal transfer／non-own classification／docomo-smtbの既存評価結果と
Sheetルールを同時に照合する。JSONとSheetの間に優先順位は付けない。
用途・収入分類が異なる場合、または同じtransactionに複数のSheetルールが異なる
意味／相手口座を返す場合はreview。既存の明示needs_reviewも無断置換しない。
金額別グループはSheetの完全一致金額条件で表現できるため、今回の金額別回答を
既存の金額不問JSONキー全体へ広げる必要はない。

## 実装／検証の進捗

実装済み：汎用グループ生成、原本・記帳証拠からの候補抽出、A～F表示行生成、
送金元入力、回答・固定snapshot検証、第4ブロックの読込／保持／受付capture、
銀行専用ルールschema、JSON競合評価、idempotent登録／revision復活、受付からの
回答実行、今回のみの意味保存、live原本・台帳再照合を伴うatomicルール永続化、
通常銀行replayへのルール読込。
非公開の実データ138件は44グループ、32単発、特典10／自動入金3／review31となり、
調査時の全44境界・件数・金額と一致する。

既存のbank recurringとincome writerへ承認済み用途を渡す。write直前には銀行
正本だけでなく、同じInbox原本の内容SHA256・modifiedTime・親folder・processed
状態も再確認する。保存済み収入は金融・原本・分類の全項目が一致すれば過去の
有効な根拠コードを維持し、再appendしない。金額・日付・摘要・alias・source・
hash・identity・収入分類の不一致は保留する。Receiptの照合は変更しない。
archive直前には承認済みincome／expenseの実明細との厳密照合も必須。

追加実装済み：UI候補のrefresh接続、用途確認とsettlement状況の表示、未送信入力の
保持、0件時に空欄となるホーム導線、共有受付から既存の銀行runnerへ渡す再処理経路、
既存duplicateの欠落支出明細を事前診断で保留する判定。
キャッシュした原本493取引・台帳・前回本番分類証拠のread-only検証では44グループ／
88表示行と元の全境界・件数・金額が一致し、用途確定済み未記帳収入6件、不一致0件。
現在のSecretsを使ったlive再処理や実Spreadsheetへの設置結果ではない。

追加実装済み：新しい送金元口座の明示所有確認と確認関係の再利用、非金融回答と既存
金融記帳の競合保留、processed原本との一致を伴う非金融回答行の安全な退避。

未完了：既存review statusの安全な解消、適用件数の記録、古い原本に対する実settlement authorityの確認、
本番配置・44groupのnative表示確認・本人の用途回答後の実再処理。
通常のbounded writerへの接続は書込authorityを増やさない。古い4原本に対する
21件・6件の実補完可否は既存のwindow・account・row bound等に従って別途確認する。
これらの接続・検証が済むまで、目的全体の完了や本番利用可能とは報告しない。

本番main・validated SHA・既存書込authorityはこの設計文書で変更しない。
確認済み27件に限定された過去の収入backfillを、未確定21件・未記帳利息6件の
書込authorityとして使い回さない。
