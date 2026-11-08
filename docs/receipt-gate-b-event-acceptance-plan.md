# PR #200：Gate B iPhoneセル・イベント受入計画（適用前）

**最新：[実施記録](receipt-gate-b-event-acceptance-results.md)。専用project/triggerの手動設定報告、iPhone商品修正・チェック1回・入力保持まで確認。独立読取のイベントログ0件でUUID/oldValueは未証明、Gate B/Cと本番接続はNO-GO。** 以下は適用前の承認計画として保持する。既存の[通常セル案](receipt-cas-iphone-revision.md)と「確認」1可視タブ・1行1案件を維持する。

## 1. 変更対象と適用範囲

対象BOOKはGate Aで独立確認した**作成済み合成ブック1冊だけ**。実book ID・sheet ID・ownerは非公開manifestの値を固定allowlistに使う。既存PNGと検証専用JSON（production=false / enabled=false / status=unconfigured）、旧CAS probeには書き込まない。

適用前にBOOK metadata/ACLと対象範囲を再読取し、既存入力があれば初期値で上書きせず停止する。空の初期BOOKに対する予定差分は次のとおり。

| 位置 | 予定する設定 |
|---|---|
| 「確認」A1:AT3 | 見出し＋合成一般案件1行＋sort用の登録不可な合成退避行1行。実案件・追加原本は使わない |
| A:I | 案件ID、種類、原本リンク、支払総額、修正後合計、差額、イベント試験状態、メモ、専用登録チェック |
| J:R / S:AA / AB:AJ | 商品1 / 商品2 / 空き追加枠。各9セル：名称、数量、行金額、大分類、小分類、商品/外税/値引き、帰属、採用/除外、原本照合 |
| AK:AT | 非表示の機械用case/source/item/試験UUID binding。利用者に長いID・hashを入力させない。26列の初期グリッドを46列へ拡張する案 |
| 固定・入力規則 | A列と見出し1行を固定。既存合成カテゴリのdropdown、種別/帰属/採否の選択、原本照合checkbox。原本リンクとメモは保持 |
| 同BOOK内の `_gate_b_events` | **非表示の観測ログ1タブ**。新規Driveファイルではない。唯一の可視タブは「確認」のまま |

商品A=500、商品Bの候補=730（意図的な誤読）、支払総額=1,368を初期表示する。原本には500/868/1,368だけが印字されている。合計は採用する**行金額**を足し、数量を再乗算しない。初期1,230/差額138→商品Bを868へ修正→1,368/差額0を確認する。税・値引きの計算/カテゴリ整合をGate Bだけで検証済みとは扱わず、既存Python validatorによる独立検証はGate Cへ残す。

結果欄は「イベント観測のみ・登録未受付」と明示する。「受付済み」「検証済み・未記帳」を擬似表示しない。Gate C用の終端結果cacheやPRIVATE初期化はこの適用差分に含めない。

## 2. Apps Script project・trigger・権限

- **専用bound project 1件**をBOOKに作る案。予定名 `KakeiboAI_PR200_GateB_Event_20261108`。既存category/manual-entryのproject・triggerへコードを混ぜず、既存ownerのGoogleログインで操作する。projectは追加資源なので、この計画とは別の作成承認対象。
- handler案は `receiptGateBObserveEdit`。BOOK・「確認」のsheet ID・登録列I・単一セル形状を照合する**installable onEdit 1件だけ**。作成はownerが専用projectのtrigger画面から行う。time-driven/onOpen/onChange triggerは作らない。発行されたproject ID・trigger UID・実行ownerを非公開manifestへ記録して照合する。
- bound BOOK内の編集/ログ保存用の最小scope候補は `https://www.googleapis.com/auth/spreadsheets.currentonly`。セル操作にはこのscopeが公式に列挙されているが、実際の全使用メソッドとmanifestを適用前に照合する。[Rangeの認可仕様](https://developers.google.com/apps-script/reference/spreadsheet/range#setDataValidation(DataValidation))
- triggerは手動設定/削除とし、`ScriptApp.newTrigger/getProjectTriggers/deleteTrigger`を使うinstallerは追加しない。この案ではprogrammatic trigger管理用scopeを要求するコードを入れない。Drive、外部HTTP、Gmail、SA秘密鍵、PAT、GitHub Secretsは使用しない。
- **新projectの認可は未確認**。既存ownerのBOOK編集ACLと、別projectのmanifestにあるscopeだけでは新projectの認可済みを証明できない。初回認可が求められる場合はscope画面を提示して停止し、承認なしに進めない。scope拡張を既存許可扱いにしない。[Apps Script認可](https://developers.google.com/apps-script/concepts/scopes)
- trigger実行者は作成ownerであり、実編集者の本人確認にはならない。`e.user`・effective user・メールを承認証跡やログに使用しない。[installable triggerの実行主体](https://developers.google.com/apps-script/guides/triggers/installable#restrictions)

## 3. イベントと安全な保存先

観測するのは人がiPhone Sheetsアプリで操作した登録セルの `event.value` / `oldValue`。試験用UUIDとcaseの固定対応は専用projectの合成fixtureと機械欄に用意し、`register:<UUID>` / `ready:<同UUID>`をcheckboxに機械設定する。表示は通常のチェックだけ。通常TRUE、欠落oldValue、複数セル、未知/別UUIDなら判定を停止する。[単一セルedit eventの値](https://developers.google.com/apps-script/guides/triggers/events#edit)

イベント行の現在値を読んでタップ時の案件を推測しない。UUIDから試験fixtureのcaseに対応させ、行番号は診断情報に限定する。コピー・部分sort等で対応が不明なら観測判定を停止する。このfixtureはGate Bのイベント試験用であり、private ticket・不変snapshot・CAS受付の代わりにはならない。入力の新鮮性・replay/CAS・backend受付完了はGate Cの未達条件として残す。

ログは同じ合成BOOK内の非表示 `_gate_b_events` にだけ記録する。項目はUTC時刻、試験run ID、trigger UID、sheet/range形状、許可されたcheckbox値/oldValue、試験UUID/case、観測判定・固定理由コード。登録列以外の自由入力値・メモ・画像・JSON全文・メール・認証情報を採取せず、公開CIやconsoleへ出さない。ログは編集可能な観測記録で、本人承認・確定journalとして利用しない。handlerは入力・原本リンク・メモ・結果案件行を書き戻さない。

同イベントの重複は観測として区別し、登録・検証・dispatchを一切発生させない。ログ保存が不明/欠落なら試験結果はUNKNOWNで停止し、自動再送しない。APIやscriptのセル更新は人のonEditの代用にしない。[triggerの制約](https://developers.google.com/apps-script/guides/triggers/installable#restrictions)

## 4. iPhone操作と受入記録

1. 別途承認された設定完了後に、利用者がiPhone Sheetsアプリで対象BOOKの「確認」を開く。OS/アプリ版と試験run IDを記録する。
2. 原本リンクで500/868/1,368を確認して戻り、商品Bの金額730を868へ修正。合計1,368・差額0、メモと入力保持を確認する。名称/数量/カテゴリ/採否/追加枠も同じ通常セルで操作できることを確認する。
3. 当該試験UUIDの専用チェックを**1回**タップする。ownerが非表示ログを確認し、value/oldValueが同じUUIDに対応するか利用者の操作と照合する。登録受付や終端検証はまだ行わない。
4. 個別runで、タップ直後の全行sort、行挿入/削除、通常TRUE、欠落oldValue、複数セル、別UUID、二重タップ、後の下書き/メモ編集、通信不明を確認する。人の実機で発生していない境界はオフライン補助試験と明記し、実機成功に数えない。
5. 同案件の観測または安全停止を記録し、別案件への登録/結果書戻し0、入力・メモ保持、再送0を確認する。欠落eventや対応不明を成功扱いしない。PC/Nodeの成功をiPhone実機成功へ読み替えない。

## 5. 停止・無効化と次の承認単位

対象ID/ACL不一致、新しい共有・認証・scope追加、予定外project/trigger、event identity欠落/競合、ログUNKNOWN、別案件への書込み、対象外API呼出しが必要になれば直前で停止。GitHub Actionsへのdispatch・金融writer接続・PNG/JSON更新・CAS再試験・Cloud Run操作は行わない。

将来設定された専用triggerを止める場合は、ownerが専用project画面で**非公開manifestに記録した新規trigger UID 1件だけを削除**する。全trigger削除や既存定期処理停止はしない。その後、対象BOOKの試験チェックだけを無効表示とし、下書き・メモ・観測ログ・Drive資源は保持する。実行済み観測の取消しやUNKNOWNの自動再試行は行わない。

**次の承認対象はこのGate B試験セット1件**：作成済みBOOKの上記セル/非表示ログ設定、専用bound project 1件とinstallable onEdit 1件、記載した権限差分の確認、利用者のiPhoneイベント観測まで。新project初回認可など未確認の権限を包括承認と解釈せず、適用時の実要求が異なれば再提示する。Gate C・本番接続は承認対象に含めない。現時点ではすべて未設定・未実行。
