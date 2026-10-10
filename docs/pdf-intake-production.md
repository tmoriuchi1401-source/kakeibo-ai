# 新着複数ページPDFの受付境界

既存の3時間receipt runnerと実行ロックを使用する。PR #91全体は統合しない。
単ページの取込・Medical手入力・給与経路・会計writerは既存実装を維持する。
新着複数ページの定期接続は初期disabledで、mainの手動限定canary成功後に
`PDF_INTAKE_AUTOMATION_ENABLED=true`へ切り替える。既存scheduleは変更しない。

## 認証と受渡し

ActionsがFirestoreのOAuth sessionを読む構成は採用しない。既存service accountには
その権限がなく403となる。権限追加ではなく、Google署名付きmachine ID tokenを
既存Cloud Runが検証し、private Driveの確定済み証跡だけを照合する。
セッション・Cookie・Google本人token・署名keyをActionsへ渡さない。

private設定とcurrent registryは既存private folder内に置く。所有者と既存service
accountだけのACLを毎回検証する。GitHub Variableには設定ファイルの参照IDだけ置く。
registryはstrong ETagのIf-Match更新とexact read-backを必須とし、無条件fallbackはない。

送信許可はsource SHA256・page count/number・stable page identity・privacy観測・review
revisionへ固定する。記帳許可はさらにReceipt Unit・segmentation・item identity・入力
snapshot digest・plan digestへ固定する。両許可は別purposeでMAC検証する。
本人actorは既存OIDCのissuer/sub allowlistから導出する。セル値はrequest入力である。

## ページ別処理

全ページを先にローカル観測し、normalだけ自動解析対象とする。unknownは認証付きの
明示送信許可を待つ。Medical/payroll/明確PIIは一般AI経路へ入らない。原本PDFをGeminiへ
送る経路はない。送信直前の原本hashとauthority照合後、対象pageだけfresh RGB PNG化する。
独立2回読取とstable位置manifestを用い、Unit数/identityが不安定なら要確認にする。

商品のカテゴリは既存マスタを使う。信頼できる値だけ事前入力し、不足は空欄。
既存PDFページ確認へ縦カードを追加し、本人入力を再解析で上書きしない。
一般の記帳許可は既存Google共通セッションv2で明示確定する。A144/logoutを維持する。
normalの未表示・完全検証済み候補には不要な本人認証を追加しない。
Medicalは外部AIを使わず手入力待ちに保持する。Medicalを一般送信許可へ変更できない。

## 件数・再実行・保存

1回最大3ファイル・3Receipt Unitの新規write、1PDF最大50ページ/50MiB。
上限で後回しになった正常候補は次回へ引継ぎ、手入力へ強制変更しない。
曖昧なduplicate、明細差、本人許可欠落は該当ページ/Unitだけ保留する。
原本hash/page count/registry構造不一致はその共通sourceのGateである。

既存writerのmaterializerからplanを2回生成し完全一致を確認する。durable intentを
CAS保存してから、固定planの3表appendだけを許す。途中例外後はread-backのみ。
not-written/partial/unknownを自動retryしない。全行/数式/既存ledgerの完全一致でのみ
lost-responseのterminal回復を許し、追加appendは行わない。

全Page/Unit terminalと会計read-backが揃うまで親PDF移動を禁止する。移動前に
既存year-partitioned permanent repositoryへ軽量eventとsource summaryを保存する。
移動後もfile ID/hashを維持し、親フォルダのread-back一致後だけcurrent metadataをcompactする。
履歴には画像・PDF・Gemini全文・sessionを複製しない。原本は1コピー。
current registryは4MiB/500active pagesでfail-closed。認証requestは既存600秒TTL。
cleanupは有効化しない。過去canaryのauthority/history/入力値/ledgerを移行・変更しない。
