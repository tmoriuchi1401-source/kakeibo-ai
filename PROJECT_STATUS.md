# KakeiboAI PROJECT STATUS

> **役割:** KakeiboAI開発の「現在地の正本」。新しいCodex / Work / Goalを開始するときは、過去チャットや古いbranchより先にこのファイルを読む。
>
> **重要:** このファイルは状況整理であり、本番write・apply・権限拡張・外部データ変更の承認ではない。

## 0. このファイルの使い方

- 大きなPhaseが終わったときだけ更新する。細かな診断ごとには更新しない。
- 各機能を **L0〜L4** で管理する。
  - **L0 未着手**: 本番用途の実装なし
  - **L1 解析可能**: 入力を安全に読み、previewできる
  - **L2 取込可能**: stable identity / dedupe付きで取込データへ登録できる
  - **L3 家計簿反映可能**: 支出・収入・除外・要確認まで到達できる
  - **L4 定期運用確認済み**: 実データで自動運用・再実行安全性を確認済み
- `A/B/C` 判定だけを書かない。「何についてAか」を必ず書く。
- `main SHA` や実行件数は **Last verified** として扱い、再開時に必要なら確認する。
- 過去の実験branchの成績で、本番機能を未完成へ戻さない。
- 個人開発方針として、完全性・網羅性・過剰な安全証明より **実用性・開発時間・保守負担・削減作業量** を優先する。重大事故を防ぐ安全境界は維持し、低頻度・低影響は人間確認を許容する。

---

## 1. Last verified

### 2026-09-16 Medical「今回入金額」への対応再開

- 本人から、固定Medicalの印字ラベルは「今回入金額」、利用中GeminiはFreeとの回答を受領。
  従来UIに該当ラベルがなかったため、同じ匿名化/応答schema/確認画面へこのラベルを追加。
  前回・累計入金額、請求額、未収額は追加対象外。金額そのものを指定・fixture化しない。
- 前処理版はv4。旧版の本人確認を流用せず、同じ原本から得た安全な領域または
  本人が最終PNGを確認した範囲だけを送信対象にする。
- Google公式Free条件を再確認。入力/応答は製品改善・人によるレビューの対象となり得るため、
  個人・機密・機微情報を含めない。本人申告をプラン確認の根拠として記録し、課金/認証は変更しない。
  実送信・候補・replayの結果は別途検証する。ラベル追加だけで本人確認済み・解析成功とは扱わない。
- ラベル対応SHA `5ddbc0a7b7cefeaf1d8bfa7e4f4f0bbb11a209f6` のLinux CI `35053772526` 成功、
  main/承認SHAへ反映。Free送信設定とprepare-onlyの本番確認applyは、本人crop確認前の更新として
  実行環境の自動承認レビューが拒否。拒否操作を別経路で実行せず、本人へ確認更新・定期再開の承認を依頼。
- 読取検証で固定原本は同一ID/親/MIME/hash、Drive版だけ変更。内容変更・削除はなし。
  loopback UIは同一bytesを確認した現在版を新たに表示できるよう修正。本番storeの版や旧確認を更新せず、
  Actionsの厳密な版照合も維持する。本人の新しいcrop確認を保存するまでは送信しない。
  非公開証跡は `label-20260916`。再受付・本番設定変更は自動承認拒否の解消後に行う。

### 2026-09-16 Medical派生画像AI接続（本番前処理・匿名化保留）

- 更新Goalは固定Medical1件と同じinboxの検証済み派生支払額画像だけを既存Google Gemini APIへ送ることを承認。
  原本、OCR全文、施設名、日付、診療情報は送信対象外。本人確認後だけ既存writerへ進む。
- checkpoint `6d31751` から画像結果/admission、issuer selector、transaction combinedの純粋な根拠検証を回収。
  旧10枚の手動座標や過去AI回答は本番候補へ流用しない。旧承認cropの原本hashは今回対象と一致しなかった。
- 新しい前処理はラベルと囲み枠から領域を求め、許可文字と画素範囲を検証した新規PNGだけを生成。
  日付・施設は非AIの別根拠。原本処理にAI鍵を渡さず、送信子プロセスにDrive/Sheets認証・原本情報を渡さない。
  intentは既存専用JSON、成功結果は同じ原本版/crop/前処理/prompt/modelで再利用。新規3件/runまで。
- 確認欄は上書きせず、候補G列と「候補で医療費を確定」を接続。候補変更時は旧判断を再利用しない。
  不足項目のみ手入力可能。機械候補の生成自体は会計writeを生まない。
- Windows全体合成1561件成功、その後のprepare-only/実OCR合成画像試験も局所成功。
  実帳票は使用せず、合成人物・架空施設・合成支払額の画像で領域分離/混入拒否を確認。
- mainを検証済み `bd047e682c2425276113c37f138c7b072b86261b`、続いて
  `01cdf0e8b945413192cf86c8084a817479b150e9` へ通常fast-forward。
  Linux CI `35050464106`（一般1370＋機微合成194＝1564件）・`35050978028` 成功。
  全CIは本番Secrets/実帳票なし。後者はPDF描画を20M画素以内へ制限し、保存AI応答の改変検知も追加。
- 新親manual `receipt_confirmation` / apply、`MEDICAL_DERIVED_AI_POLICY=prepare-only` で実行。
  run `35050711456` 成功、最初は描画サイズ制限で保留。修正後の `35051151746` も成功し、
  固定Medical1件の非AI施設名・カテゴリ候補を取得。支払額領域を一意に特定できず
  `payment_region_ambiguous_or_absent` で保留。日付・金額候補は空欄。
- 同一専用JSONと確認シートG列/判断選択肢を更新。本人入力H:O/Mは変更せず、
  一般変更不要6件・確認待ち4件、Medical未確定1件、固定原本の版・inbox保持を読戻し確認。
  Gemini送信/応答/候補金額/会計変更は0件。送信0の安全保留を解析成功と扱わない。
- 匿名化修正用の一時的なloopback UIと、版/範囲/PNG hash/署名をActionsで再照合する経路を追加。
  本人が最終画像を見て確認した結果だけを扱い、代理で確認ボタンを押さない。
  原本を表示するのは本人のブラウザだけで、AIへ送らない。UIはローカルの確認記録のみ保存し、
  保守中の反映後も実際の送信はActions側の検証済みPNGに限定する。
- 匿名化確認UIと数値領域の連結拒否を含む実装SHA
  `c41cacfc78764bc0e411dff4f9e08a77120ae531` のLinux CI `35052205070` 成功。
  一般1370＋機微合成210＝1580件、Node 6件、compileall/diff-check成功。Windows全体も1580件成功。
- 同SHAの通常 `all` run `35052380694` は全11段階success。銀行preview/収入write OFF。
  Amazon対象0、au PAY残高21件不変、PayPay新規0、receiptはMedical1/unknown1を保留。
  カードの通常新着3件を取込：auto_expense1件・transfer_aupay_charge2件。
  Sheets読戻しで3件のID一意・統合先空欄・関連支出0件を確認。追加計上条件は復活させていない。
  auto-expense/review-applyの支出追加/更新0、既存固定一般10件の会計4表も不変。
- 通常run後も確認5行のH:O/Mは開始前と完全一致、重複0、Medical原本保持・会計4表0行。
  3 native state ready / 共通ledger全source ready。4stateと専用JSONの本人所有/親/共有を維持。
  Medical解析intent/実送信/応答は0件。匿名化不成立と利用プラン不明のため画像送信だけ保留。
- 定期・共通保守を一時停止してmain更新・実前処理・通常経路確認を完了。
  最終文書commitもLinux CI成功後にmainへ反映し、承認SHAを一致させて06:17/18:17定期と共通保守を再開する。
  最終設定の読戻しは非公開 `anonymous-20260916/final-github.json` に保存する。旧日常OFFを維持。
  Medicalの待ちを理由に一般運用を止め続けない。更新後cronは未観測。
  プラン確認・本人の安全な切出し確認・Gemini実送信/金額候補/replayは未達。
  匿名化AI運用開始・L4とは報告せず、Goalを継続する。

### 2026-09-16 一般・Medicalの本番確認受付開始

- 保存済みnormal10件を再利用し、AI再送なし。5件の従来needs_reviewのうち、店舗表記だけで
  明細・金額・カテゴリが同一の1件は機械判断で既存維持へ。残る4件（うち明細0件が1件）は本人確認対象。
  完全一致3件・内容一致/照合保護2件を含め、変更不要は計6件。修復実績はまだ0。
- 新親へ `receipt_confirmation` scopeと、通常receipt段階のAIキーなし検出を接続。
  既存専用JSONに最小確認stateを永続化し、専用Sheetsタブ「領収書確認」1枚へ表示する実装。
  手入力H:Oをrefreshが上書きせず、未入力・候補・確定待ち・反映済みを区別する。
- Medicalは既存inbox/privacy gateを使い、原本/OCRをAIへ送らない。normalの同一bytesだけを既存AI経路へ渡す。
  原本版・確認内容・重複候補を検査し、本人が明示確定したものだけ既存ID規則で反映する。
  Medical原本は保持。4本番state、銀行preview、Payroll、他sourceの会計条件は変更しない。
- 合成で未入力拒否、入力保持、別プロセス再開、確定反映/重複0、既存支出へのリンク、保存失敗、
  応答不明、原本変更、表示改変、反映直前の入力変更、表示更新の再開を確認。
  実入力がないため本人確認後の実記帳は未確認。合成の反映成功を実記帳成功とは扱わない。
- 実装SHA `68740435e5f3ac5e8d2512b3a6aadd0891a64ad5` のLinux CI `35041277724` 成功。
  一般1370＋機微合成70＝1440件、Node 6件、compileall/diff-check成功。本番Secrets/実帳票なし。
  追加の局所確認で、反映直前に入力が変わり会計callがまだ0なら再確認へ戻し、既存メモも保持する。
- 本番導入への明示承認後、稼働run 0を確認して定期・共通保守を一時停止。
  mainを `c4039a80c7259cbfdef79506972c781d14a44b86` から上記検証済みSHAへ通常fast-forwardし、
  専用bindingと承認SHAを設定。本番受付run `35042040514` は成功。
- 実タブ「領収書確認」は一般4行＋Medical1行。変更不要6件は機械判断として専用storeに保存。
  本人入力欄は全て空欄、固定MedicalのID/版/hash・リンク・inbox保持をread-backで確認。
  Medicalの会計4表は0行、一般10件の関連4表は保存済みsnapshotと一致。修復・本人確定の代行は0。
  実画面で既存値/候補/理由、黄色の入力欄、カテゴリ/判断ドロップダウン、未確認表示を確認した。
  証跡は非公開領域の `confirmation-20260916` 配下。Medical原本を開いたりAIへ送信していない。
- 通常定期と同じ `all` のmanual run `35042346137` も全11段階success。銀行preview、会計新規追加/修正0。
  Amazon候補0、カード既存2、au PAY残高22件不変、PayPay新規0、receiptはMedical1/unknown1を保留。
  review-refreshを含む通常処理後も確認5行・保存11ID・本人入力が完全一致し、確認行の重複0。
  3 native state ready / 共通ledger全source ready、4stateのmetadataと専用JSONの同一ID/本人所有/指定SA共有を維持。
- 定期・共通保守の再開条件は、文書commitも含む最終mainのCI成功・承認SHA一致・稼働run 0。
  再開時のremote SHA/Variables/Workflow状態は非公開 `confirmation-20260916/final-github.json` に保存する。
  旧日常OFFを維持し、新定期は既存06:17/18:17 JSTへ戻す。更新後cronは未観測、Medical実記帳/L4は未確認。
  一般24件の残りMedical1/unknown13は別の未完了項目であり、今回の確認受付5件と混同しない。

### 2026-09-16 本番Geminiで固定normal10件の再解析・比較

- 更新Goalはnormal10件の実原本を本番Actionsの既存GeminiAIからGoogle公式APIへ送信することを明示承認。
  Windows鍵は不要。本番`GEMINI_API_KEY`/SA/SpreadsheetのSecret存在、Workflow env、
  receipt apply子プロセスとSettings/lazy GeminiAIへの受渡しをread-only確認した。
  previewは鍵除去を維持。初回run `35036471978` で実原本の送信・応答・構造化結果検証・保存まで成功。
  既存Secretをそのまま利用でき、新規キー設定・Windowsへのキー移送・認証方式の変更は不要だった。
- 新親manual `receipt_reimport` scopeを追加。固定manifest/専用結果IDを要求し、scheduleからの起動は拒否。
  同じReceiptPipelineの解析・validationを共用し、通常のalready_importedスキップと新着scheduleは維持。
  新しい解析器/認証/モデルは追加しない。最初1件、続く各run最大3件。Linux privacy差は保留。
- 専用結果JSONで送信前intentと成功応答を保存し、同一版のreplayはAI鍵なしで保存結果を再利用。
  ID/版/hash/宛先/modelを照合し、結果不明の再送と保存の盲目的retryを拒否。表示は件数/固定エラーコードのみ。
  比較処理は既存行を読取専用で扱う。今回は訂正根拠が確定した対象がなく、台帳writeは0。
  修復writerは未接続であり、将来の修復には対象と独立した根拠を固定した最小接続が必要。
- 既存本人Drive接続と既存管理フォルダの本人owner/指定SA writerを確認。
  初回upload拒否後、本人が専用JSONの作成・更新を明示承認した。承認後の作成1回で固定IDを保存し、
  SA読戻しbytes一致を確認。以後同一IDへ送信意図・比較元・解析結果を更新した。
  この専用JSONは4本番stateとは別。新規Google資源はJSON 1個、共有変更・台帳修復・原本移動は0。
- 実行main/承認SHAは `7313742a6b775f072c757ef8ccf087cc99355a62`。
  旧SHAの定期run `35036058099` の成功終了を待ち、通常fast-forwardでmainへ反映した。
  定期と共通保守は一時停止して対象runを直列実行。旧日常/旧cache writerのOFFと銀行previewを維持。
- 本番実解析run: `35036471978`（1件）、`35036675727` / `35037017029` / `35037469334`（各3件）。
  normal10件すべてGemini成功・既存validator通過・保存済み。Linux privacy保留0、失敗0。
  全10件で日付/合計は既存と一致。完全一致3件、内容一致/照合リンク保護2件、差分確認待ち5件。
  比較器の分類ではunchanged3 / needs_review7（照合保護2を含む）。対象4表の修復前snapshotと読戻しは一致。
  確認待ち1件の既存明細は0件。非AI原本抽出でも商品/金額対応を確定できず、要確認を維持した。
  他4件の店舗表記等もAI差分だけでは訂正しない。非公開の対象別比較一覧を `receipt-reimport-review.html` に保存済み。
- 再実行 `35037853486` 成功。保存結果10件を再利用、AI再送0・会計変更0・残対象0。
  同一IDの対象4表は修復前snapshotから不変。会計の二重追加/二重修正は0。
- 終了read-backで3 native state ready / 共通ledger全source ready、4state metadata不変を確認。
  専用結果JSONも同一ID・本人所有・指定SA writerのみを維持。最終文書commit後にmain/承認SHAをそろえ、
  新親定期と共通保守を元のactiveへ戻す手順。旧日常/旧cache writerは再開しない。
- Windows全体合成 **1415 passed**、Node 6件、compileall/diff-check成功。
  合成試験と実送信/比較の証拠を分けて扱う。台帳修復成功・全24件再取込完了・L4とはしない。
- 実装SHA `d00044dd84030b9ad0d160355d2af367d9715ee1` のLinux CI `35034043311` 成功。
  一般1358件＋機微合成57件＝1415件、Node/compileall/diff-check成功。本番Secrets・実帳票なし。
- 実行SHA `7313742a6b775f072c757ef8ccf087cc99355a62` のLinux CI `35034710518` も成功。

#### Medicalの独立したWindows非AI検証

- 最新inboxのPDF2件をID/版/hashで固定し、既存非AI gateでMedical1件とunknown1件を識別。
  原本・OCR本文・患者情報を外部AIへ送信せず、Google writeも0。
- 対象Medicalは`needs_review`。ラベル付き支払日・発行施設位置・実支払額の抽出候補はいずれも0で、
  独立した確認根拠なし。欠損を仮日付/0円で埋めず、本人確認済みや自動確定として扱わない。
- 専用の非公開Windows storeへ既存MedicalInboxHandoffShadowでpending1件を保存。
  別プロセスで復元し同一入力は`duplicate_suppressed`、pending1件を維持。既存medical-review CLIで閲覧できる。
  検証用gateのJSON出力が診断コードを省略していたため、元の固定判定記録から復元して再検証。
  元reviewの判断を書き換えず、pending強制解除・原本移動・会計反映は行っていない。
- 最小候補検査・保存・再起動抑止までの実績。利用者向けの会計確認UI/確定反映への接続は未完了であり、
  shadow保存だけで「確認付き運用開始」やL4とはしない。非公開証跡は同領域の`medical-inbox`配下。

### 2026-09-16 一般再取込の固定・比較準備（実再解析は未実行）

- 最新remote mainと実承認SHAは `1af88c5f3b617fe00d53a838ac49fe7a55352ac3` で一致。
  production/schedule=true、legacy_disabled=true、旧日常・旧writer入口OFFをread-only再確認。
  定期run `34980878313` は同SHAでsuccess。今回はrunの状態確認であり、全sourceの個別読戻し検証ではない。
- 指定一般再取込folder直下31件からPDF/画像24件・31ページを固定。CSV6件と配下folder1件は除外。
  固定ID/版/hash/原本・既存4表とカテゴリはrepository外の非公開Windows領域に保存。
  原本移動・新ID発行・既存取込マーカー削除なし。24件すべてに既存receipt/import行があり、
  うち1件は要確認・支出明細0件。取込IDの存在だけで明細完成とは扱わない。
- 既存Windows OCR/privacy gateの実判定はnormal10、Medical1、unknown13。
  unknownには4ページ/5ページのPDFの既存OCR上限による保留を含む。gateや上限は変更していない。
- `app/receipt_reimport.py`に固定sourceと既存IDでの読取専用比較を追加。
  不足候補、ヘッダ/明細差、照合済み支払い、手動判断、無効明細、ID競合を区別する。
  AI差分や既存解析hashの一致だけでは訂正・不足追加を許可しない。writerへの接続は未実装。
- normal10件の既存Gemini再解析は、送信前に実行環境の自動承認レビューで拒否された。
  さらに現在のWindows環境には既存Gemini鍵の設定がないことを確認。
  送信対象10件のID/版/hashと既存送信先/modelを非公開allowlistへ固定。AI送信0、本番変更0。
  一般再解析・差分修復は未完了。inboxのMedical実1件検証・確認付き運用もまだ開始していない。
- Windows合成pytest **1389 passed**（追加11件）、関連31 passed、compileall/diff-check成功。
  branch上の準備でありmain未更新。新定期と固定Drive stateの正本は維持する。

### 2026-09-15 統合Actionsへ本番切替・新定期有効化

- 実行SHA `8b52c6a758fc69e77c59467aec43938829624697`、Linux CI `34968751087` 成功。
  合成pytest 1378件・Node 6件、compileall/diff-check成功。文書更新後も最終main/承認SHA一致を必要条件とする。
- 全source preview `34968092682`、Amazon実canary `34969011775`、read-only replay `34969243835`、
  通常all apply `34969415764`、通常all再実行 `34969971209` が成功。通常経路は両runとも全11段階成功。
- 新規Amazon通常購入1件を実4表各1行へ反映し、実値/日付/金額/関連ID/重複0を非公開計画と照合。
  canary後の同じ購入は候補0、続く通常applyでも再計上0。別の未処理イベント1行は通常経路で保存。
- 再実行前後の実会計/event追加0、既存行変更0、原本移動0。state更新と会計更新を分けて確認した。
  Amazon/カード/銀行/共通ledgerの固定4ファイルが正本でready、全11段階の最終成功あり、pending0。
- 新親production=true、legacy_disabled=true、schedule=true、06:17/18:17 JSTをread-only確認。
  旧日常4と旧writer系manual5入口は停止維持。月次backup/retention・レビュー表示・schema保守3入口のみ元へ戻した。
  Payroll read-only Taskは維持。銀行preview/収入write OFF、Medical現状維持。
- 残る保留: レシート2件（実行中の新着追加1件を含む）、通常applyのカードwithheld1、
  従来対象外のcanonicalカード未反映436行。上限緩和・過去一括修復は行っていない。
- 将来cronの実到来は未観測。定期と同経路の手動実績と定期ONで切替完了とし、L4の実績を捏造しない。
  source別件数・runリンク・復旧/main更新手順はREADMEへ集約。実ID/本文/金額/原本はGit/log/artifactへ出さない。

#### 切替準備・移送の実施履歴

- 移送実装SHA `daef10eb19b96a6c92be1ce57cbb8ff78597b7c8`をLinux CI `34967256358`
  （1322+55=1377 pytest、Node 6件）成功後mainへ通常統合。
- 旧日常4入口と手動/月次write 8入口を停止、live run 0を確認。元状態は非公開保存。
  Payroll read-only Taskは維持。固定IDの暗号文、検証SHA、切替3フラグ以外は変更していない。
- 正式移送 `34967852055` 成功。停止後の完全キーexact取得、4bundle検証・復元・固定ID更新・
  GET bytes/digest一致・本人所有/親/共有維持を確認。旧cacheは変更していない。
- 全source preview `34968092682` 成功（11段階）。独立read-backでも4state・台帳・原本配置は不変。
  過去のcanonicalカード未反映行は計上対象外。銀行preview/収入write OFFを維持。
- 実previewに選定購入と無関係なAmazonイベントが含まれていたため、限定canaryだけ
  選定Order IDのイベントへ絞る最小修正を追加。通常applyの対象/上限・計上条件は変更しない。
  修正検証中は新親/新schedule OFF、旧入口停止を維持し、検証後に上記canaryを実行した。

- 最新Goalは旧停止→正式state移送→preview→限定canary→通常all apply→再実行→新定期ONまで。
  以前のpreview後の旧系復帰計画を置き換え、成功時はDrive正本の新運用を継続する。
- `state-migration.yml` / `state_migration.py`で完全cacheキーと最終state保存run/attemptを照合し、
  native3sourceと新規ledgerをすべて検証・復元後、準備済み固定4ファイルへ同一ID更新する経路を追加。
  manual/main/検証SHA一致・共通排他・既定inspect。Gmail/AI鍵・artifact・cache保存なし。
- Actionsのenvログへ実IDを出さないため、既存SA鍵の公開部分で5固定ID/限定canary referenceを暗号化し、
  Python内でだけ復号する。新規鍵・OAuth・Secrets追加なし。実bindingと設定値は非公開領域に保持する。
- 銀行preview/収入write OFF、Payroll/Medical現状維持。会計条件・authority上限に変更なし。
  成功済みinspect 34962727963とWindows権限試験は反復していない。
- 修正のLinux CI/main統合後に限定canaryから継続し、上記の通常apply/replay・新定期ONまで完了した。

### 2026-09-15 state移送前のcache検査（mainで1回実行、3source成功）

- 旧運用を停止する前の検査として、`state-cache-inspect.yml`を追加。manual/main限定・inspectのみ。
  完全cacheキーと最新run/attempt成功を照合し、exact hit以外は停止。Google Secrets/実ID/本番bindingを渡さない。
- 元cacheは保持し、許可されたnativeファイルとWALだけを一時領域へコピー。
  既存adapter/移送CLIでschema・成功checkpoint・未確定記録・lease・復元一致を検査する。
  診断用bindingのbundleは一時領域だけで破棄。履歴sourceの空bootstrap、cache保存、artifact uploadなし。
- 他Workの銀行収入OFF変更`d9d1e57b3376d07e50bd77a28d5858690bd325f2`を保持して通常merge。
  PROJECT_STATUSの同位置追加は両方の記録を保持。会計ルール・旧入口の実行条件は変更していない。
- 検証SHA `21112169ae62c99ac04e66c0b77fde4542fba60b`。Windows全体 **1359 passed**、compileall/diff-check成功。
  [Linux CI 34960953834](https://github.com/tmoriuchi1401-source/kakeibo-ai/actions/runs/34960953834)成功。
  一般1304件＋Payroll/Medical合成55件。本番Secrets/実帳票なし。
- main更新は初回レビュー拒否後、再提示された移送Goalの許可と検証証跡を確認して通常push成功。
  mainは`d9d1e57`から上記Linux検証済み`21112169`へfast-forward。remote SHAを確認。
- inspectは初回の自動承認レビュー拒否後、本人の「mainでinspectとして1回起動」の明示承認で実行。
  [run 34962727963](https://github.com/tmoriuchi1401-source/kakeibo-ai/actions/runs/34962727963)、attempt 1、
  上記検証済みSHAで20:20:10〜20:20:22 JST、成功。run総数1・artifact 0をAPIで確認。
- Amazon `34907742177-1` / カード `34907806301-1` / 銀行 `34911236780-1` の完全cacheキーで
  exact hit、許可nativeファイル1/5/1、checkpoint・閉じた記録・一時復元一致を全source確認。
  checkpointは同日JSTでAmazon 08:12:38、カード08:13:26、銀行05:48:11。
  最新native statusは順に`noop` / `complete` / `dry_run_noop`。銀行scheduleはpreviewとして扱う。
- 旧運用を動かしたままの候補検査であり、最終移送版ではない。旧入口停止・正式state移送・
  ActionsからのDrive読取・親previewは未実施。Google認証を渡さず、今回のGoogle直接変更0。
- 実Variables画面は未設定。既存28 Workflowはactive。元状態をrepository外の非公開
  `production-state-setup/migration-start-workflows.json`へ保存。設定を変更せず旧運用を継続。
- 20:05 JSTに本番SAのread-only scopeで固定4ファイルを再読込。すべて未初期化の準備用JSONで
  前回試験から不変、本人所有・固定親・指定SAだけの共有を再確認。今回のGoogle直接変更は0。
  証跡は同じ非公開領域の`migration-preflight-destinations.json`。本番state/Actions接続の確認ではない。
- 次は固定4bundleの正式移送経路と、実IDをActions logへ出さない受渡しを準備・検証する。
  準備完了まで旧運用を停止しない。停止後は最新run/attempt/cacheを再確定する。
  inspect成功をDrive本番接続・本番切替・L4確認とは扱わない。

### 2026-09-15 Drive保存先の準備・実接続確認済み

- 今回の到達点は保存先準備とWindowsからの実接続確認。本番state移送・旧運用停止・新親有効化は未実施。
- 開始HEADは`af80174e6bb35e60a763f180858465b7cf075f16`、専用branch/upstreamは
  `integration/production-orchestration-20260915` / `origin/integration/production-orchestration-20260915`。
  working tree clean/stashなし。他Workのmain進行を取得し、最新確認`b61965c970ea391a3177f25d7598f7f45cad123d`
  までfast-forwardで保持。今回mainはpushしない。AGENTS.mdは作業先/親に見つからなかった。
- 実際のGitHub Variables画面は未設定。新親のguardは不成立、旧日常4入口と既存保守入口はactive。
  Variables/Secrets/Workflow設定は変更していない。
- 本人Drive接続と既存Gmail認証の本人を照合。本番SAは既存設定とDrive認証応答を照合。
  本人My Drive直下・家族共有フォルダ外に`KakeiboAI_system_state`を1個作成し、
  本人所有のJSON 4個（Amazon・au PAYカード・銀行・共通運用記録）を配置した。
- フォルダに指定本番SA 1つだけのwriterを付与。子4ファイルは継承し、本人owner+指定SAのみ。
  anyone/domain/group/家族共有なし。親位置は本人認証で確認。既存フォルダ/原本の権限は変更なし。
- 本番SAと既存`DriveStateTransport`で、各JSONの読取→試験値更新→同一IDの新規読戻しを実施。
  **4/4で送信bytes一致、ID/本人所有/親/形式/共有範囲維持を確認**。本人認証でも最終権限を再確認。
  内容は`UNINITIALIZED_NOT_PRODUCTION_STATE`のまま。現行native/ledger validatorが拒否することを確認。
  ready/checkpoint/native stateの作成、履歴sourceの空bootstrap、削除/作り直しなし。
- 実IDは作成ごとにrepository外`%LOCALAPPDATA%\KakeiboAI\production-state-setup\setup.json`へ保存。
  同領域に4個の`*.binding.json`、読戻し/権限証跡、移送元cache metadataを保存。
  ACLはWindows本人/SYSTEMのみ。実ID・認証情報・state本文は公開Gitへ含めない。
- Amazon `34907742177` / カード `34907806301` / 銀行 `34911236780`の成功run、
  state restore/save step成功、同じrun/attemptのmain cache存在をread-only確認。銀行はpreview。
  cache本文やcheckpointは取得しておらず、稼働中の観測を最終移送版にはしない。
- 次回の全write入口停止・run終了確認→最新確定state取得→正式移送/読戻し→親preview→canary→
  定期切替はREADMEに集約。月次/手動/ローカルwriteの割込みも防ぐ保守境界を必要条件とした。
  Actions上の接続確認、本番復元、本番切替、L4確認は未実施。
- 今回の直接Google変更はfolder 1個/JSON 4個作成、SA writer付与1件、JSON内容更新4回。
  Sheets write・原本move/delete・新規OAuth同意/scope/鍵・Task/課金変更なし。従来運用や他Workの操作とは区別する。
  repository変更はREADME/PROJECT_STATUSだけ。補助コード/実行コードを変更していないため、新しいpytest/CIは実行しない。
### 2026-09-15 銀行収入recurring接続（write OFF）

- `b61965c`から専用branchで既存bank runnerへ接続。新たな分類器・DB・schedulerは追加しない。
- ONかつ保護された収入scopeがある場合だけ、入金RAW保存→既存BankIncomePipeline差分反映。
  保存済み未反映は新着0件でも再開し、重複PDF・同一IDの再計上を防ぐ。
- 既存SQLiteのrun記録でappend intentと全列read-backを保持。欠落/不一致の未知結果は停止。
  親のpendingは従来の復旧手順で扱い、自動解除しない。支出・transfer/card条件は維持。
- 上限は支出・入金保存・未反映収入のsource ID和集合で共用。固定backfill承認は再利用しない。
- 銀行専用scheduleはdry-run。直近schedule `34911236780`の実ログでもdry_run_noop/write0。
  Repository Variablesは0件、統合親は未有効。入口切替担当へ変更範囲を共有した。
- Workflow差分は収入Variableを既定falseで渡すenv各1行のみ。schedule・commands・guard不変。
- 実データread-onlyで既存backfill全件一致、追加候補0、要確認保留を確認。
  Inboxも直近schedule preview以降の新規/更新PDFなし。詳細はGit除外 `.private`。
- 最終full pytest **1347 passed**、compileall / diff-check成功。実ネットワーク禁止の合成検証。
- 本番write・Secrets/Variables/authority実変更・入口有効化・Payroll/ホーム変更は未実施。
- 手順と承認差分: [銀行収入recurring接続](docs/bank_income_recurring.md)。
  停止位置は**収入write OFF、本番有効化承認待ち**。

### 2026-09-15 固定銀行収入backfill完了

- 個別承認済み固定planだけを扱う `bank-income-backfill.yml` を追加。manualのみ、既定preview。
- mainの検証済みSHA・対象と全列のcommitment・既存ルール一致を実行直前に照合する。
- 共通production concurrency内で1件canary/read-back/replay、残り、最終preview/replayを順に検証。
- 他表は数式/値の一致を検査。未知の結果や不一致では停止し、自動retry・rollbackを行わない。
- full pytest **1307 passed**、compileall / diff-check成功。テストは合成値のみで外部通信禁止。
- 実行main `e0075c81fb063e3f12a04dda8b51f900c7e44caa`。
- Actions preview `34956505998` / apply `34956603502` とも成功。
- 固定集合のcanary・全列read-back・最終preview・replay追加0・月別合計を確認。
- 別コネクタでも全列とRAW型を再確認。Payroll・支出・取込・要確認・ホーム不変。
- recurring収入未有効。Secrets・schedule・ルール・Payroll設定・Drive/processed変更なし。
- 詳細の固定集合・金額・取込IDはGit除外 `.private` に保存。

### 2026-09-15 銀行収入・Payroll分離（branch検証、本番未切替）

- 最新main `af80174e6bb35e60a763f180858465b7cf075f16` を独立cloneし、
  `feature/bank-income-payroll-separation`で実装。開始時dirty/stashなし。
- 現行Payrollは給与専用2表だけへ保存、ホームは支出一覧参照でPayroll集計なし。
  存在しない連携を切断する機構は追加しない。既存Payroll checkout/Task・保存対象を維持。
- `bank_income.py`に既存確認済みルール＋銀行取引種別での収入判定、保存済み取込基点の
  backfill preview、収入明細A:J schema案、既定OFFのwriter、bank限定月次集計を追加。
  identity resolver / bank ID/hash / Settingsルール / SheetsDBを再利用。
- `bank-income-preview`はread-only clientで動き、apply入口なし。
  新着PDFのdaily/recurringにも同じ判定のpreviewを追加。既存expense-only authorityを維持。
  共通schemaへの登録なし。収入シートや本番UIは未作成/未変更。
- 指定Sheetのread-only snapshotを同じ本実装でpreview。
  分類件数・実金額・摘要・取込ID・元行番号・月次案・確認事項と既存計上の切替判定は
  Git除外の私的報告だけへ保存。実データの診断結果は公開Gitへ含めない。
- 検証：関連93 passed、最終追加/recurring集中50 passed、full pytest **1276 passed**
  （23.39秒、Windows Python 3.14、全テストの実ネットワーク禁止）。
  別checkoutの既存Payroll writer合成テスト17 passed。
  compileall / diff-check成功。既存google-genaiのDeprecationWarning 1件。
- 方針・schema・backfill/recurring別の承認範囲・UI案は
  [銀行収入とPayrollの分離](docs/bank_income_payroll_separation.md)。
  main反映、Googleデータ変更、既存行削除、Secrets/authority変更、収入scheduled writeは行わない。

### 2026-09-15 新入口無効でmain統合済み

- 新Goalの範囲は「新入口無効でmain統合」。本番切替・L4確認は含めない。
- 最新remoteを再取得: main `73ff2ffb859ca82cf7bf4a5f3a375e4e0094d9e2`、
  専用branch `0e7aa79ab04f154f1c40ac49fee2ed998cb3d471`。開始時working tree clean、stashなし。
- canonicalカードの`auto_expense`・統合先空欄の追加計上条件を今回の統合から除外。
  `app/auto_expense.py`を統合前mainと同じ内容へ戻し、新親も従来条件だけを使用する。
  新着/過去未反映どちらも追加計上しない。未反映解消は別の対象・上限・承認を持つ将来作業。
- 実際のGitHub Actions Variables画面でRepository Variablesなしを確認。
  production/schedule/legacy停止の3変数はすべて未設定。APIで旧25 Workflowすべてactiveを確認。
  Variables/Workflowのenable-disable設定は変更していない。
- 旧入口に有効になる差分はREADME「会計条件の分離」へ記録した。
  共通lock/main guard、上限超過の処理前停止、preview読取専用化、normal provenance/retention縮小。
  cron/sourceコマンド/銀行scheduleのpreview、金額/計上先/従来対象は維持する。
- 修正後Windows検証: 集中回帰42 passed、全体1234 passed（24.74秒）、compileall/diff-check成功。
  ネットワーク禁止の合成テスト。旧CLIの親OFF、従来3決済source、除外状態、返品/transfer、
  部分write後と完了後の重複防止を確認。親合成経路も5取込/4支出とカード未反映維持を確認。
  修正SHA `002a112bcbfba2fcee5b9b6abf2e63f0d28e7cca`の
  [Linux CI #34925859316](https://github.com/tmoriuchi1401-source/kakeibo-ai/actions/runs/34925859316)も成功。
  Ubuntu 24.04.5 / Python 3.12.14、一般1183 passed（13.47秒）、Payroll/Medical合成51 passed
  （4.30秒）、合計1234。両jobのcompileall/diff-check成功。本番/AI Secrets・実帳票なし。
- mainを`73ff2ffb859ca82cf7bf4a5f3a375e4e0094d9e2`から上記`002a112`へfast-forwardし、
  通常push/remote SHA一致を確認した。後続の完了記録commitは文書のみ。
  実行コードとWorkflowはLinux検証済みSHAと同一。専用branch/upstreamは
  `integration/production-orchestration-20260915` / `origin/integration/production-orchestration-20260915`。
- 統合直前にもVariablesなしを再確認。統合後APIで旧25 Workflow activeと新親登録を確認。
  新親は登録状態activeだがjob guardのVariable条件がfalseで、本番処理は開始しない。
  旧入口は継続可能。親OFFを旧本番コードへの影響ゼロとは扱わない。
- 今回の直接外部変更は専用branch/mainの通常pushと合成CIだけ。本番Workflowの手動起動、
  enable-disable設定、Google操作、state作成/移送、Variables/Secrets/OAuth、Task、公開/課金変更なし。
  従来の定期運用によるGoogle操作は継続するため環境全体のwrite=0とは断定しない。
  到達点は新入口無効でmain統合済み。本番切替完了・L4確認済みとはしない。
- 次の実作業はDrive固定stateファイルの準備・権限確認・移送。準備前に旧運用を止めない。
  本GoalではGoogle/state/Task/Variables/Secrets/OAuthに直接変更せず、本番Workflowを手動起動しない。

### 2026-09-15 統合・切替準備の履歴（下記の会計条件追加は上記で除外）

- 最新main `73ff2ffb859ca82cf7bf4a5f3a375e4e0094d9e2` を独立cloneし、
  `integration/production-orchestration-20260915` で作業。開始時dirty/stashなし。
  詳細な現行→移行後の責任表、全25 Workflow/Windows棚卸し、復旧・切替手順は
  **README「8. GitHub Actions / 統合・切替準備」**へ集約。
- 銀行の記述不一致を確認: latest mainはcron 06:47 JSTがあるが、schedule実コードは
  `--dry-run`。直近#3/#4はmanual success（今回入力apply値・個別write数未確認）。
  以下の過去L4 recurring記述は定期applyの現状証明に使わない。勝手にapplyへ変更しない。
- Windows Payroll Taskは毎日06:00、09-15 06:00:01終了0。最新logはread-only true /
  writer invocation 0。Task変更なし、新規帳票件数は未確認。
- 実装: `drive_run_state.py`で既存SQLite/manifestのsource・target・schema検証、
  source別Drive保存/read-back、pendingによる中断・不明結果の自動再開抑止を追加。
  `production_run.py`で直列順序/依存失敗skip/件数だけのsummary/失敗伝播を追加。
  新規DB・外部lock基盤は作成していない。
- 合成テスト: fake Driveの欠落/破損/binding違い/WAL/保存不明/復旧を検証。
  既存Amazon runnerの実write経路をfake Sheets/Gmailで実行し、state保存失敗→
  確認待ち→明示的復旧→replay write0を確認。合成テストは実Googleへ接続しない。
- 最新検証: Windows Python 3.14.7、全体pytest **1183 passed**（19.37秒）。
  `tests/conftest.py`で全テストの実ネットワーク接続を禁止して成功。
  compileall / diff-check成功。google-genai由来の既存DeprecationWarning 1件。
  Linux/Actionsの実行成功を意味しない。checkpoint前fetchでもorigin/mainは同SHA。
- 第2checkpoint準備: 親Workflow/既存CLI assembly、全25既存入口の共通concurrency/main guard、
  offline state移送CLI、au PAY残高dry-runと取得上限の事前検査、receipt/PayPay inboxの
  pagination超過検出、retentionのnormal provenance限定をbranchに追加。
  manual previewとschedule有効化を別Variableに分離。銀行scheduleはpreview維持。
  SecretsなしのLinux合成Workflow（一般 / Payroll・Medical別job）を追加、未実行。
- レシート/PayPay/共通writeも`production_ledger.py`の非公開運用JSONでpendingと最終成功を保存。
  不明結果の自動再実行を禁止し、旧成功時刻を保持。初期bundle準備はoffline専用CLI。
- 第2検証: Workflow YAML/trigger/guard/lock/依存、親CLIの本番境界、既存関連処理を含む
  最終全体pytest **1209 passed**（20.60秒）、compileall / diff-check成功。
  最終読込/銀行preview成功時刻の調整も含めた結果。ネットワーク禁止下で実行。
  ローカルテスト依存PyYAML 6.0.3をGit除外領域へ追加（`PYTHONPATH=.private/test-deps`）、
  再現用は`pip install -r requirements-test.txt`。要件別監査はREADMEの末尾表に記録。
- 第3検証: 共通fake Sheets/Drive/Gmailに既存CLI/parser/SheetsDB/後処理を接続し、
  Amazon・一般receipt・カード・残高・PayPayの新規取込と支出反映、通常apply再実行の会計append0を確認。
  銀行は現行どおり空folder preview。state破損/receipt commit後の応答消失では依存後処理を停止。
  ローカルOCR/AI応答のみfixtureとしprivacy判断は既存gateを通した。実通信・削除なし。
- 通し検証でcanonicalカードの`auto_expense`取込行が共通計上されない不整合を発見し、一度修正した。
  canonical ID/hash/取込日時あり・統合先空欄だけを対象にし、既存stable支出ID/dedupe/照合を維持。
  この追加計上条件は最新Goalで統合対象から除外した。実データの修復は未実施。
  共通preview 5種をread-only Sheets接続へ変更。
- 親manualへ`scope=amazon_canary`を追加。既存exact reference/1購入/event1/header1条件を使い、
  他source/共通後処理は起動しない。合成canaryの4表各1行、read-only replay write0、
  対象不一致時の会計write0を確認。具体入力/実read-back/復旧順序はREADMEへ集約。
  checkoutはmain tracking refを取得し、承認SHA/イベントSHAから進んでいれば実行前に拒否する。
- 最新全体検証: **1231 passed**（22.71秒）、既存DeprecationWarning 1件。
  compileall / diff-check成功。Windows Python 3.14.7、全テストでネットワーク禁止。
- GitHub Billing Overview / Budgetsで契約・含まれる枠・残量・支出停止設定をread-only確認。
  具体的なアカウント利用額等はGit除外の私的確認記録に保持。READMEには公開仕様に基づく
  private化の影響と推計のみを追記。公開設定/契約/予算の変更なし。
- 追加外部確認: 現在設定済みのSAでDrive `about.get`だけをread-only実行して成功。
  SAの容量上限0/共有ドライブ作成不可を確認。新state folder/file IDは未設定のため、
  実ファイルの所有/共有/更新は未確認。既存所有者による初期配置と現在SAでの固定file更新を
  READMEへ明記し、認証切替・所有権移転・Google外部writeは行っていない。
- Linux検証完了: 09-15のユーザー明示承認後、`943e4771abb8dbf4fdd1cb7fc17eee38c7b4004f`を
  専用branchへpush。upstreamを設定し、[Synthetic integration checks #1](https://github.com/tmoriuchi1401-source/kakeibo-ai/actions/runs/34921871563)
  が成功。Ubuntu 24.04.5 / Python 3.12.14で一般系1180 passed（13.38秒）、
  Payroll/Medical合成51 passed（4.05秒）、合計1231 passed。両jobのcompileall/diff-checkも成功。
  本番/AI Secretsなし、実帳票なし、テストからのネットワーク接続禁止。mainは`73ff2ff`のまま。
- **未完了:** 外部stateファイルの所有/共有/更新可否の確認、
  実対象canaryの承認・state移送・実read-back。実切替は未実施。Goalは継続中、L4へ昇格しない。
- checkpoint `20452dc` の通常pushは自動承認レビューがpublic repoへの新コード/運用情報公開として
  拒否。その後、ユーザーが公開pushとSecretsなしLinux CIを明示承認し、上記push/CIを完了した。
  この承認にmain統合・本番起動・Google外部write・公開設定変更は含まれない。
- **外部未実施:** main統合・Workflow起動/変更・Google write/move/delete・state upload・
  Task停止・Secrets/OAuth変更・visibility/課金変更。本番L4への昇格なし。
- 現在repoはpublic。契約枠/支出停止設定は確認済み。private化は提案段階で未実施。

- **確認日:** 2026-09-14 JST
- **Repository:** `tmoriuchi1401-source/kakeibo-ai`
- **main（一般レシート機能checkpoint）:** `a01f7c3453a90e7214438ea073d43d3115c1e169`
- **確認したもの:** GitHub main / 銀行3adapter統合 / 銀行実PDF smoke / 一般レシートActions #203（attempt 1 / replay attempt 2）/ Google Sheets「家計簿AI」read-only
- **銀行統合時検証:** full pytest `1056 passed`、compileall成功、diff-check成功
- **origin/main（今回の再統合基点）:** `bbee3679d2ff2b53e36922640603ef7c03df4b5a`
- **銀行最終反映checkpoint:** `agent/bank-final-reflection`、transfer判定コードcheckpoint `622b58cabc23f538dbda3f98ed50301a405e239d`。full pytest `1088 passed`、関連test `63 passed`、compileall成功、diff-check成功。最終反映経路で合計98件の本番支出を反映し、write-eligible支出backfillを完了。さらに人間確認済みの自己口座間transfer 19件を`bank_non_expense`へ反映
- **今回未確認:** Task Scheduler最新履歴、ローカルMedical review store

### 現在の最重要判断

KakeiboAIは、主要な入力sourceを新しく増やす段階より、**既に取り込めているデータを最終的な家計簿表示までつなぎ、既存production経路を安定運用する段階**に入っている。

**次の主作業:** 銀行の残るexact card settlement 12件は別の明示承認まで現状維持。recurring productionはまだ有効化しない

**並行する小作業:** 一般レシートはproduction保守完了。以後は通常運用監視のみ

---

## 2. 全体ステータス

| 機能 | Level | 完成範囲 | 現在の残課題 | 次の1作業 |
|---|---:|---|---|---|
| **PayPay** | **L4** | 通常`支払い`CSV → Drive → dedupe → 取込 → 支出計上 → processed → replay安全性 | 返金などparser対象外の例外は別扱い | **保守。通常支払いの再開発をしない** |
| **Amazon通常購入** | **L4** | Gmail新着通常購入のbounded recurring production | 返品・返金・取消、CSV商品明細は別経路 | **保守。対象範囲を混同しない** |
| **au PAYカード** | **L4** | Gmail incremental recurring production。実write確認済み | review項目の意味と最終処理状況 | **reviewだけ確認** |
| **au PAY残高** | **L4相当** | Gmail通知取込・既存dedupe・共通後続処理 | 大きなblockingなし | **保守** |
| **一般レシート** | **L4** | `receipt_inbox` → privacy gate → normalのみGemini → structured明細 → Sheets → processed。実シートに解析済み24件。scan PDF 3件のproduction canary / read-back / replay確認済み | blockingなし。低頻度例外は既存review運用 | **保守。offline OCR方式へ置換しない** |
| **銀行PDF（auじぶん / ドコモSMTB / 千葉）** | **L4 recurring production** | 住宅ローンcanary 1件、3銀行代表10件、backfill 87件の合計98件で本番支出write。transfer 19件とcard settlement 12件は`bank_non_expense`、収入48件は支出化せず維持。manual recurring canary / replayは新着0件・追加write 0で成功 | 毎日06:47 JST、最大20 PDF / 100 rows、expense-onlyのstanding authority。review / income / unsupported bankは自動writeしない | **保守。authority scopeを拡張しない** |
| **Payroll** | **L3 / 定期scanはread-only** | 実シートに給与明細ヘッダ1件・項目18件・勤務先マスタ1件。Windows scheduled read-only scanあり | 最新scheduled runと新規明細時の運用確認 | **Task Scheduler実績を1回確認。新規明細がなければ開発しない** |
| **Medical** | **L1〜L2 / privacy運用中心** | Medicalを外部AIへ送らない本番境界、local OCR / review / shadow実装 | local review永続運用と未見帳票評価は別課題 | **既存review運用を確定。新データなしにtaxonomyを増やさない** |
| **共通 reconcile / auto-expense / review / 支出一覧** | **L4** | 本番経路と定期実行実績あり | sourceごとの未反映・例外を可視化 | **作り直さない** |

---

## 3. 銀行PDFの入力基盤は3銀行対応済み。支出backfillと確認済みtransfer整理も完了

### 2026-09-14 銀行統合の進捗

3銀行のPDF対応をmainへ統合済み。現在のmainは `30b277ac7f2f8e9807053bf9c0a769a0737e055e`。

- 対応銀行: **auじぶん銀行 / ドコモSMTBネット銀行 / 千葉銀行**
- 発行元marker・header・geometryからadapterを自動判別
- 未知形式はfail-closed
- 通常運用は `bank-pdf` preview → 明示承認時のみ `--apply`
- stable source identity / duplicate / collision / bounded authority / read-back安全境界を維持
- 千葉銀行の専用branch成果もmainへ統合され、3銀行が同じ日常運用経路になった

実PDF parser smoke:

| 銀行 | parser結果 |
|---|---:|
| auじぶん銀行 | **92件** |
| ドコモSMTBネット銀行 | **150件** |
| 千葉銀行 | **53件** |

統合時検証: full pytest **1056 passed**、compileall成功、diff-check成功。

Sheets read-only再確認では、既取込identityはduplicateとして吸収され、新規候補がないケースはsafe no-opになることを確認。少なくともauじぶん銀行はduplicate 61 / candidate 0、ドコモSMTBはduplicate 90 / candidate 0で、再処理による既存行の再writeは発生させない。

### 既存シートで確認済みの銀行行（2026-09-14 transfer整理完了時点）

| status | 件数 | 現在の意味 |
|---|---:|---|
| `bank_expense` | **12** | exactなcard settlement。今回のtransfer 19件とは分離し、未反映のまま保護 |
| `bank_non_expense` | **19** | 人間確認済みのG1/G2/G3自己口座間transfer。支出を作らず確定済み |
| `bank_loan_repayment` | **0** | 住宅ローン返済4件はすべて支出反映済み |
| `bank_income` | **48** | 銀行収入として取込済み。支出にしないことをpreviewで確認 |
| `auto_expense` | **106** | 既反映8件と、銀行最終反映経路で追加した98件 |
| **合計** | **185** | 現在のproduction sheet観測値。parser smoke件数とは別物 |

**重要:** parser smokeの 92 / 150 / 53 はPDFを解析できた件数であり、Sheetsへ新規writeした件数ではない。既存185行の棚卸し値と混同しない。

### 取込済み銀行行の最終反映preview

`agent/bank-final-reflection` の共通read-only previewで、現在の185行を次のとおり分類した。

| 最終分類 | 件数 | 扱い |
|---|---:|---|
| 新規支出 | **0** | 承認済み87件backfillを完了。追加予定writeなし |
| 新規収入 | **48** | `bank_income`を維持し、支出明細へ書かない |
| excluded / link | **0** | 現在の実データに一意な既存支出link候補なし |
| non-expense | **31** | 確定済み自己口座間transfer 19件と、未反映のexact card settlement 12件。支出を作らない |
| review | **0** | G1/G2/G3の19件は人間確認済みexact ruleで解消 |
| duplicate | **106** | 既存`auto_expense`と本番反映済み98件のstable targetを確認。再追加しない |

production入口は通常workflowへ未接続。applyはexact 1 identity、承認済みSpreadsheet ID、expected Git HEADの完全一致を要求し、`取込データ`と`支出明細`をread-backする。2026-09-14に住宅ローンcanary 1件、auじぶん4件 / ドコモSMTB 3件 / 千葉3件のbounded batch 10件、続くbackfill 87件を同じ1件単位経路で反映した。87件は全件read-back一致し、同一identityのreplayはduplicate 87 / 予定支出write 0 / 予定取込更新0。

続いて、人間確認済みの自己口座間transfer 19件を、既存operator rule機構のexact条件だけで`bank_non_expense`へ反映した。条件は正規化摘要・方向・account aliasの完全一致で、G1 `口座振替SMBCドコモSMTB` / outgoing / `jibun-primary`、G2 `口座振替SMBCスミシンSBIネツ` / outgoing / `jibun-primary`、G3 `口座振替DFAUジブン` / outgoing / `docomo-smtb-primary`。19件すべて取込更新1 / 支出作成0 / 支出更新0 / read-back一致。同一19 identitiesのreplayは`already_non_expense` 19 / 予定取込更新0 / 予定支出write 0。最終全体previewは新規支出0 / 収入48 / non-expense 31 / review 0 / duplicate 106 / link 0、予定支出write 0 / 予定取込更新12。残る12件は今回対象外のexact card settlementであり未変更。

### 次のGoal

write-eligible支出backfillと確認済みtransfer 19件の整理は完了。残るexact card settlement 12件は今回の承認範囲外として変更せず、別Goal・別承認まで保護する。4つ目の銀行adapter追加とrecurring production有効化はまだ行わない。

### 銀行Workの終了条件

- 3銀行とも同じ日常preview / apply経路を再利用できる → **達成済み**
- 既存identityの再処理がsafe no-opになる → **確認済み**
- 129件の未反映出金行（`bank_expense` 125 + `bank_loan_repayment` 4）を、支出98 / non-expense 31に分離。支出98件と確認済みtransfer 19件を反映済み。残りは保護対象のexact card settlement 12件
- 48件の`bank_income`を支出と混同しない → **preview確認済み**
- 既存決済sourceとの二重計上を作らない → exact link候補0、card settlement 12を非支出、支出backfill完了後は既反映106をduplicateとして確認
- 必要なコード変更は最小限
- 最終反映についてcanary → 3銀行bounded batch → 87件backfill → 各件read-back → replay安全性を確認 → **98件で確認済み**

### やらないこと

- 4つ目以降の銀行adapterを、既存3銀行の最終反映より先に追加する
- 全銀行を共通化するための大規模framework再設計
- non-expense対象31件を支出として追加
- 銀行の摘要だけを頼りに危険な自動分類を増やす

---

## 4. 一般レシート: 本流はAI解析。offline MVPではない

### Production authority

`receipt_inbox` → `ReceiptPipeline` → local privacy判定 → **normalのみGemini** → structured output → category / total / date検査 → Sheets → `receipt_processed`

- Medical / payroll / `sensitive_unknown` は外部AIへ送らない
- `agent/general-receipt-import` のoffline OCR previewは診断用であり、本番parserではない
- 本番の一般レシートは**全体画像/PDFをAI解析に回す方式**

### 実績

- 実シートに**解析済み24件**
- これはproduction利用実績であり、「24/24を人手照合して完全正解」という意味ではない

### 2026-09-14 production保守結果

blocking issueに限定した最小修正を `a01f7c3453a90e7214438ea073d43d3115c1e169` としてmainへdeploy済み。

1. 最新の定期run #202（HEAD `7d0e60d`）ではPDF 3件が `pdf_ocr_failed` → `sensitive_unknown` → Gemini禁止で保留。PDFはembedded textが空でOCR fallbackへ進んでおり、runner workflowにTesseract本体と日本語言語データのsetupがなかった
2. workflowで `tesseract-ocr` / `tesseract-ocr-jpn` を導入し、`jpn` / `eng` availabilityを実行前に検査
3. mergeで脱落していた `GeminiAI.analyze_receipt(..., known_source_classification=...)` とadapter直前のprivacy再検査を復元
4. `取込データ`をreceipt materializationのcommit markerとして最後にwriteし、レシートID / 支出IDでpartial retry時の重複を抑止
5. synthetic通常画像 / scan PDFの実Tesseract smoke成功、OCR runtime欠落時のfail-closed再現成功。full pytest **1067 passed**、compileall / diff-check成功

明示承認後のActions #203 attempt 1で保留scan PDF 3件をすべて `imported` とし、支出明細5件を生成してprocessedへ移動。Sheets read-onlyではレシート24/24が`解析済`、receipt import marker 24件が全件一意、今回の支出ID 5件も全件一意だった。attempt 2のreplayではreceipt inbox結果0件・追加write 0で成功した。

一般レシートのblocking残課題はない。以後は既存定期workflowを保守し、低頻度例外だけreviewへ送る。

### 終了条件

- 通常レシート画像/PDFがproductionで処理できる
- Medical / sensitiveをGeminiへ送らない
- PDF前処理失敗を認識できる
- AI解析成功後のSheets反映と再実行が安全
- 追加のoffline OCR研究へ戻らない

---

## 5. 完成済み / 保守モード

### PayPay

**完成範囲:** 通常`支払い`。

2026-09-14までのproduction acceptanceで、46支払いについて既存1 / 新規45、append45、46/46 read-back、processed move、auto-expense45、duplicate 0、replay 0を確認済み。

**保守ルール:**
- 通常支払いparser / Drive取込 / dedupeを再設計しない
- 返金・送金・チャージ等を「通常支払い完成」のblockingにしない
- 例外は実際に必要になった時だけ個別対応

### Amazon通常購入

**完成範囲:** Gmailの新着通常購入の注文合計をbounded recurring productionで取り込む。

**対象外 / 別経路:** 取消・返品・返金、Order History CSVの商品明細。

「Amazon全イベント完全自動」と表現しない。

### au PAYカード

recurring productionは実writeまで確認済み。直近確認runでは新規3件を書込み、failure 0。

`review=4` は要確認対象だが、取込本体を未完成へ戻す理由にはしない。何を示すかだけ確認する。

### au PAY残高

共通workflowで継続運用。保守扱い。

---

## 6. Payroll

### 現在地

- 実シート: 給与明細ヘッダ **1件 success / 要確認FALSE**
- 給与明細項目 **18件 / 要確認FALSE / not_required**
- 勤務先マスタ **1件**
- `integration/payroll-materialization-adoption` にWindows scheduled **read-only** scanあり

### 次の1作業

Windows Task Schedulerについて以下だけ確認する。

- LastRunTime
- LastTaskResult
- scheduled scan log
- 新規明細の有無

**新しい明細がなければ、追加実装をしない。**

---

## 7. Medical

### 維持する絶対境界

- 実Medicalデータを外部AIへ送らない
- privacy判定でMedical / sensitiveはfail-closed
- local OCR / review / shadowは外部AI authorityとは分離

### 現在の課題を2つに分離

**A. 帳票解析精度**
- 未見の実帳票が来た時に評価
- 新データなしにLevel / taxonomy / diagnosticを増やさない

**B. 本番review運用**
- Windows local review store
- 人間が確認する手順
- shadowの永続性

GitHub-hosted runner上でlocal storeを永続化できない問題と、帳票解析ロジックの問題を混同しない。

---

## 8. 共通運用

共通production workflowには、レシート、au PAY通知、PayPay、reconcile、auto-expense、review refresh、支出一覧refreshが存在する。

### 状態解釈ルール

- workflow green = 全データが処理済み、ではない
- `privacy_blocked` = workflow failureではなく、安全な保留になり得る
- `0 candidates` = 正常no-opになり得る
- `needs_review=0` = parser対象外イベントまで全て処理済み、ではない

---

## 9. 優先順位

### Priority 1 — 銀行3行の残るnon-expense運用

3銀行parser・adapter統合、write-eligible支出backfill、確認済みtransfer 19件の整理は完了済み。残る**exact card settlement 12件は、別承認があるまで変更しない**。recurring productionの有効化にも進まない。

### Priority 2 — 一般レシートproduction保守（完了 / 保守）

PDF前処理とAI呼出し契約の最小修正、production canary、read-back、replay確認まで完了。新しい解析方式は追加しない。

### Priority 3 — 運用確認

- Payroll scheduled run
- au PAYカード review
- PayPay返金等の例外（必要なら）
- Medical local review運用

### Priority 4 — その後にCoverage実測

新しいsourceを増やす前に、1か月程度の実データについて以下を測る。

- 自動計上率
- 人間確認件数
- 取込済み未計上件数
- 二重計上候補
- 未対応sourceの実際の件数

この実測で、次の機能追加を決める。

---

## 10. Codex / Work / Goal 開始時ルール

新しいKakeiboAIタスクを始める場合、実行者はまずこのファイルを読み、対象機能について次を確認する。

1. 現在のLevel
2. 完成済み範囲
3. 次の1作業
4. 「やらないこと」
5. Last verified以降にmain / runtimeが変わっていないか

### 基本方針

- 既存production authorityをまず再利用する
- 新規frameworkより既存経路の接続・修正を優先する
- 重大事故を防ぐfail-closedは維持する
- 低頻度・低影響はreviewへ落としてよい
- 実データなしに証明用diagnosticsを増やさない
- 本番write / apply / Drive move / permission expansionは明示的な承認境界を守る
- コード変更前にread-onlyで実際のblocking issueを確認する

### 作業終了時に更新する項目

対象機能について以下だけ更新する。

```text
Level:
完成範囲:
Last verified:
実データ実績:
残課題:
次の1作業:
やらないこと:
Evidence:
```

**一つのGoalの完了だけで、無関係な機能の状態を書き換えない。**

---

## 11. 今は行わないこと

- 一般レシート本流をoffline OCRへ置換
- PayPay通常支払いの再実装
- 旧PayPay coverage branchの全面統合
- 見つからなかった古いworktreeの復旧調査を継続
- 新データなしのMedical diagnostics追加
- 3銀行の最終反映を放置したまま、4つ目以降の銀行adapterを増やす
- テスト数・workflow greenだけを根拠に「完成」とする
- KakeiboAI Core完成前に販売インフラを優先する

---

## 12. Evidence / 参照先

### Production / main

- `RECEIPT_ROUTING.md`
- `app/receipt_pipeline.py`
- `app/gemini_ai.py`
- `app/receipt_text_extraction.py`
- `app/paypay_pipeline.py`
- `app/bank_pdf_pipeline.py`
- `app/bank_canary.py`
- `app/bank_canary_production.py`
- `app/auto_expense.py`
- `docs/amazon_recurring_production.md`
- `docs/aupay_card_recurring_production.md`
- `.github/workflows/process-receipts.yml`

### 専用branch / checkpoint

- Payroll: `integration/payroll-materialization-adoption`
- Medical: `integration/medical-structured-ocr-privacy`
- 千葉銀行旧開発branch: `agent/bank-pdf-chiba`（成果はmainへ統合済み。通常運用はmainを正本とする）
- 一般レシートoffline診断: `agent/general-receipt-import`（**production本流ではない**）

### 2026-09-14にread-only確認したproduction evidence

- 一般レシート: 解析済み24件。Actions #203 attempt 1でscan PDF 3件 / 支出明細5件をproduction反映しprocessed移動、attempt 2でreceipt結果0件のsafe replay
- PayPay: 取込52行 / 支出明細52行
- 銀行PDF（transfer整理完了後の最終preview）: 取込185行 / 新規支出0 / 収入48 / non-expense 31 / review 0 / duplicate 106 / link 0。予定支出write 0 / 予定取込更新12。今回の19件replayは`already_non_expense` 19 / 予定取込更新0
- 銀行3adapter統合後の実PDF smoke: auじぶん92 / ドコモSMTB150 / 千葉53
- 銀行統合main: `30b277ac7f2f8e9807053bf9c0a769a0737e055e`、full pytest `1056 passed`
- Payroll: header1 / item18 / employer1
- 直近共通workflow: 一般レシート #203成功（機能checkpoint `a01f7c3`）。OCR `jpn` / `eng`検査成功、保留PDF 3件解消
- au PAYカード recurring: 新規3件write成功
- Amazon recurring: 正常no-op

---

## 13. 履歴

### Google Sheets 日常UI（対応導線の本番適用・検証完了）

- **Level:** 利用者承認後に本番UIを適用。native数値・構造照合、fixture検証、390px幅での実画面目視確認を完了。
- **完成範囲:** UI所有のホーム・カテゴリ対応、先頭の日常3画面、内部台帳5枚の非表示。ホームの件数と「対応する →」を同じ行に配置。対象月のカテゴリ未分類専用一覧から既存支出明細F:Gへ2段階で移動し、人がカテゴリを明示修正できる。通常/Amazon reviewは既存の全期間・判断/候補欄へリンク。対象月プルダウン、UIだけの再生成/復元、既存refreshの書式補正を維持。
- **Last verified:** 2026-09-14、基準main `f89021c`からホームだけの31 UI requestsを適用。月切替検証後も既存20業務シートの値・数式・入力規則・メモ・rich textが適用前と一致。支出/取込/Amazonデータ、status定義、分類/取込authorityに変更なし。
- **実データ実績:** 当月→過去月→「当月（自動）」のnativeプルダウン操作で、対象月・見出し・支出合計・未分類件数/金額・全カテゴリ集計が独立計算と一致。両reviewは全期間のまま不変。最後に元の過去月選択を復元。ホームA:Iの数式エラー0、グラフ1。実データや給与値は記録しない。
- **再実行/復元:** B4の選択を保持し、UI所有マーカー付きの2シートを再利用。ID欠落/重複、無効・異なる月・分類済みの元台帳行には修正リンクを出さない。推奨は一意かつカテゴリマスタに存在する商品一致/同じ店舗履歴のみで、自動採用しない。今回の限定UI復元18 requestsをprivate領域へ保存。fixtureで再生成・選択保持・所有権・復元・リンク検証が成功。
- **目視確認:** 2026-09-14、利用者が金融情報を含む実画面画像の作業内確認を明示承認。390px・100%でホームの金額/注意書き/リンク/カテゴリ表/グラフ、通常reviewの判断/統合先/カテゴリ/Amazon候補、Amazon reviewの候補選択欄を確認。支出一覧B/C幅を86/90pxへ調整し、A:D合計331pxで金額末尾の切れを解消。判断ドロップダウンは選択せず閉じ、値の保持を確認。
- **今回の目視確認:** 390px・100%で「要対応」、対象月の未分類件数/金額、全期間のreview、短い注意書きと月選択が横切れ・不自然な改行なく表示された。当月と過去月の両方を確認し、列幅320px・色・フォント・大きな金額表示を維持。一時的なブラウザー幅は復元済み。
- **対応導線のLast verified:** 2026-09-15、実装基準`f24a290`。当月/過去月/0件のnative確認と390pxでのカテゴリ修正先・通常/Amazon review入力欄への移動を実施。再開時の月・件数更新後も、最新データで未分類全行・修正先・候補・月集計・全期間reviewが独立計算と一致し、UI数式エラー0。最新の月選択を保持、ブラウザー幅を復元。業務値・判断・入力規則への書込み0。中断前後の全業務値の完全一致は未確認。
- **テスト:** 現行main `cbb7c8e`取込み後のcheckpoint `59895bf`で全体pytest **1141 passed**（関連UI **32件**を含む）、compileall・diff-check成功。実データのカテゴリ入力、推奨採用、一括更新、本番refreshは実施していない。
- **コード統合:** [PR #4](https://github.com/tmoriuchi1401-source/kakeibo-ai/pull/4)。利用者が2026-09-15に既存GitHubリポジトリへのUIコード送信と銀行変更を含む現行mainへの統合を明示承認。既存mainを競合なく取り込み、mainとの差分はUIコード・テスト・文書8ファイルだけ。銀行authority・workflowの追加変更はない。
- **残課題:** 必須UI改修は完了。カテゴリ手修正は次の既存支出一覧refreshで反映し、source再取込時のauthorityは従来どおり。実機iOS/Android固有の操作は未検証。旧カテゴリ補助シートは保持。
- **次の1作業:** 通常の既存refreshに合わせて利用する。UI確認目的の本番業務処理は起動しない。
- **やらないこと:** 既存シート削除/改名/列順変更/取引修正、共通処理の再開発、本番業務処理を使ったUIテスト、新規認証・サービス・共有/保護変更。
- **Evidence:** `app/sheets_ui.py`、`app/sheets_ui_actions.py`、`app/sheets_ui_cli.py`、`app/sheets_ui_verify.py`、`tests/test_sheets_ui.py`、`tests/test_sheets_ui_actions.py`、`docs/sheets_daily_ui.md`。private観測/承認資料はGit除外。

### 2026-09-14

初回の全体棚卸しから長期運用用ステータスへ整理。

主な修正点:
- 一般レシートはoffline MVPではなくGemini productionが本流と確定
- PayPay通常支払いをL4 / 保守へ移行
- 銀行の「取込」と「家計簿反映」を分離し、次の主Workに設定
- Payrollは実登録済みであり、未着手扱いを撤回
- Medicalの解析精度と本番review運用を別課題として管理

### 2026-09-14 銀行更新

- auじぶん銀行 / ドコモSMTBネット銀行 / 千葉銀行の3adapterをmainへ統合
- mainを `30b277ac7f2f8e9807053bf9c0a769a0737e055e` へ更新
- 実PDF smoke: 92 / 150 / 53件
- full pytest `1056 passed`、compileall / diff-check成功
- 千葉銀行を「専用branchの残件」から「main正式対応」へ変更
- 銀行の次Goalをadapter追加ではなく、既取込行の最終家計簿反映に限定

### 2026-09-14 一般レシートproduction保守

- runnerへTesseract本体 / 日本語言語データと`jpn` / `eng` availability検査を追加
- AI adapterの`known_source_classification`契約と送信直前privacy再検査を復元
- receipt / expenseのstable IDと`取込データ`commit-marker-lastでpartial retry / replay重複を抑止
- full pytest `1067 passed`、compileall / diff-check成功
- main `a01f7c3`のActions #203 attempt 1で保留scan PDF 3件をimport、支出明細5件、processed移動を確認
- Sheets read-onlyでレシート24/24解析済、receipt marker 24/24一意、今回支出ID 5/5一意を確認
- attempt 2はreceipt結果0件で成功し、再解析・再writeなし
### 2026-09-14 銀行最終反映preview

- 取込済み銀行行185件を3銀行共通のread-only経路で最終分類
- 新規支出98 / 収入48 / non-expense 12 / review 19 / duplicate 8 / link 0
- incomeを支出化せず、card settlementと曖昧な金融相手行を自動支出から除外
- stable expense ID、read-back、部分失敗後のreplay修復を実装
- production applyはexact 1 identity / approved target / expected HEADへ限定
- 住宅ローン1件のcanaryを承認済みproduction targetへ反映
- `取込データ`は`auto_expense` / target `M-619abd838394af6ab069af7b`、`支出明細`は103,950円 / 住まい / 住宅ローン / activeとしてread-back成功
- 同一identityのreplay previewはduplicate `already_finalized`、予定write 0件。全体残件は支出97 / 収入48 / non-expense 12 / review 19 / duplicate 9
- 明示承認後、auじぶん4件 / ドコモSMTB 3件 / 千葉3件のwrite-eligible支出だけを、既存の1 identity単位production経路でbounded batch反映
- 全10件で支出1件作成 / 取込1件更新 / read-back一致。needs_review / withheld / ambiguous / 既存duplicateは選択・変更していない
- 同一10 identitiesのreplay previewはduplicate `already_finalized` 10件、予定支出write 0 / 予定取込更新0
- `支出明細`は10行増加し、`取込データ`は対象10行だけstable target付き`auto_expense`へ更新。API再読込とGoogle Sheets表示で既存レイアウト破損なし
- 全体残件は支出87 / non-expense 12 / review 19の118件。追加反映はこのbounded batch承認に含めず、別途明示承認を必要とする
- 続く87件backfillでは、直前previewの87件一致後、各identityを直前preview → 1件apply → read-backの順で処理
- 追加25件（auじぶん14 / ドコモSMTB11）は全件支出作成・取込更新・read-back一致。同一25 identitiesのreplayはduplicate `already_finalized` 25件、予定write 0
- 第6組の開始前に追加production writeが承認境界で拒否されたため停止。第6組は未開始で、残り62件には触れていない
- 停止後previewは支出62 / 収入48 / non-expense 12 / review 19 / duplicate 44。保護対象の件数は不変
- 残り62件への明示承認後、各identityを直前preview → 1件apply → read-backの順で全件反映
- 62件すべてで支出1件作成 / 取込1件更新 / read-back一致。同一62 identitiesのreplayはduplicate 62 / 予定支出write 0 / 予定取込更新0
- 最終全体previewは新規支出0 / 収入48 / non-expense 12 / review 19 / duplicate 106 / link 0。保護対象31件と収入48件は未変更
- `支出明細`は62行増加し、`取込データ`の行数は不変。先頭・末尾identityと対応支出IDは各シートで一意、Google Sheets API再読込と画面表示で反映を確認

### 2026-09-14 確認済み自己口座間transfer整理

- checkpoint `69106432fef990649912103a4ef99bf6147bcf2b`を基点に、review 19件が人間確認済みG1/G2/G3固定集合と完全一致することを再確認
- 既存exact operator rule機構を再利用し、正規化摘要・方向・account aliasの完全一致3条件だけを追加。金融機関名、金額、近接日だけでは判定しないfail-closed境界を維持
- G1 5件、G2 8件、G3 6件以外へのrule一致は0件。production previewは対象19 / 新規支出0 / transfer・non-expense更新19 / その他変更0
- code checkpoint `622b58cabc23f538dbda3f98ed50301a405e239d`で19件をexact 1 identityずつapply。全件で取込更新1 / 支出作成0 / 支出更新0 / read-back一致
- apply後のGoogle Sheets API再読込で19件すべて`bank_non_expense`、統合先空欄、`confirmed_internal_transfer`注記あり。`取込データ`2916行 / `支出明細`1439行で行数不変。G1/G2/G3代表行を画面でも確認し、表示崩れなし
- 同一19 identitiesのreplayはnon-expense 19 / `already_non_expense` 19 / review 0 / 新規支出0 / 予定取込更新0 / 予定支出write 0
- 最終全体previewは取込185 / 新規支出0 / 収入48 / non-expense 31 / review 0 / duplicate 106 / link 0。予定支出write 0 / 予定取込更新12。残る12件は今回対象外のexact card settlementで未変更
- 関連test `63 passed`、full pytest `1088 passed`、compileall / diff-check成功。recurring productionは有効化していない
