# 現金支出の手入力（初期版）

スマホで金額を入力し、必要なら内容・日付・カテゴリ・メモを変更する。支払方法は現金、カテゴリ未選択なら「その他 / 未分類」。大カテゴリを選ぶと、小カテゴリはその配下だけをリスト表示する。受付後に GitHub Actions が正式な「支出明細」に追記して日常表示を更新する。受付から反映までは待ち時間がある。

同じ受付 ID は二度書き込まない。取消は別の受付として実行し、記帳後なら計上状態を `void` にして集計から除外する。原本行は削除しない。元の受付が先に処理されていなければ取消して書き込みを防ぐ。

## 導入

1. この変更を main に反映し、CI が成功した SHA を既存の `KAKEIBO_VALIDATED_MAIN_SHA` に設定する。既存の production 設定、Sheets / Drive のサービスアカウント権限を確認する。
2. 自分専用の standalone Apps Script プロジェクトを作り、`apps-script/manual-entry/Code.gs`、`Index.html`、`appsscript.json` を配置する。`Code.gs` にある `doGet` などの関数を、既存のカテゴリ操作スクリプトと同じファイルに貼り合わせない。
3. スクリプトプロパティ `MANUAL_SPREADSHEET_ID` に Actions の `SPREADSHEET_ID` と同じ台帳の ID を設定する。`MANUAL_GITHUB_TOKEN` にこのリポジトリ限定の fine-grained PAT（Actions: read/write、Metadata: read）を設定する。トークンはシート・Git・チャット・URLに書かない。Apps Script プロジェクトは共有しない。
4. 所有者で `installManualEntry` を一度実行し、Google の Sheets と外部通信の権限を承認する。台帳の「カテゴリ」と「支出明細」を確認し、非表示の `_手入力受付` を作る。このシートを削除・編集しない。
5. ウェブアプリとして新規デプロイし、**実行ユーザー: 自分 / アクセス: 自分のみ**を指定する。URLをiPhoneのホーム画面に追加する。アクセスを「全員」や「Googleアカウントを持つ全員」にしない。
6. まずテストの現金支出を1件登録し、受付、Actions 完了、支出明細への1行追記、日常表示、画面からの取消と `void` を確認する。本番の既存支出と同じ内容を試し入力しない。

Apps Script のコードを更新した場合はウェブアプリのデプロイ版も更新する。新しい main が検証 SHA と異なる間はワーカーのジョブが起動しない。既存の台帳処理と同じ production concurrency group を共有するため、受付はすぐでも反映には時間がかかることがある。

受付シートで `dispatch_failed` は GitHub 接続エラー、`error` は処理中断を示す。`dispatching` が長く続く場合は、対応する UUID の Actions run と validated SHA を確認する。通信結果が不明な受付や一部反映済みのエラーは、同じ支出を再入力せずに台帳の ID `MAN-...` と受付行を照合する。UUID だけを Actions に渡し、入力内容はスプレッドシート内に保持する。
