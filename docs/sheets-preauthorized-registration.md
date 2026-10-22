# Sheets A方式：事前許可による登録（合成実装）

2026-10-22のユーザー方針変更。#157最終head `ade63481fedd6c12bb12f7437636ffa2dc24e7dc` 基点。
取引ごとの比較・本人確認POST・Google再認証・CSRF付き承認を、新しい日常経路では要求しない。
旧方式のコード・本番gateは保持する。これは本番承認gateの削除や切替ではない。

## 権限モデルと成立条件

**技術的には、信頼済み入力元への事前許可に基づくbackend実行として成立し得る。セル編集者の本人承認を成立させる方式ではない。**
初期設定の管理者Googleアカウントと利用権限を検証し、許可された入力元・処理種別・保存先・実行主体・writerをbackendで固定する。
このモデルでは、許可された共有編集者全員からの入力を受け入れることになる。共有ACLや入力元が未検証・変化したときは停止する。
個々の編集者を識別できないSheetsセルを、本人identity、金融authority、設定の正本にしない。

現在は **完全合成のみ**。既存 `ExistingHostSessionGate.current` の署名付きGoogle login/owner bindingを初期設定で一度検証する。
独立したmemory policy IOにある `permissions_verified`・ACL digestは**合成fixtureの証拠**であり、実Google ACL/IAM調査や本番許可の代用ではない。
初期設定を行うための新UI・認証基盤・HTTP route・DBは追加していない。
初期login/sessionが期限切れになっても、日常登録はsession/POST/CSRFを再利用しない。
worker再構築も既に検証した合成permission objectを再利用する。本番の永続許可復元・署名・失効・executor認証は未実装。

backend構成として渡したruntimeのowner/workbook/executor/destination/writerとpolicyを一致させる。
案件ごとにentry/source ID/kind/上限金額を固定し、policyの強い版・bytes・ACL digest・期間を毎回再読取りする。
設定時のpolicy observationと異なれば失効扱い。実行途中の変更も停止する。設定更新は再設定の別操作であり、取引ごとの再認証ではない。
Sheetの行番号、注記、操作セル、結果セルは権限証拠にならない。

## 日常操作と表示

正式「確認」1タブ、1行1案件・固定ID・原本Driveリンク・入力・メモ・保留を維持する。
合成投影のID隣は「操作依頼」「登録・結果」。旧比較モードも残すが、新経路の日常導線では使わない。

1. 「入力中」のまま日付・店舗/施設・実支払額・カテゴリ等を入力する。医療は完全手入力。一般複数明細は独立した既存明細入力だけを使い、総額カテゴリから推測配賦しない。
2. 原本と入力を確認し、同期が済んでから標準プルダウンの「登録 1」（保留修正後は次の番号）を選ぶ。別の比較・確定操作やSafari遷移は不要。
3. 同じ場所の受付待ち→結果を見る。入力不足は「保留」、不一致は「競合」、許可不足は「権限未検証」、不明は「UNKNOWN」を区別する。
4. 今回の正常到達点は **「登録検証済み・未記帳」**。記帳完了ではない。既存writerへの新権限契約は未接続であり、結果照会だけとする。
5. UNKNOWNは登録番号を変えて再送しない。backendは保存結果の照会だけを行い、原本/金融状態の照合が必要なら運用者が解決する。

「登録 n」は重複・順序の識別子であって権限ではない。同じ原本の受付済み結果を別番号で再登録しない。
訂正/取消/再記帳は未実装の別ライフサイクルであり、このUIで新しい入力を既存完了結果に混ぜない。

## 実装と再利用

- `staging_shadow/standing_permission.py`：初期Google owner検証、独立backend policyのfresh検証。一般手入力/医療/現金/レシート自動/銀行/PayPay/Amazonの許可範囲を明示。自動経路はtrusted native adapterのeligible判断がなければ拒否。
- `staging_shadow/sheets_registration.py`：#157 polling/intent lease/immutable receipt/UNKNOWN、既存暗号化journalとDraftCASを継承。別schemaのdetails slotを使い、新DBは作らない。旧#155と#157の未解決intent/UNKNOWNもfenceにする。
- #155 `_capture` のfresh Sheet/Drive revision bytes/hash/native版・案件ID・入力snapshotの再照合と、既存native validator/read-only記帳計画を再利用する。
- 初回と3秒後以降のpollでcommandと全体snapshotが一致してから受付。worker再構築後は再観測する。onEdit欠落/重複/順序逆転に依存しない。
- Sheet側IF/AND/EXACT式で受付待ち/入力変更を表示。比較・本人確定用tokenを新UIに出さない。元17列を逆投影して既存RawReaderへ渡し、安全assertを維持する。
- 結果publisherの送信前intentも同じjournalへ保存。応答を失ったSheet writeはworker再構築後も自動再送せず、式/規則が完全一致するread-backだけ回復扱い。表示の応答不明は金融実行のUNKNOWNと混同せず、保存済み登録結果を維持する。
- 本番の `PurposeBoundary` / `VerifiedPost` / guarded writer / native authorityは変更しない。standing policyを偽のsessionや本人承認に変換しない。新経路のwriter/authority/AI/原本移動は0。

3秒の安定観測は遅延同期の一部を検出するためで、端末内の未同期編集がないことの証明ではない。
実Sheets transportの同期契約・受付版を検証しないまま金融接続しない。
結果セル/表示保護も安全境界ではない。金融側の正本はCAS/journalとexact ledger read-backになる。

## 対象別の現在地

| 入力元 | 新許可モデル | 今回の接続 |
|---|---|---|
| 通常レシートのSheet修正入力 | source/kind/上限の事前許可 | 合成8案件内の既存画像adapter→native計画→未記帳結果まで |
| 医療手入力 | 完全手入力・AI禁止・同じ版照合 | 部分入力保存/保留、完成入力のnative計画→未記帳結果まで |
| 現金手入力 | `cash_manual`明示scope | 許可境界テストまで。既存 `manual_entry._payload/execute` は保持。原本なし固定identity・受付CAS/重複・exact read-backの接続は未完了 |
| 通常レシート/銀行/PayPay/Amazonの自動取込 | 対応entryとtrusted nativeのeligibleが必要。不明/矛盾/重複疑いは拒否 | 許可境界テストまで。既存判定/取込/writer回帰を実行。新Sheet workerには未接続、既存定期処理はNO-GO |
| PDF/旧保留 | 構造/identity不確定・旧保留を継続 | 入力/メモを保持し、登録を拒否 |

汎用entryを定義したことは、各入力元の自動記帳を完成させたことを意味しない。
通常レシートの自動判定もSheetの選択値から作らず、既存pipelineの判断を独立に観測する必要がある。

## 検証とiPhone実機

合成テスト：入力→単一登録→権限/原本/native検証→未記帳結果、再読込み、session期限切れ後、権限/設定変更、同時2/16 worker、原本差替え/版/別案件/カテゴリ競合、部分医療、PDF/旧保留、通信断、UNKNOWN保持・自動再送0、旧モードのUNKNOWN fence。
旧承認付きguarded writerのテストも実行し、正確な記帳後read-back/旧gateの拒否条件を維持する。新経路で実writeの成功を主張しない。
PC/390pxはnative fixture値の静的表示を既存の外部通信遮断Chromiumで検証する。Google Sheetsアプリそのものではない。
Ubuntu CIはPR headをcheckoutし、read-only token・production write OFF・依存導入後loopback-only netnsでブラウザーを実行する。

**実iPhone・実Google transport・実Sheets再計算は未確認。** 将来、別承認で専用合成ブックへ接続できたときの操作確認：

1. 「確認」で案件IDを検索し、一般/医療の原本を往復して入力/メモが残ることを確認する。
2. 入力中は登録されないこと、同期後「登録 n」の1操作だけで受付待ちと対応する案件/版の結果が見えることを確認する。
3. アプリの戻る・再起動・通信断/復帰後も入力と受付番号が残り、二重登録しないことを確認する。
4. 保留医療の追記、PDF/旧保留の拒否、UNKNOWN時に新しい登録を促さない表示を確認する。
5. 固定IDと見出し、横スクロール時の案件対応、縮小表示保持、プルダウンの誤選択/連続タップを確認する。

今回、既存SheetsやDrive資源の作成・変更は行わない。

## 本番までの最小工程（別承認）

1. 実Googleアカウント・利用権限・共有編集者・入力元ACLと、固定executor/sink/writerの初期検証・失効/再設定契約を実装する。外部の任意編集者からの入力を許容しない。
2. 実backendの強い耐久CAS、受付版/同期、旧writerとの共有排他、pending/UNKNOWN照合、原本なし現金のidentityを解決する。
3. 事前許可に対応する既存native/writerへの権限契約を追加して独立検証する。既存本人承認gateを削除せず、権限外writer拒否・exact ledger read-back・重複/replayを同じ境界で保証する。自動取込各native adapterをその共有排他に接続する。
4. 別承認の合成Google接続とiPhone実機試験。その後に範囲限定canary・有効化・main mergeを個別承認する。

従来4処理・新R1のNO-GO、実backend CAS・旧writer競合排除・pending照合の未解決事項は維持する。
本番金融write・authority発行・Cloud/IAM・Variables/Secrets/validated SHA・原本移動・AI送信・main変更なし。
