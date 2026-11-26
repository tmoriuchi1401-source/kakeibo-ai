# 限定実記帳canary：本番照合と停止境界

2026-11-26。今回のowner指示による後続記録。
基準はPR #208 `49fd6a7d2a11789ea6ce794d4d1178b527f4a6aa`。
A01～A15本文は不変。隔離E2Eの合格は再利用し、再dispatchしていない。
**原本保護のmain適用は完了。実金融canaryは未実施・NO-GO。定期開始は未承認。**

## 原本保護の適用

[PR #209](https://github.com/tmoriuchi1401-source/kakeibo-ai/pull/209)の
head `3eedf62ebd7417bbf8b79e4f75b2f6d61ddef5c2`、最新main/base
`fa7ac9378ef51e4a51258dd6053afe52ed0a2bb8`、既存CI45748610528の全3job成功、
6ファイル差分を確認した。変更にworkflow、schedule、金融writer、銀行処理は含まれない。
直前のActions実行中・待機中0、親bank/receipts ready、カードpending保持を確認してmerge。

実mainは `70a6181fb7658a8a8d1885fa5d7ab9eb8d74d9b1`。
tree `6cc77f7e53c14a6ef56fd77394b67467648791b3` はPR209 headと一致。
main pushの[CI45806072759](https://github.com/tmoriuchi1401-source/kakeibo-ai/actions/runs/45806072759)
は同じ実SHAで全3job成功。その後main/SHA/CI/稼働不存在を再GETし、
`KAKEIBO_VALIDATED_MAIN_SHA` 1変数だけを実mainへ昇格、独立GETで一致を確認した。
他の全Variableは昇格前後で完全一致した。

銀行schedule=true、global/core/PayPay=false、receipt専用schedule未設定を保持。
次の銀行cronより前に昇格完了。SHA不一致の間に銀行runは観測されていない。
実cleanup、原本移動・削除、金融dispatchは0。保持側へ閉じる既存monthly設定を維持する。

PR208へこのmainをmergeし、競合したretention試験はmainの追加CLI試験を含めて保持した。
重複した同一httpx pinだけを整理。app、workflow、Apps Scriptは基準208と同一。
必要差分のretention/maintenance/CLI試験18件とwhitespace検査が合格した。
PR208はDraft、main未merge・本番未配備のまま。

## カードUNKNOWNと一般レシートの独立性

既存[カード調査](receipt-live-connection-status.md)を再利用する。
native generation121/pending、payload SHA256
`c5a1b95eb9dd62d3ba5950adac63967902061355d7f9f6b0cfcd86dfd46b7fe7` は以前と同一。
親generation1414のcardもpending・attemptあり。bank/receiptsほかはready。
今回の読取り中も親/nativeは不変。新しい復旧証拠は得られていない。

| 検査対象 | 判定 |
|---|---|
| 排他 | 既存hosted Actionsの共通kakeibo-productionとScriptLockを再利用可能。Drive read/write/readをCASとは扱わない |
| 通常drive_receipts | verify_source_readinessが全parent/native readyを要求。カードpendingで停止。guardを迂回しない |
| 手動receipts | source自体は分離できる構造だが、成功後needs_ledger_order→finish_ledger_updatesが共有台帳の並べ替えを呼ぶ。今回の既存行不変・限定影響範囲の証拠がない |
| 重複 | native check_duplicatesはreceipt/importの同一原本IDとreceipt/expenseの日付・金額を確認。未確定カードcandidate全体とのcanonical照合を代替しない |
| native writer | exact-prefix appendと旧値/数式のreadbackは既存実証を再利用できる。対象の親attempt・plan・snapshot、実行経路と共有後処理の固定は未成立 |
| PR193 | PayPay単独取込だけの既存分離。receipt許可、共有会計後処理の安全証明として流用不可 |

**安全な分離は未成立。金融canaryだけ停止する。** カードUNKNOWN・恒久フェンス、
既存guard、履歴・checkpoint・pendingを変更しない。新しい分離機構やwriter改修は追加していない。

## 本番接続の読取りと配備準備

既存本番owner OAuthを既存7scopeのままrefreshし、Google本人・既存client/project一致を確認。
試験OAuthは使用しない。新資格、権限追加、資格保存・失効は0。
RSA bindingを既存SA keyで読取り照合し、state folder、親、カードnativeの固定ID一致を確認した。
金融bookのownerと既存SA writer ACLも一致。bank native generation110/readyは独立2読取で一致。

本番ScriptのHEADとversion5本文を私的workspaceへ保存、既存記録の各file hashと一致。
ブラウザの配備管理で既存deploymentの**version5 / API executable / 自分のみ**を確認。
過去4配備はarchive済み。既存編集triggerはowner本人、version5、receiptReviewEdited 1件。
読取RPCはHTTP200/done、backend=false、registration=true、STOP=false、金融write0。
旧UUID/requestを変更していない。

既存scopeによるdeployments API一覧は403。scope拡張しない。
ブラウザには未保存の編集が表示されているため、保存・破棄・配備をしていない。
Script Properties全体の安全な退避、trigger source IDの最終binding照合は未了。
旧StartPreflightはGoogleに残るPATを送信し得るため呼び出していない。

現在のRepository Variable一覧ではRECEIPT_CONFIRMATION_BINDINGと
RECEIPT_HOSTED_REVIEW_BINDINGがともに未設定。owner Secretは名前の存在だけを確認。
旧receipt stateのledger/ownerは一致するが、稼働bindingの完全照合とは扱わない。
receipt-hosted namespaceも未作成。旧manifest/解析10件/確認履歴94件を初期化しない。

ローカル配備候補はPR208既存部品の再利用だけ：Sheets、Review、ReviewSetupを更新しHostedを追加。
manifest、Code、TaxCorrection、PolicyRecheck、StartPreflightは同一。
54関数の衝突なし、Node構文検査合格。新しい保守関数・trigger・UIを追加しない。
配備用payloadと各hashは私的workspaceだけに保存した。**配備準備は未完了**である。

rollbackは同じdeploymentを既存version5へ戻し、必要なら退避HEAD本文へ戻す範囲とする。
先にProperties/trigger/履歴/未保存編集を保護して差分を確定する。
金融write開始後はrollbackや行削除で補償せず、UNKNOWNを保持して独立readbackだけを行う。
state/UUID/履歴/STOPを過去snapshotで上書きしない。今回rollback実施0。

## 原本候補と固定計画

既存processed168件とinbox38件のmetadataを読み取り、既存state/金融IDと照合。
通常画像の新規metadata候補0、inboxに未所有PDFのmetadata候補29件。
この29件すべてが一般・単ページ・未記帳だとは判定していない。
最新2件だけの原本hash/version/親folder/編集権限を固定し、metadata再読取で不変を確認した。
ページ数はそれぞれ4/5で、両方とも対象外。残る27件は未選定・未解析。
原本bytesや医療内容を外部AI・PR・CI成果物へ送っていない。

**A/B選定0、実AI送信0、実金融write0。** 一般と確定した対象、期待明細・合計・カテゴリ、
writer権限・親attempt・使用SHAを束縛できておらず、承認に出せる固定記帳planはまだない。
正常の確認シート登録0と例外登録1回の実金融受入はともに未検証。
架空の例外を作らず、旧UNKNOWNや記帳済み原本を流用しない。

次は有効な新規一般原本の指定、共有金融への安全な分離と本番bindingの確定。
それらを満たして固定planを提示した後に本人の対象確認を受ける。
PR208のmain merge・本番配備・実金融writeはその承認前に進めない。
現金/カテゴリPAT、買取票収入、認証付きPDF例外は別単位、定期運用開始も別承認のまま。
