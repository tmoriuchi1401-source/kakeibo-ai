# R1 本人確定UI：隔離合成環境

対象は画像一般と画像医療完全手入力。正式なGoogle Sheets「確認」1タブを模した**既存host内の隔離検証用表示**であり、常用Web画面・実Sheetsへの切替ではない。mainのAGENTS.md・継続仕様全体（特に第10節）とPR #123〜#145を基準とする。

基点は #145 `8658d5a1614d62e18d4c59e5eb08dfa008efac3f`。新Draftは [#146](https://github.com/tmoriuchi1401-source/kakeibo-ai/pull/146)、baseは `codex/iphone-v3-guarded-writer`。開始時のmain／23 PRのhead/base/SHA/Draft/stateは [baseline](iphone-r1-ui-pr-baseline.json) に保存。既存PR・mainの更新、merge/rebase/cherry-pickは行わない。

## 接続した操作

確認1タブ → 案件ID検索 → 別画面の合成PNG原本と往復 → 列内の入力・明細修正 → 下書き保存 → 保存済み修正前後の比較 → チェック＋「この内容で記帳」 → #145の実writer本体 → read-only結果照会。

- 1案件1行、列方向・横スクロール、見出しと案件ID列を固定。長い行の縦移動でも案件IDの文字を固定する。並べ替え機能は追加しない。入力はIDにbindingし、行番号をidentityに使わない。
- 採用済み列の順序を維持し、一般の固定明細catalogをカテゴリ欄内に表示する。明細追加・削除・並べ替え・分割・結合は拒否する。実Spreadsheetの列・幅・保護・入力規則を変更する処理はない。
- G103/H301、既存の対象外G102/M102・PDF・対応不明の旧医療p1は表示・保留。既存合成下書きの入力・途中メモを残し、原本対応がある保留は原本導線を残す。PDF金融writerには接続しない。
- 一般は保存baselineの70＋50円から80＋40円へ。医療は保存baseline120円と比較し、**編集欄を空で開始**して本人が135円等を完全手入力する。古いmedical parsedを入力欄へ補完しない。
- 原本PNGはテスト内で固定文字列から生成する。原本ID/hashを含むnative fixtureの初期定義とtrusted bytes portを、下書き・native lineageの保存**前**に一度だけ構成する。その後のbaselineを書き換えて成功させる処理はない。原本GETはそのbytesのhashを照合して表示する。
- 一般・医療は異なる原本IDを持ち、**同じ共有CAS backend・全表合成台帳・カテゴリmaster port**を使う。別々のCASを共有済みと扱わない。各operationのnative/shared/ledgerは独立した保存先であり、単一transactionではない。

## 変更ファイルと関数

| 接続点 | 役割 |
|---|---|
| `app/iphone_item_drafts.py:image_issue/image_inspect` | 画像whole-source専用。既存positional expense ID・catalogを固定。既存ReceiptResult/receipt validator/医療カテゴリを再利用。PDF Unit/index/partitionの流用なし |
| `staging_shadow/iphone_confirmation_ui.py:OneTabConfirmation.read/proof/post/inspect` | 保存draft・比較proof・既存host attestation・native v3・#145 writerをつなぐadapter |
| 同 `DraftCAS` | versioned memory draft IOを既存`ConditionalRequests.replace`へ適合。新CAS engine/DBなし |
| `app/iphone_confirmation_host.py:make_isolated_confirmation_application(one_tab=...)` | 既定OFFの既存factoryへの明示注入。隔離設定・memory runtime・既存認証が揃った場合だけ合成routeを登録 |
| `app/unified_shadow_templates/iphone_one_tab*` | 1タブ検証用DOM、原本PNG表示、未保存ガード、比較、明示操作、結果・応答不明の表示 |
| `tests/iphone_r1_fixtures.py` | 合成PNG・baseline・異なる原本ID・共通CAS/ledger/master・既存署名付き合成ログインを構成 |
| `tests/test_iphone_r1_adapter.py` | 実writer・2/16 instance・native/shared/ledger CAS応答喪失/412・crash・再構築・snapshot不変性 |
| `tests/test_iphone_r1_confirmation.py` | 実Flask hostのsession/CSRF/Origin、表示後draft/source/master/ledger変更、部分入力・保留 |
| `tests/test_iphone_r1_browser.py` | 実ChromiumのDOM・入力・クリック・popup・再読込み・二重タップ・通信断・再認証 |
| `.github/workflows/iphone-r1-confirmation-checks.yml` | 専用Ubuntu 24.04/Python 3.12。PR専用・contents:read・write flag false・資格情報なし |

旧`ConfirmationService`の`save_draft/acknowledge_snapshot`と旧templateは維持する。`acknowledge_snapshot`は非金融のまま。既存writer/旧validator/authority schema/native v3/共有ownership engine/auth-session bundle/production constructor/runner/workflowには変更を加えない。

既存hostには小さい認証フォーム用16KB制限とno-script CSPがある。比較proofを運ぶ**合成専用POSTだけ**800KBまでのbounded bodyを許し、合成namespaceだけに隔離CSP（self script/connect/img）を最後に付ける。実金融POSTやproductionの上限・CSPを変更しない。原本routeにも既存認証とno-storeを適用する。

## 保存・表示・金融確定のbinding

1. 下書き保存はDRAFT_ONLY portへの既存CASだけ。owner/case/source/catalog/provenance/カテゴリ/型を検証し、revisionを増やして全値のdigest・strong tagを読戻す。原本revision bytes/hashもtrusted readerから再照合する。native qualification・共有claim・authority evidence・writerは0。
2. 比較GETは`prepare_inputs`を再利用。immutable baseline、保存draftの全値/revision/generation/digest/strong tag、原本bytes/hash/Drive版/独立native版、カテゴリmaster版、全ledger値/数式、original/proposed parsedとplan、ownershipを二重readする。GETはqualification/CAS/writeを行わない。
3. 表示proofは既存sessionのactor、session cookie/request、600秒TTL、case、ACCOUNTING_POST、保存draftと上記snapshot全体へ暗号学的にbindingする。request IDはserver claimsから決定し、再取得で無関係な金融operationを作らない。
4. 明示POSTでは既存`ExistingHostSessionGate`がGoogle actor/owner/session/Origin/CSRFを検証し、`VerifiedPost`を発行する。browserからauthorityを受け取らない。saved draftと表示snapshotを再導出し、全値・版・tagが違えば再確認を要求する。
5. native inputのexact digest/request ID/revision=0へ既存`PurposeBoundary`を接続する。合成専用の既存FIXTURE evidenceをmemory portへ置き、native v3 validatorで独立再検証する。実authorityへの昇格はない。
6. native request fingerprintはdraftを含む全inputs、baseline/source/master/before/plan/ownership、本人attestation・request ID・期限を含む。native consume/start/completionと共有CASはこのfingerprintに結合する。

JSON上のtuple/list表現をcanonicalに揃える以外、入力値のtrim・日付変換・差額調整・カテゴリ変更は行わない。既存医療parserが日付・施設名を変換する入力は、表示proof作成前に拒否し、本人の明示編集を要求する。

保存直前のproof取得で別操作の最新draftを暗黙に採用しない。画面に表示した全値/revision/strong tagとproofを比較し、別端末・別操作が先に保存した場合は送信前に拒否し、入力中の値・メモを保持する。成功時はexact read-backのrevision/tagを画面へ返し、送信中の追加編集も残す。POST後の並行更新は既存CASで拒否する。

## writer・結果

#145の`SyntheticDraftConfirmation`と既存`_MemoryLedger/_GuardedConfirmation`をそのまま利用する。exact snapshot検証 → native qualification → 共有CAS/whole-source claim → native pending＋consume同一CAS → 原本/draft/master/ledger再検証 → writer_started CASと完全read-back → 一度限りlease → 既存writer本体 → 全表独立read-back → native completion → 共有awaiting_readback/complete照合 → read-only inspect。

一般は既存receipt・expense01を更新し、固定expense02とimportを追加する。医療は完全手入力からreceipt/expense/importを追加する。修正日付・店舗/施設・カテゴリ・支払方法、全ID/値/数式/順序・既存label・無関係行を比較する。メモは確定snapshotに保持し、既存writerが保存しない欄に新しい会計仕様を追加しない。

| 表示 | 根拠・動作 |
|---|---|
| 下書き保存済み | draft CASとexact read-back。金融完了ではない |
| 確定前の再確認が必要 | 未入力/表示後変更/原本・master・before変更。新鮮な比較が必要 |
| 合成検証での記帳成功 | native completion・共有complete・全ledger値/式/read-backが一致した既存inspectのみ |
| 保留 | G103/H301/旧医療p1/PDF/対象外。確定操作なし |
| PARTIAL：部分反映・要照合 | 一部ledger反映。監査・照合待ち |
| UNKNOWN：結果不明・要照合 | 開始/completion/CAS/read-back/通信の証拠不足。監査・照合待ち |
| 競合または期限切れによる拒否 | owner/session/Origin/CSRF/期限/競合の拒否。入力保持 |

受付成功やwriter戻り値だけで成功表示しない。PARTIAL/UNKNOWNは再確定・自動retry・追加append・補完・pending/fence解除をしない。結果GETは既存inspectだけを使い、qualification・再dispatch・完了書込み・台帳修復をしない。画面再読込み後にも保存requestの結果リンクと停止状態を表示する。

記帳済みrequest/snapshotはimmutable。後からdraftが更新されても書き換えない。現在draft/ledgerが変わった場合、既存の厳密inspectはUNKNOWNを返し得るが、結果画面は当時の保存snapshotを表示する。過去の全行一致だけを根拠に現在のCOMPLETEへ昇格しない。

native qualification前に応答を失っても、既存の合成authority evidence portに残る試行を上書きしない。再構築時にnative requestがなくevidenceだけがある場合はUNKNOWNとして表示し、新しい比較proof/再確定を拒否する。POSTがserverへ届く前の通信断はserverが観測できないため、送信したタブはcase ID/request IDだけをsessionStorageに保持し、再読込み後も照合待ちにする。入力値・医療情報・proofは保存しない。このbrowser markerは表示制御のみで、認証や金融許可には使用しない。server側のCAS/native/evidence fenceが独立した安全境界である。

## 検証証拠と範囲

最終headのCIリンク・確定件数・成否はPR #146の本文で報告する。試験を定義・開始しただけで合格としない。

- 新adapter/host/browserのcollectと個別PASSED node IDを照合する。#145の342件、R0/設計/runtimeの473件、既存安全selectorの2744件も専用Ubuntu CIで実行する。旧証拠を今回の新UI試験件数に数えない。
- PC 1365px/390px：ID検索、PNG原本popupと往復、一般明細/カテゴリ等の編集、医療空欄からの完全手入力・部分保存、再読込み保持、前後比較、明示チェックとクリック、二重クリック、結果GET、応答喪失後の照会、timeout時の入力保持を実イベントで検証する。期限切れ時の入力保持・保存再開はDOM操作、再認証は既存hostの合成start/callback handlerを実行する。Googleの実ログイン画面操作は含まない。
- Chromiumの各HTTP requestはin-process bridgeで**実Flask handler**へ渡す。POST成功responseの捏造やwriter mock成功は使わない。ネットワーク公開listener・実OAuth・実Google呼出しはない。PNGのnaturalWidth、sticky IDのviewport内表示も検証する。
- `.private/r1-browser/`の画像・request trace/viewport JSONはCI artifact `r1-synthetic-browser-evidence`へ保存する。全て架空データであり、実原本・資格情報・監査データは含まない。
- 新adapterで2/16独立instance、各CAS応答喪失/412、writer途中停止、全行後completion失敗、native完了後共有完了前停止、再構築read-only、後からのdraft変更を検証。dispatchの実bodyを監査しoperationごと最大1回とする。
- 通常取込whole-source/PDF Unit混同・旧pending/applied/UNKNOWN・AI/OCR/旧医療拒否などの全詳細matrixは既存#145/安全回帰を維持して実行する。全てをブラウザーから再現したと主張しない。
- Windowsでは既存auth-session固定bundleのhashが一致しないため、host/browserの合否はhash条件を緩和せずUbuntuで判定する。ローカルadapter・既存writer試験と区別する。

## 未実施・R2/R3

iPhone Safari/Sheetsアプリの実機受入、実Sheets raw readerとの接続、実Drive原本、実OAuth、追加ユーザーの実認可は未実施。PC/390pxの合格は実機合格ではない。

R2には全旧runnerのmandatory gate・並行入口のdrain/運用確認が残る。R3には実host金融provider、実backendのACL/CAS/耐久性/監査/鍵/保存・復旧、live原本/ledger reader、実認証を伴う総合検証が残る。新DB/CAS engine/auth方式で置換しない。実履歴移行・旧pending解除・PDF金融接続・限定canary・本番有効化・Cloud/deploy・main統合はいずれも本作業に含めない。

本番Spreadsheet/既存検証ブック・実Drive/Firestore/OAuth/医療情報アクセス、新Drive資源、実金融authority/会計write、原本移動、AI送信、Cloud/deploy、production切替は0。本Draftの成功範囲は隔離合成の本人確認操作から結果照会までに限る。
