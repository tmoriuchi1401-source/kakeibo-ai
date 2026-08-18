# 次回作業メモ

最終更新: 2026-08-18 (UTC)

## 現在地

- ブランチ: `main`
- 最新コミット: `d679f76` (`Add long-term maintenance and retention`)
- 作業ツリーはクリーン。
- iPhone の Google Drive アプリが生成するスキャン PDF を、レシート画像と同じ取込フローで処理できる状態
- 正常取込、要確認、既取込の画像/PDFは `receipt_processed` へ移動する

## 確認済み

2026-08-18 に以下を実行し、全テストが成功した。

```bash
.venv/bin/pytest -q
# 60 passed in 1.17s
```

## 次の工程

README の「次の実装」に沿って、次は以下を進める。

1. 「店舗」シートの店舗名から標準店舗名への別名解決を照合処理へ追加済み（未コミット）
2. 実運用データを使って要確認判定を調整する

実運用の「取込データ」は最終確認時点で384件。2026-08-17 に追加した1,148円のレシート（`DCM 鎌取店`）が、同日・同額の未分類au PAY（`DCM鎌取店`）と一意に一致し、`matched_receipt` として正常統合された。空白差は既存の正規化で吸収できたため追加ルールは不要だった。「店舗」シートは引き続き空。次は正規化だけでは一致しない表記揺れ、または誤候補の実例が出た時点で店舗マスタへ追加する。

6〜8月の `auPAY_YYYYMM.csv` と `auPAY_Card_YYYYMM.csv` を取込済み。au PAY CSV専用取込を追加し、既存メール行との同日・同店舗・同額の重複を件数単位で除外する。取込後は全384取引となり、6月19日の165円（デイリーヤマザキ表記差）をレシートへ正常統合した。`amazon_needs_review` が要確認シートから漏れる不具合も修正し、現在は高17件・中161件の計178件。

au PAYは「3時間ごとのGmail速報取込 → 月1回のCSV確定」というハイブリッド運用に対応。月次CSVで既存メール行と一致した場合は再登録せず、備考へ `CSV確認済=YYYYMM` を記録する。既存の7月3件・8月12件にも確認済みマーカーを反映済み。カードは月次CSVを正本とする。

要確認シートのカテゴリ選択は、モバイルアプリでも安定する `大カテゴリ｜小カテゴリ` の一体型ドロップダウンへ変更した。従来の大・小カテゴリ個別入力も反映処理では引き続き受け付ける。2026-08-18 に実シートへ反映済み。

Amazonカード照合は、商品単価ではなく注文ID単位の合計金額で判定するよう修正した。既存30件を再判定し、一意一致16件・複数候補1件・注文履歴なし13件となった。候補なしは `amazon_unmatched` として中優先度へ分離した。複数候補1件は2026-06-27注文へ手動統合済み。現在の要確認は高0件、中155件、計155件。

長期運用向けに、照合対象を直近6か月・全未処理・最近取り込んだ過去日付データ・関連する過去候補へ限定し、金額インデックスで総当たりを削減した。要確認の入力規則と支出一覧の日付書式にあった1,000行・10,000行の固定上限も撤廃した。

月次保守としてSpreadsheetコピーと処理済みレシートの1年後完全削除を追加した。`receipt_processed/kakeibo_backup` フォルダは作成済み。サービスアカウントにはDrive保存容量がないため、バックアップ実行には本人OAuthの `GOOGLE_DRIVE_BACKUP_TOKEN_JSON` が必要。GitHubへのSecret登録は未完了。削除プレビュー時点の期限切れファイルは0件。

家計簿の年別・月別・カテゴリ別ダッシュボードは未実装。ただし確定データの「支出一覧」から独立して後付け可能なため、取込・照合の安定化後に実装する方針。

次回の優先候補:

1. Google Cloudの復旧後、Driveバックアップ用OAuthを完了する（下記参照）。
2. 月次Workflowをリモートへ反映し、手動実行でバックアップと削除処理を検証する。
3. 中155件はレシート・Amazon注文履歴の有無を確認し、正規化で一致しない店舗表記があれば「店舗」マスタへ登録する。
4. 次月以降はGmail速報取込を継続し、月1回 `auPAY_YYYYMM.csv` と `auPAY_Card_YYYYMM.csv` で確定する。

## DriveバックアップOAuthの再開地点

Google Cloudの調子が悪かったため、Google Auth Platformの `Audience` 設定で中断した。
コード実装、専用フォルダ作成、ローカル設定までは完了している。

- バックアップフォルダ: `receipt_processed/kakeibo_backup`
- フォルダID: `1GtC7iPw4YA_e4UnajAd7Fw9yryCc7Rxf`
- サービスアカウントによるコピーは `storageQuotaExceeded` になることを確認済み。
- 本人のDrive OAuthトークンを使う方式へ変更済み。
- GitHub CLIの認証トークンが無効なため、Secretsは未登録。
- 月次Workflow: `.github/workflows/monthly-maintenance.yml`

再開時は、手元のPCでGoogle CloudのDesktop app用OAuthクライアントJSONを取得する。
Google Auth PlatformのAudienceは通常の個人Gmailなら `External` とし、自分をテストユーザーへ追加する。
その後、ローカルPCへリポジトリをclone/pullして次を実行する。

```bash
python -m app.cli drive-backup-authorize "/path/to/client_secret.json" drive-backup-token.json
```

ローカルで `SPREADSHEET_ID` と `BACKUP_DRIVE_FOLDER_ID` を設定して確認する。

```bash
python -m app.cli backup
```

成功後、GitHub ActionsのRepository Secretsへ以下を登録する。

- `BACKUP_DRIVE_FOLDER_ID`: 上記フォルダID
- `GOOGLE_DRIVE_BACKUP_TOKEN_JSON`: `drive-backup-token.json` の全文

最後に `Monthly backup and receipt retention` を手動実行する。バックアップ用トークンや
クライアントJSONはコミットしない。処理済みレシートの期限切れプレビュー・実行経路は
確認済みで、2026-08-18時点の対象は0件（削除0件）。

## 再開時の手順

```bash
cd /workspaces/kakeibo-ai
git status --short
git log -1 --oneline
.venv/bin/pytest -q
```

その後、主に以下を確認する。

- `app/reconciliation.py`: 金額・日付・店舗による照合ロジック
- `app/aupay_csv_pipeline.py`: au PAY月次CSVの取込、メール行との照合、CSV確認済み記録
- `app/review_pipeline.py`: Amazon曖昧候補を含む要確認抽出と手動反映
- `app/sheets.py`: モバイル向け一体型カテゴリ入力規則
- `app/maintenance.py`: 月次Spreadsheetバックアップと1年経過レシートの完全削除
- `tests/test_reconciliation.py`: 現在の照合仕様と追加すべき回帰テスト
- `config/categories.tsv`: カテゴリマスタ（店舗名マスタとは別物なので混同しない）
- `README.md`: 現在の運用手順と「次の実装」

## 注意事項

- `.env`、`service-account.json`、Gmail OAuthトークンなどの秘密情報はコミットしない。
- 実運用データをテストへ追加する場合は、氏名、住所、注文番号、伝票番号などを匿名化する。
- 重複候補が曖昧な場合は自動統合せず、従来どおり `needs_review_duplicate` とする。
- レシートや既存行は監査用データなので、照合ルール変更時も削除しない。
