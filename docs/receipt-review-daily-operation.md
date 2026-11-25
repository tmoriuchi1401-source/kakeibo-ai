# Phase 2 日常運用の準備と開始条件

本変更はPR #205の固定SHA `b69590d298c36895945327ff1c0f9412e22b5e7e` を基点にした
独立ブランチのコード修正。金融write、原本移動、main merge、Script再配備、継続認証の新規保存、
Windowsタスクの登録・有効化、長期backend/GitHubゲートONは実施しない。

## 登録許可とbackendの区別

既存version 5の `receiptReviewRegistrationEnable` はiPhone登録だけを許可する。
backend受付とGitHubゲートはOFFのままにでき、次の案件にも登録許可が残る。
STOPがある場合は登録も停止する。失敗・応答不明時はEnableを繰り返さない。
停止は `receiptReviewRegistrationDisable`。`receiptReviewStop` はbackendをOFFにし、
Script内のPATコピーを削除するが、登録許可と既存STOP・受付・フェンスを保持する。

## 最小修正

- 日常設定では旧 `max_case_count: 4` を `max_open_case_count: 50` に置き換える。
  数値は1〜50。登録済みの履歴が4件を超えても新規取込を継続できる。
  `case_count - request_count` が未登録案件数。旧限定試験設定も引き続き認識する。
- 1回につき取込候補1件、AI HTTP試行最大1回、受付済みUUIDのbackend検証最大1件。
  backendに2件以上待ちがあれば新規取込より検証を優先する。
  正常な金額保留は確定結果として保存。UNKNOWN・重複判定は運用STOPを残す。
- 通常取込は原本ID・hash・版、保存済み取込履歴、恒久SQLiteフェンスを再利用する。
  backendは固定snapshotの「受付済み」だけを検証。結果済み・UNKNOWNの再送は禁止。
- 受信フォルダは100件ずつ最大10ページを完全読取してから取込する。
  ページ循環・重複ID・範囲超過は副作用前に停止。原本を移動・削除しない。
- 既存Scriptの1,000行境界を維持し、保存案件900件で新規取込を停止する。
  保存履歴を削除・再生成せず、ownerが容量対応を判断する。
- `RUNNING.json` を認証前に排他的・永続保存。PC中断や強制終了で残った場合は
  次回STOPにし、処理を自動再開しない。通常完了だけでRUNNINGを除去する。
- 通常の完了・例外終了ではGitHubゲートOFFと `receiptReviewStop` を各1回試み、
  独立読取でbackend OFF・PATコピーなしを確認する。確認不明はSTOPを保持する。
  登録常時ONを解除する呼出しは追加しない。
- 医療・混在・sensitive unknownを外部AIへ送る許可は追加しない。
  `SheetsDB` と `_materialize_result` を日常runnerで遮断し、金融writeを接続しない。

## 継続OAuth：個別承認が必要

既存Google OAuth clientを使用し、ownerがブラウザで同意する。
要求scopeは `openid`、`userinfo.email`、`spreadsheets`、`drive.readonly`、
`script.scriptapp`、`script.external_request`、`script.projects.readonly`。
Sheets scopeは金融ブックも含む広い権限になり得るが、処理コードは確認ブックと
read-only履歴参照に用途を限定する。Googleのscope自体が金融書込みを禁止するわけではない。
Drive書込み・Script編集・配備権限は要求しない。

新規保存の推奨先は `%LOCALAPPDATA%\KakeiboAI\ReceiptReview\owner.dpapi.json`。
Windows DPAPIの現在のログインユーザーに束縛して暗号化し、refresh tokenを平文保存しない。
既存平文owner.jsonは上書き・移動しない。日常runnerは平文ファイルを拒否する。
今回のDPAPI検証は保存形式・排他性の回帰検証であり、実ownerの継続認証成功ではない。

owner照合・保存が失敗した新規認証は失効を1回試み、失効不明を表示する。
Google側の失効が同一clientの関連grantにも影響する可能性を、認証前の承認事項に含める。
認証の停止はタスク無効化に加え、Googleアカウントの接続済みアプリから当該clientのアクセスを削除する。
ローカルファイル削除だけではGoogle側の権限は失効しない。

## Windowsタスク：個別承認が必要

既存 `install_receipt_review_daily.ps1` を再利用する。タスク名は
`KakeiboAI-ReceiptReview-Limited` のまま。既存タスクがあれば置換しない。
現在のWindowsユーザーの対話ログオン・通常権限で5分間隔。
`StartWhenAvailable`、バッテリー稼働可、同時実行IgnoreNew、再試行設定なし。
強制終了限度を4分から12分に延ばし、通信失敗後のOFF処理時間を確保する。
AI送信は既存60秒上限と残時間フェンスで制限する。
自動サインイン・PC自動起動・新規サーバーは追加しない。

停止例（ownerのPC）：

```powershell
Disable-ScheduledTask -TaskName 'KakeiboAI-ReceiptReview-Limited'
```

続いて既存GitHub変数 `RECEIPT_VALIDATION_ENABLED=false` を確認し、
既存Scriptの `receiptReviewStop` とPATコピー不在を確認する。
緊急強制終了はRUNNINGを残すので、解除して自動再送しない。

## GitHubゲート・PAT：未解決の開始条件

現在は両ゲートOFF・Script内PATコピーなしを維持する。
日常runnerにゲートON・PATコピー・workflow dispatch経路は追加しない。
OFF処理のGitHub認証は既存 `gh` の認証を使い、tokenをログや設定へ書き出さない。
DPAPIはGoogle refresh tokenの保存対策であり、既存PATの権限・期限を改善するものではない。

**自動運用はNO-GO。** 既存version 5で各サイクルを無人開始するにはbackend受付ONと
GitHubゲートON・Script内PATが必要だが、PC電源断時は `finally` やWindows監視タスクが動かない。
通常終了のOFF処理だけでこの安全条件を満たしたと判断しない。
次の内容をownerに示し、方式を決めて検証した後に個別承認を得る必要がある。

1. 最小権限・期限のPATと安全な保管方法。金融write用tokenの流用禁止。
2. PC停止中にも失効する期限付きbackend許可と、Script内PATを残さない方式。
   新しい長期ON・監視タスクだけでは解決にならない。
3. 通常の各サイクルをどう開閉するか。現在のrunnerは自らONにしないので、
   OFFを維持したままタスクだけを有効にしてもbackend検証は開始しない。
4. 変更後SHAのCI確認・owner承認済みコード配置。必要なら既存Scriptへの最小変更と
   配備も別途具体化・承認する。新規環境を作らない。

## 監視・PC停止・復旧

- PC停止前から未処理の原本は受信フォルダ、iPhone登録は既存受付台帳に残る。
  サイクル未開始なら、承認済みタスクがPCログオン後に新鮮な台帳から処理する設計。
  処理中断でRUNNING・STOPがある場合はowner照合後まで停止する。
- `STOP.json`、`RUNNING.json`、`cycle-*.jsonl`、`closed-*.json` をローカルで監視。
  診断には秘密・画像・商品・snapshotを出さない。外部通知送信は追加しない。
- ownerは受付UUID、固定snapshot、結果台帳、GitHubゲート、Script STOP、PAT不在、
  元DBのフェンスを独立読取する。UNKNOWNは再送せず照合対象のまま。
  RUNNING・STOPを消す操作を自動化しない。既存STOPを解除・古いUUIDを再利用しない。
- 認証失効・接続断でOFF処理が確認できなければ、ownerがGoogleとGitHubで権限を止める。
  PC電源断中の失効方式が未実装の現状は、Cを有効化する根拠にならない。

## 実運用開始の確認（未実施）

新しい一般レシート1件で、自動取込→iPhone候補確認・登録→受付済みUUIDの自動検証→
iPhone結果確認を行い、重複・UNKNOWN・金融writeがすべて0であることを確認する。
既存2,194円レシートのPASSを新規案件の自動運用PASSに置き換えない。
ownerのiPhone実測をPCやCIで代用しない。
