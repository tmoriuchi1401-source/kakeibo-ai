# 限定実記帳canary：再開準備の差分

2026-11-27追補：[カードalias最小修正・2行修復・新checkpointとreceipt残条件](card-alias-reconciliation-20261127.md)。
Gmail復旧/原本照合/隔離E2E/PR209は完了済みとして再利用する。以下は前段の記録。

2026-11-26。PR208基準 `81a25f859f29e9ba407408261363d4ac15f970ac`、
main/validated `70a6181fb7658a8a8d1885fa5d7ab9eb8d74d9b1`。
この文書は[前回の停止記録](receipt-production-canary-status.md)の後続である。
A01～A15本文・本番schedule・本番設定・Script・原本・金融表は変更しない。
**再開準備は前進したが、実金融canaryはNO-GO。固定記帳planも未作成。**

## A：カード復旧に不足する証拠

対象は失敗run41825241169/attempt1のみ。既存のnative121 pending、
payload SHA256 `c5a1b95eb9dd62d3ba5950adac63967902061355d7f9f6b0cfcd86dfd46b7fe7`、
checkpoint `2026-11-03T14:45:43+09:00`、親card pendingを固定した。
旧counts、現在行に失敗run以降の取込時刻がないこと、旧sealed journalは再調査しない。
これらだけでは未送信・完全反映・部分反映を分類できない。

不足していた「hostedに保存された既存Gmail資格が利用できるか」だけを、
既存state-cache-inspect workflowの読み取り専用operationで確認した。

| run | SHA | 結果 |
|---|---|---|
| [45812853362](https://github.com/tmoriuchi1401-source/kakeibo-ai/actions/runs/45812853362) | 0f7777f | 既存Gmail資格利用不能。認証失敗以降は収集・照合を実行していない |
| [45813453614](https://github.com/tmoriuchi1401-source/kakeibo-ai/actions/runs/45813453614) | d3c68cb | 固定診断 `gmail_token_status: refresh_failed`。JSON/scope不一致とは分類されていない |

後者は固定エラーを安全な分類だけに細分化した追加診断であり、失敗カードrunのrerunではない。
両方attempt1、state/financial write=0、AI=0。refresh失敗の具体的原因は非公開のまま、
失効・期限・同意状態などのどれかを推測しない。ローカル2資格だけでなくhosted Secretも現在利用不能と判明した。

新operationはbranch/owner/attempt1/expected SHA/workflow SHA/hosted Linuxを固定し、
既存共通排他を使用する。既存strict Gmail readonly collectorとcanonical plannerを再利用する。
Drive/Sheetsはreadonly scopeとGET-only wrapper、Gmailはreadonlyのみ、各HTTP retry=0。
owner OAuth、Gemini、test OAuthは渡さず、原本・明細・資格・artifact・stateを保存/出力しない。
成功時にも分類はUNKNOWN固定で、解除やwriteの権限を持たない。

残る証拠は次の3点である。

1. 上記checkpointの2日overlapから失敗step終了までのGmail原本を、同じreadonly process内で
   canonical/resend・取込12列・receipt関連・money/reviewに対応付けること。
   既存connector調査の6通/24source行・14 raw alias不一致を新規候補件数とは扱わない。
2. 失敗attemptの固定plan/journal、または失敗直前の金融・money snapshot。
   nativeには前回分しかない。取得済みrevisionの403や500000-cell変換失敗を繰り返さない。
   現在の台帳から過去のwire要求回数を逆算しない。
3. その証拠に基づく未送信/完全/部分の分類、全副作用の独立読戻し。
   既存の過去run例外を今回に流用しない。今回のoperationだけではmoney/review全照合は未完了。

既存Gmail connectorの原本を別解析toolへ転送する方法は、以前の自動承認レビューで
機微データ転送として拒否されている。その方法を再試行せず、資格の復旧が必要なら以下を本人操作案とする。

- 既存の本番Gmail clientと同じownerを使い、既存 `python -m app.cli gmail-authorize <既存client JSON> <新しい非公開出力先>` の同意画面で
  `gmail.readonly` だけを確認する。新client・test OAuth・scope追加・既存grant失効は行わない。
- 新しいauthorized-user JSONは管理下のローカル非公開ファイルへ直接保存する。
  token/refresh token/client secret/認証URLをチャット・logへ貼らない。
- 現時点ではSecretもローカル資格も置換しない。本人認証と資格置換は今回の許可範囲外。
  置換を後日承認する場合は既存値を安全に退避し、ローカルだけの限定証拠収集を優先する。
  共有 `GOOGLE_GMAIL_TOKEN_JSON` の置換はカード以外のAmazon/au PAYメール取込にも影響するため別途固定する。
- Gmail再認証が成功しても、失われた失敗attempt journalや書込前snapshotが自動的に復元されるわけではない。
  pending/UNKNOWN解除は依然許可しない。

## B：source別縮退の最小差分と限界

[PR193](https://github.com/tmoriuchi1401-source/kakeibo-ai/pull/193)の
`drive_paypay -> paypay` 限定readiness・全parent再読戻し・銀行review後処理除外を再利用した。
既存writer、実行source、共通排他を使い、新writerは作らない。
selected sourceと推移的依存のparent/native ready、attempt/error空、binding/schemaを検証する。
未確定カードを含む他sourceの親要素は完全保持、親再読戻し不明時は停止する。
共有ledger_order、review_apply、reconcile、auto_expense、projectionをこの取込に含めない。

これはPayPay**取込だけ**の隔離案で、全会計処理完了や本番運用再開の承認ではない。
実PayPay dispatch、schedule変更、main昇格は行っていない。
銀行専用経路は変更しない。一般レシートをこのallowlistへ追加しない。

一般レシートの実金融writeには次の未解決点がある。

- 失敗カード候補のcanonical集合が未確定で、receipt既存duplicate検査だけでは未確定カードとの重複を排除できない。
- 手動receiptは親の `needs_ledger_order -> finish_ledger_updates` に加え、既存childの
  `ExpenseViewPipeline.refresh`、legacy confirmation、medical/PDF等の分岐を持つ。
  親の並べ替えだけを省いても、既存行・数式・共有履歴の不変を証明したことにならない。
- 従って手動 `receipts` にも既存全source readinessを適用し、カードpendingを回避する抜け道を閉じた。
  既存native receipt authorityもprepareと各append直前に他sourceのready/attempt/errorを確認する。
  途中でpendingが生じた場合は次appendを止め、UNKNOWNを維持し自動gap fillしない。

**Aの復旧証拠が成立するか、未確定カードとの重複・child共有副作用まで分離できる固定案が成立するまで、
receipt金融writeは停止する。** 今回は安全でないreceipt分離を実装していない。

## 本番bindingの固定ローカル案

旧記録・既存暗号化binding・Google実体の照合結果から、値を推測せず固定した。
具体的なIDと暗号文はローカル `.private/canary-resume/fixed-bindings-private.json` に保持し、Gitに含めない。
旧record/入力/UUID等を含む提案payloadも同じprivate directoryだけに保存する。

| 設定 | 契約と固定対象 |
|---|---|
| RECEIPT_CONFIRMATION_BINDING | `file` は既存receipt state IDを既存SA鍵/RECEIPT_REIMPORT_FILE_ID契約でwrap、`manifest` は既存digest `264893c7b964a9d70fcd842893c0241bc465aa9d86e6bbb44f27bfa226d7cd27` |
| RECEIPT_HOSTED_REVIEW_BINDING | 既存script project、version5 API deployment、owner、既存確認book、既存inboxの5項目。旧runner・保存済みtrigger source・実book owner/readbackの一致から固定 |
| receipt_hosted_v1 | 空bootstrap不可。既存13所有権をheldで参照、68 source/66 hash fenceを継承する非空の追加案。enabled=false、financial_write_enabled=falseで実行不能 |

既存root全key/manifest/解析10件/確認履歴94件はdeep equalityで不変。
旧確認bookの3case/3request、旧SQLite AttemptFence、UUID、STOP、UNKNOWNは元の所有場所を保持する。
新namespaceのrequestsは新規受付0件を表し、旧requestの削除・初期化・再採番ではない。
旧stateを過去snapshotで上書きする適用手順は作らない。提案はfresh stateへの差分としてのみ扱う。

コードの最小追加は、引き継いだsource/hash fenceをAI/review/writerより前で検査し、
runtime保存で既存fence集合の縮小/削除を拒否すること。production factoryは継承inventoryなしで接続しない。
旧隔離fixtureの6key契約は維持する。新しい本番namespaceは無効なので現状のfactoryでは停止する。
`native_apply=False` とfinancial_write_enabled=falseを今回変更しない。

旧設定が存在しない理由は、保存済み記録ではhosted production接続を有効化する段階が未承認・未実施だったため。
削除履歴や過去に有効な値が存在した証拠はない。GitHub Variable APIは変更履歴を提供しないため、
「誰かが削除した」と断定しない。

本番変更前の固定対象はこの既存stateへのnamespace**追加**と上記2 Variableのみで、
既存SA writer/既存owner OAuth・Script本人限定APIを使う。権限拡張やGoogleへのPAT転送は不要。
ただしfresh ACL/receipt payload digest/current Script Properties/trigger sourceの一致は未了であり、
このproposalはまだ適用可能な変更planではない。本番設定変更は別承認まで実施しない。

## Apps Script：不足している本人操作

既存version5と退避HEADは再利用する。OAuth、配備一覧、HEADダウンロード、trigger作成を繰り返さない。
今回もCUAで既存trigger1件（本人/version5/編集時/receiptReviewEdited）の表示を確認したが、
Project settingsへのclickは `coordinate input geometry is unavailable` で失敗した。
設定画面のPropertiesやtrigger編集画面のsource IDを未確認のまま確定済みとは扱わない。

後日の本人作業は次の順序。これは今すぐ設定変更・配備を依頼するものではない。

1. 既存Scriptの編集画面で「未保存の編集」があれば、対象fileの内容を安全なローカルへ退避する。
   保存/破棄/refresh/配備の前に既存退避HEADと比較し、差分があれば上書きしない。
2. Project settings → Script propertiesを開き、全Propertiesを秘密値が外部へ出ない保管先へ退避する。
   チャットへ返す情報は `RECEIPT_REVIEW_CONFIG_JSON` の非秘密bindingが提案と一致するか、
   registration flag、STOPの有無・状態だけ。PAT/token/client secretを貼らない。
   不明なら値を転記せず「未確認」とする。プロパティの追加・保存・削除はしない。
3. Triggersで既存receiptReviewEditedの編集画面を開き、source spreadsheetが固定確認bookと同一か確認する。
   保存せずcancel。trigger数・owner・version・handler・source IDを固定記録する。
4. 既存API executable deploymentがversion5/自分のみのままか確認する。
   新version作成、権限変更、trigger再作成は行わない。

旧 `receiptReviewStartPreflight` はGoogle側PATを送信し得るため実行しない。
Prepare/Configure/Hosted開始/registration有効化も今回実行しない。
将来の最小配備候補は既存Sheets/Review/ReviewSetup差分とHosted追加のみ。
rollbackは同じdeploymentをversion5へ戻し、必要なら退避HEADへ戻す範囲で、
Properties/triggerを退避比較し、state/履歴/UUID/STOPを巻き戻さない。金融writeの取り消しをrollbackに含めない。

## 原本候補と再開条件

本人指定1件目はsource/hash両方に旧AttemptFenceがあるため除外を維持する。
2件目は前回固定のversion8/hashに対応する1ページ・一般・非買取の原本を候補として保持する。
旧state/registry/AttemptFenceと一致せず、再利用できる解析結果は未発見。
今回は原本再解析/AI送信しない。実行直前にID/hash/version/owner/inbox/権限/privacyを再照合する。
正常/例外の別は実解析結果が出てから判断する。架空の不整合で例外を作らない。

再開には次が必要である。

1. Aの証拠によるカード状態の安全な扱い、またはBのreceipt重複・共有副作用分離の実証。
   UNKNOWN・フェンスを解除して再開する案は今回の範囲に含めない。
2. 本人の上記保護操作とfresh binding/ACL/stateの一致。設定対象・差分・使用権限を固定し、
   本番設定/Script/PR208 mergeの別承認を受けること。
3. その後の明示的AI許可の下で固定一般原本を各1回まで解析し、既存結果があれば再送しない。
   期待明細/件数/合計/カテゴリ/重複/親attempt/使用SHA/影響範囲を固定したnative planを提示すること。
4. 本人対象確認と限定apply承認後にのみ実金融writeへ進むこと。正常の確認登録0件、
   実在する例外はiPhone登録1回、独立読戻しと同一案件結果表示を受入条件とする。
   結果不明は再送しない。例外候補がなければ未検証のまま報告する。

今回の許可は準備・最小修正・Draft PR・隔離試験まで。実AI=0、実金融write=0、
本番設定/Script配備/main merge/原本移動削除/フェンス解除=0。定期運用の開始は別承認である。

## 差分試験

既存隔離E2E7run、owner OAuth、PR209適用、既存CIの証拠は再利用し、再実行していない。
今回の変更だけを、既存統合fixtureと実native writerで合成データとして検証した。

```
python -m pytest -q --tb=short \
  tests/test_card_pending_readonly.py tests/test_isolated_paypay_intake.py \
  tests/test_receipt_card_pending_boundary.py tests/test_receipt_legacy_fence_inventory.py \
  tests/test_production_flow.py tests/test_architecture_contract.py \
  --basetemp ../pytest-resume-final
```

**125 passed**。Gmail失敗の秘密非出力、readonly/実行境界、PayPay source/parent保持、
receipt card-pending拒否、最初のappend後にpending発生→次append停止/UNKNOWN再送禁止、
旧fenceのsource/hash検査・削除拒否・production inventory必須、A01～A15関連を確認した。
既存Google genai依存のDeprecationWarningが1件。実原本・金融データを試験へ投入していない。
最初の実行はtmpをrepo内へ置いたため既存外部DB制約でsetup停止したが、
workspace内のrepo外へ移して全件成功した。制約は緩めていない。

この変更はPR208へのstacked Draftとし、本番適用やmergeを求めない。
