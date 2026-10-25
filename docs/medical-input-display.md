# 医療手入力：不足項目の結果表示

PR #159固定head `8aa665f1903df3548a6efe9fb34f91c53a7474a9` からの独立変更。
正式「確認」1タブの列・入力欄・Driveリンク・入力規則は変更しない。

## 変更

既存validatorで医療入力が拒否され、既存CASで部分入力の保存とfresh読戻しが終わった後だけ、
そのエラーと同じsnapshotを案内へ変換する。判定条件・status・受付・native計画・writerは変えない。
`draft_v2_text/integer/complete_manual_only` の既存エラーに対し、既存必須欄の空欄を項目名で補足する。
日付・施設名・実支払額・カテゴリが対象で、支払方法とメモは任意のまま。
値の正しさを判定する新validator、推測入力、AI/OCR、自動補完は追加しない。
説明できない既存エラーや非空の不正値は「入力不足または入力不正」「入力条件未成立」とし、空欄と断定しない。

| 状態 | 表示と次の行動 |
|---|---|
| 入力不足の保留 | 例：未入力：実支払額。部分入力保存済み。次番号は既存sequenceから案内 |
| 競合 | 入力は未採用。再登録せず案件・原本・保存結果を照合 |
| UNKNOWN | 既存の結果不明・照合待ち・自動再送禁止を保持 |
| 受付済み原本の再依頼 | 受付済み・未記帳。再登録せず保存結果を照会 |
| 登録検証済み | 登録検証済み・未記帳。金融接続なし |

新しい医療結果には「自動処理は停止中。入力修正だけでは登録検証・記帳されません」と表示する。
これは現在の合成試験構成の静的説明であり、runner監視や稼働検知を追加したものではない。
入力不足だけに、修正・同期後の有限試験セッションで使う次番号を案内する。
競合・UNKNOWN・受付済みには新番号登録を促さない。結果の表示値は許可や同期完了の証拠にしない。

新文言は新しくfinishする医療receiptのreasonにだけ保存する。既存schemaを増やさず、
既存receipt・配信済み結果・SENDING/UNKNOWNを移行・再送しない。
既存claim、formula guard、fresh照合、exact読戻しを維持する。

## 検証と範囲

差分試験は新旧の表示処理を同じ完全合成fixtureに適用し、状態・受付内容（reason以外）・下書きwrite回数・入力保持の一致を比較する。
空欄1件/複数、任意欄、入力不正、競合、UNKNOWN、受付済み、旧結果の再起動保持と実Google境界の結果式を対象とする。
PC/390pxは実際の結果数式を独立評価したセル表示fixtureであり、Google Sheets/iPhone実機の新規合格とはしない。
使う入力・画像・診断はすべて既存の完全合成データ。実医療情報を取得・ログ・GitHubへ送信しない。

既存の専用CIはbaseブランチを限定しているため、この独立PR用に同じ隔離構成のPR自動CIを1件追加する。
既存workflowは変更しない。新CIはcontents:read・write flag false、Google credentialなし、ブラウザーはloopback-only netns。
schedule、workflow_dispatch、runner有効化は追加しない。

Windowsの一時領域権限と、Git管理下の一時領域で発生する既存bundleの `bundle_result_changed` は、
Git管理外の作業用一時領域を指定して解消した。hash条件・fixtureは緩和していない。
ローカルとUbuntu CIで合成検証し、最終の統合・表示結果はPR説明に記録する。
最終CI件数と受入結果はPR説明に記録する。

#159の入力・原本往復・再起動後保持の既存iPhone合格は採用し、再試験しない。
合成医療M101はテスト内の使い捨てfixtureだけで、実受入ブックの受付済み案件には接続しない。

本番Sheets、main、既存PR、A0-G、認証/Secrets/IAM/Variables/validated SHAは変更しない。
金融write・authority・原本移動・AI送信は0。本番4処理・R1・無人runnerはNO-GOを維持する。
実Googleへの適用・新表示だけの実機確認は別承認。
