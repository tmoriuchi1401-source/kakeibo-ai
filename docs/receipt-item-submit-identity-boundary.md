# iPhone「確認」からの商品別修正登録：接続前の本人識別監査

2026-11-07。**本人識別と要求への結合が未解決のため、受付接続は停止。完了条件は未達。**

PR #195（仕様、`56aa3bb683f94d74785b996c0b458a8664169069`）と #196（商品別修正の合成検証、`93e0fe7330e4d4aaa27144f92148f2503f159127`）を確認した。両PRはDraft・未merge。AGENTS.md、[iPhone確定仕様](iphone-receipt-review-mvp-spec.md)全文、[商品別修正仕様](receipt-mismatch-itemwise-posting-spec.md)、[前段の検証記録](receipt-mismatch-itemwise-validation.md)を前提とする。

本調査は #196 のheadから分岐した独立Draft。差分はこの文書とオフラインの監査テストのみ。#195/#196、main、本番設定・Sheets・原本、銀行等の決済処理、authority、validated SHAは変更しない。金融write、実Actions dispatch、AI送信、新規Drive／Cloud資源、トリガー設置、Webアプリ／既存ホストのdeployは実施しない。

## 1. 調べた既存経路

| 経路 | 再利用できる機構 | そのままでは不足する証拠 |
|---|---|---|
| `apps-script/category-submit/Code.gs` | B1の明示チェック、script lock、UUID、入力JSON＋SHA256を先に保存、UUIDだけActions送信、busy中の二重送信防止、応答不明時の送信停止、同じ画面の結果表示 | `categorySubmitEdited(e)` は `e.user` を渡さず `submitCategoryInput()` を呼ぶ。受付メタデータに操作者の検証証拠がない。User PropertiesのPATはトリガー設置者の接続資格であり編集本人の証拠ではない |
| `app/category_sheet_requests.py` | snapshot検査、受付ID照合、runningへのclaim、固定IDで結果をmerge、受付後の下書き保持、エラー時の自動再試行禁止 | `SheetRequestStore` は同じ編集可能なSpreadsheetの非表示シート。UUID／hashは本人認証や、独立したprivate journalのサーバー側CASの代わりにならない。現在の `execute_request` はカテゴリ・銀行処理も起動できるため商品修正へ直接転用しない |
| `apps-script/manual-entry/Code.gs` | UUID、入力snapshotの先行保存、再送抑止、UUIDだけActions送信、`manualStatus` で要求IDごとの結果取得 | 認証境界は「実行者＝自分／アクセス＝自分のみ」のWebアプリdeployment設定に依存。ソース内の `manualSubmit` は操作本人の証拠を受付に保存しない。この配置の実態は今回確認していない。「確認」タブの編集イベントが同じ認証済み操作だとは推定しない |
| `app/manual_entry.py` | UUIDから固定支出ID、claim、重複確認、結果保存、read-back | 独立した一般レシート修正検証workerではなく、手入力支出を本番writerへ送る。呼び出せば金融writeを行い得るため今回のno-write接続には使わない |
| `auth-session/` の既存ログイン | Google署名／issuer・audience・sub／期限／nonce・PKCE、owner allowlist、cookie・Origin・CSRF、要求UUIDとsnapshot結合、session失効・CAS | ブラウザ上の明示POST用。Sheetsの編集トリガーからブラウザcookie・CSRF付き操作を受け取る実装がない。機械から既存hostへ接続できることは編集本人の証明ではない |
| `auth-session/generic_review.py` / `app/receipt_plan/readers.py` / `app/pdf_intake_registry.py` | verified actor＋request digest、private journalの版照合、強いETag／If-Match、独立read-back、未知結果時の停止 | 既存のregistered page/Unit・商品数固定の確認契約。汎用の不一致修正要求へ無条件に転用できない。特に `RegisteredJournal.replace` はposting authorityをsealするため、今回の検証だけの目的で呼ばない |
| `.github/workflows/category-sheet-request.yml` / `manual-entry.yml` | UUIDだけのdispatch、既存concurrency、main／validated SHA等の安全条件 | どちらも本番入口。新しいno-write worker用の入口ではない。PAT所有者やGitHub actorはSheetsを編集した本人の代わりにならない |

非表示シートやセル保護を安全境界と扱わない。既存の各経路が従来の用途で不正利用されているという判定ではなく、今回求められた「編集した本人・原本・修正内容を安全に結ぶ」契約を、そのまま満たせないという調査結果である。

## 2. Google側の仕様と停止理由

- installable triggerは設置者のアカウントで実行される。編集者と設置者が同じとは限らない。[Installable Triggers](https://developers.google.com/apps-script/guides/triggers/installable#restrictions)
- 編集イベントの `user` は利用可能な場合に限られる。欠落時にownerへ置き換えない。[Event Objects: Edit](https://developers.google.com/apps-script/guides/triggers/events#edit)
- `Session.getEffectiveUser()` はトリガー設置者を返す。これを編集本人として記録しない。[Session](https://developers.google.com/apps-script/reference/base/session#getEffectiveUser())
- `ScriptApp.getIdentityToken()` もeffective user用であり、`openid` scopeを要する。既存両manifestに同scopeはなく、scopeを足すだけでも編集本人との一致問題は解決しない。[ScriptApp](https://developers.google.com/apps-script/reference/script/script-app#getIdentityToken())

したがって、チェックボックスに既存PAT dispatchをつなぐだけの案は**採用しない**。自己申告メール、triggerUid、UUID、hash、GitHub actor、機械ID token、hidden queue、test stubを人間の署名済み操作証拠として代用しない。

`e.user` が存在する実行だけを使う可能性まで否定するものではない。ただし、iPhone／対象Googleアカウントでの可用性、本人の安定したidentityへの結合、queue改変から保護された証拠の保存、バックエンド側の独立検証方法が未確定である。単にメール文字列をqueueへ追加する実装は作らない。

既存ブラウザログインの署名・session・CSRF/CASを保ったままSheets操作へ結合する方法も、現行の1回登録操作のままで成立するか未確認。別Web画面への変更、追加認証方式、scope／secret／host変更を推測で選ばない。ユーザーの「認証・権限・接続方法に未解決の問題があれば、その部分で停止する」に従い、この境界で接続実装を止めた。

## 3. 最小の接続候補と、今回選定しなかった部分

本人識別が解決した後の最小候補は、採用済み「確認」の案件内で「修正内容を登録する」を一度操作し、既存の先行snapshot保存→UUIDだけの起動→要求IDによる結果反映パターンを使うこと。別タブ・新規サーバーは追加しない。商品別検証は #196 の `review` / `registration_plan` / `preview_item_corrections` を再利用する。

しかし、**本人証拠の入口が未確定なので、採用可能なend-to-end構成の選定には至っていない**。仮の肯定経路や「認証済み=True」の代替backendは実装しない。新規商品編集UI、hidden queue、トリガー、workflow、worker、結果反映処理は追加していない。採用済みの1タブ、横スクロール、ID列・見出し固定、リンク・入力・メモ保持、G103／H301も変更していない。

## 4. 実施した検証

`research/receipt-submit/identity-audit.test.cjs` は、既存のfake Sheets／UrlFetchハーネスをそのまま利用する**現状再現テスト**である。新しい認証の成功試験ではない。成功は不足が再現したという意味であり、将来本人証拠を導入したら期待値を再監査する。

| 再現事項 | 結果 |
|---|---|
| categoryの編集eventにuserがなくても既存fake dispatchが呼ばれる | 再現 |
| owner／別editorのeventで同じUUID入力・snapshotになる（操作者が保存されない） | 再現 |
| Session情報を読めない状況でもcategoryは設置者の設定で起動する | 再現 |
| manualの入力に自己申告actorを付けてもpayloadに本人証拠として保存されない | 再現。ただしdeploymentアクセス制御の試験ではない |
| 既存manifestにopenid scopeがない | 確認 |

実行：`node --test research/receipt-submit/identity-audit.test.cjs apps-script/category-submit/Code.test.cjs apps-script/manual-entry/Code.test.cjs` → **12成功・0失敗**（新規監査5、既存7）。すべて通信・Sheetsはfake。実Actions起動・金融write・原本移動・AI送信0。

関連Python：`python -m pytest tests/test_category_sheet_requests.py tests/test_manual_entry.py tests/test_receipt_item_correction_review.py tests/test_receipt_item_correction_plan.py tests/test_shared_session_bundle.py -q` → **82成功・0失敗**。これは従来機能・PR #196・既存auth bundleの検証であり、iPhoneからの本人識別付き登録成功ではない。TEMP/TMPとpytestの一時領域はrepository外の許可済み合成領域を利用した。

既存auth/session契約：`python auth-session/compose.py --test` → **363成功・0失敗**、`live_write: 0`。署名／session・Origin／CSRF・要求混同・失効・CAS競合・replay等の既存契約を含む。初回はFlask関連とFirestore依存が不足しcollection error 4件で中断したため、既存CIと同じ `Flask==3.1.3`／`google-cloud-firestore==2.34.0` を許可済みのテスト専用ディレクトリへ分離インストールし、再実行した。グローバル環境・本番依存・requirementsは変更していない。

今回の差分は実行経路に影響しない文書・監査テストのみなので、本番workflowや全体CI設定は変更しない。独立Draftは #196 ブランチをbaseにするため、main対象の既存PR CIはこの追加差分には自動起動しない。上記はローカル合成実行の結果であり、新DraftのCI成功とは表現しない。監査JSテストは意図的に既存の通常テストへ組み込まず、上記の明示コマンドで実行する。

## 5. 再開に必要な最小作業

1. **最初に本人証拠の結合方法を確定する。** 対象iPhone／Googleアカウントの編集本人を安定して識別し、本人・明示操作・UUID・期限・用途・案件ID・原本ID/bytes hash/version・修正snapshot digestを、独立検証可能な証拠に結合する。既存OIDC/sessionを再利用する場合もcookie／Origin／CSRFを緩めない。ここを解決せず次項へ進まない。
2. 証拠が成立したら、#196を再利用する**検証専用**workerを接続する。既存private journal/CASを検証だけのpurposeとして利用し、claim前後のsource／input新鮮性を再確認。同一UUID・変更再送・同時要求・UNKNOWNでは追加実行しない。canary posting grant、医療／PDF用authority、銀行workerを流用しない。
3. 「確認」の同じ案件へ受付／検証／修正必要／検証済み・未記帳／UNKNOWNを返す。行番号で戻さず、案件・原本版・要求UUIDで再照合し、受付後の編集を上書きしない。並替え・追加削除・再生成・応答喪失を合成試験し、最後に実Google認証とiPhone実機で検証する。
4. その後の金融write接続は別段階。既存authority・本番停止条件・重複／read-backを独立検証し、NO-GOを解除せずreview可能な変更として提示する。通常運用に限定canaryの取引ごとの手動承認を転用しない。

本稿の結論は「新規インフラが必須」でも「Sheetsでは実現不能」でもない。**既存の方式を無変更で流用するだけでは本人証拠が不足し、許可条件内で安全な接続方法を今回確定できなかった**という停止記録である。実機・実OAuth・private journalへの実要求保存・同画面への実結果反映・本番接続は未検証／未実施。
