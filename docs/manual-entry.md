# 現金支出の手入力

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

## 夫婦で同じ画面を使う場合

所有者実行のウェブアプリからは、訪問者のメールアドレスを確実に得られない。別の小さな本人確認用 Apps Script を利用者実行にし、Google が発行する ID トークンをサーバー間で元の書込み用 Apps Script に渡す。入力画面は既存の `Index.html` と同一で、夫婦は本人確認側の一つの URL を使う。本人確認側は Sheets / Drive 権限や GitHub トークンを持たない。妻への管理台帳の書込み権限は不要。

1. 所有者の非共有プロジェクトに `apps-script/manual-identity/Code.gs` と同ディレクトリの manifest、および `apps-script/manual-entry/Index.html` を配置する。`MANUAL_HOUSEHOLD_EMAILS` プロパティに許可した二人の Google メールアドレスを小文字の JSON 配列で保存する。メールアドレスをリポジトリの設定ファイルに記載しない。
2. 所有者で `inspectIdentityAudience` を実行する。Google にメールアドレスと本人確認、外部通信のみを許可する。ログの公開情報 `OAuth audience` を書込み側の `MANUAL_IDENTITY_AUDIENCE` に保存し、同じ `MANUAL_HOUSEHOLD_EMAILS` を書込み側にも設定する。トークンは表示しない。
3. 書込み側の既存プロジェクトに更新版 `Code.gs` / manifest を配置する。既存の台帳 ID と GitHub PAT は変更しない。新しいデプロイを **実行ユーザー: 自分 / アクセス: 全員**として作る。このデプロイは全リクエストで Google に ID トークンの真正性を照会し、audience、issuer、有効期限、verified email、二人の許可リストを確認する。匿名・偽造・別 OAuth audience は台帳を読み書きする前に拒否する。公開 RPC と GET は所有者の本人確認を必須にする。元の「自分のみ」のデプロイは切替と検証が終わるまで維持する。
4. 上記のサーバー URL を本人確認側の `MANUAL_BACKEND_URL` に保存する。本人確認側を **実行ユーザー: ウェブアプリにアクセスしているユーザー / アクセス: Google アカウントを持つ全員**としてデプロイする。許可した二人以外は利用できない。両プロジェクトの編集権限は所有者のみ。
5. 夫婦がそれぞれ初回の Google 本人確認を許可する。スマホ、カテゴリ、登録、反映、同じ UUID の再送、取消を検証してから日常画面のリンクを置き換える。旧デプロイを更新するだけでは、古い URL が新しい構成に転送されることはない。

受付 JSON に `entered_by`、`created_at`、`manual_entry_id` をサーバーで付加する。入力者は検証済み Google メールアドレスに限り、画面から送られた入力者名を信用しない。受付の6列、UUID、既存ワーカー、支出 ID と取消方式は変わらない。旧 JSON もそのまま処理できる。現在の UI は現金支出用であり、収入入力は追加しない。

切替の rollback は、日常リンクを旧所有者用 URL に戻し、新しい二つのデプロイをアーカイブする。旧版のデプロイは保存済みのバージョンを指定できる。秘密鍵や PAT を新規発行・拡張する必要はない。

ローカル検証: `node --test apps-script/manual-entry/Code.test.cjs tests/manual_identity.test.cjs` と `python -m pytest tests/test_manual_entry.py`。
