# 管理Spreadsheetの銀行確認

## 統合先

「カテゴリ操作」の既存3ブロックの後に、任意の第4ブロック
`■ 4. 銀行取引をまとめて確認` を置く。既存3ブロックだけを含む過去の受付も有効。
カテゴリの大／小カテゴリと、銀行の経済的用途は別のルール正本を使う。

表示はA～F。各グループの主行は条件・代表原本リンク／件数・金額・期間／用途／
収入分類／今後の自動判定／反映checkbox。下の補助行のC・Dで、自己口座間振替の
場合だけ相手銀行・口座aliasを入力する。口座番号は入力しない。
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

銀行用途の実行器が未接続の段階では、チェックされた銀行回答を受付実行の成功
として扱わない。実行器の接続前に本番UIを有効化しない。

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
銀行専用ルールschema、JSON競合評価、idempotent登録／revision復活の計画。
非公開の実データ138件は44グループ、32単発、特典10／自動入金3／review31となり、
調査時の全44境界・件数・金額と一致する。

未接続：受付からの銀行回答実行、今回のみの意味保存、live原本・台帳再照合を
伴うルール永続化、通常銀行replayへのルール読込、未記帳収入のbounded settlement、
archive直前read-back、ホーム導線、本番配置と表示確認。
これらの接続・検証が済むまで、目的全体の完了や本番利用可能とは報告しない。

本番main・validated SHA・既存書込authorityはこの設計文書で変更しない。
確認済み27件に限定された過去の収入backfillを、未確定21件・未記帳利息6件の
書込authorityとして使い回さない。
