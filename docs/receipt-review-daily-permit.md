# PR #206 電源断対策のコード準備（2026-11-26）

基準SHAは `952d64663756bdac46e0894d108ab00c3f64220a`。
owner承認はコードとオフライン検証のみ。**判定はNO-GO**。
受信側の期限付き許可カーネルは実装したが、信頼できるPAT非中継ゲート取得方式が未実装。
実入口 `receiptReviewDailyBackend` と日常runnerは固定エラーで拒否し、認証情報を読まず停止する。
未検証の中継をONにする設定フラグは設けない。

## 実装と検証の範囲

`DailyPermit.gs` の非公開関数を、既存 `Review.gs` のコマンド・writerに接続してオフライン検証する。
テストのゲート取得器、Script Properties、Sheets、時計はすべて偽物。
それらを実サービスの証拠として扱わない。公開入口から当該カーネルには到達できない。

| 要件 | 実装・オフライン証拠 | 実接続の判定 |
|---|---|---|
| 最大5分 | 期限差1〜300,000ms、期限到達時から拒否 | 判定ロジックのみPASS |
| ID・SHA・Script・確認ブック | 厳密なキー集合、UUID、固定binding、canonical JSON | 固定bindingの実配備は未実施 |
| 時計・再利用 | 未来の開始、時刻逆行、古い開始、使用済みID、順序違い、inflightを拒否 | ロジックのみPASS |
| 各RPC・副作用直前 | 各port呼出し、writer／装飾の直前・直後に受信側時計を再確認 | 実サービスは未実施 |
| 残時間 | 新AIに180秒、intakeに120秒、claim/finishに60秒、読取/closeに15秒 | ロジックのみPASS |
| 電源断後の失効 | PC側の後始末を実行せず、受信側を再読込して期限切れを拒否 | 実配備の保証は未検証 |
| 再送禁止 | ローカルRUNNING/STOP、既存DBへのID・RPC・AIフェンス追記 | 既存実DBは読書きしない |
| JSONと旧ゲート | 新受信側は文字列true/falseを許可と解釈せず、旧Script/worker/workflowはJSONをONと解釈しない | ゲート実変更なし |
| iPhone・履歴 | 旧登録経路を維持。既存の固定snapshot／UUIDとUIを用いた回帰 | 実ブック・実iPhoneは操作しない |
| 医療・金融 | 元privacy gate、単発送信、金融write遮断を変更しない | 金融write OFF |
| 自コードのPAT非保存・非出力 | PAT引数・ネットワーク取得器を設けない。秘密キー名／PAT形式の混入を拒否。例外を固定コード化 | 設計と負の試験はPASS |
| Google基盤を含むPAT非露出 | オフラインでは証明できない | **未達。中継方式を停止、NO-GO** |

## 期限付きJSON

秘密を含まない次のキーだけを認識する。

```json
{"expires_at_ms":1800000300000,"issued_at_ms":1800000000000,"permit_id":"11111111-1111-4111-8111-111111111111","review_spreadsheet_id":"synthetic-sheet-b","schema":"receipt-daily-permit-v1","script_id":"synthetic-script-b","worker_sha":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}
```

上記は偽の値。SHAは受信側で信頼された固定bindingと一致することが必要。
呼出し側が送ったゲート文字列だけを信頼せず、内部の信頼できる取得器の値と一致させる。
その実取得器が未解決のため、公開入口は拒否する。署名なしの呼出し側自己申告で代用しない。

IDは複数RPCを持つ1サイクル用。各RPCは単調増加sequenceを持ち、一度ずつ消費する。
IDの再開・延長・再利用はしない。最大32 RPC。閉じたIDのフェンスも削除しない。
受信側は非秘密の使用済みID・inflight・時刻を保持し、長期enabledフラグをONにしない。
電源断・強制終了でactive/inflightが残った場合、新IDでの自動置換も拒否する。
保存容量の上限に達した場合の自動消去・ローテーションは実装しない。容量異常も停止対象。

期限は新しいRPC・副作用を開始できる最終時刻であり、既に送ったSheets要求の取消を意味しない。
期限前に開始した要求が期限後に確定する可能性は、UNKNOWNと固定snapshotの照合で扱う。
後始末の応答不明時に同じ操作を再送しない。

## PATの評価と停止理由

実PATは作成・保存・読取・送信していない。合成canaryのみを試験に使う。
自コードにPAT保存先、PATをGoogleに送るHTTPアダプター、Logger/console/Cacheの使用は追加しない。
非公開カーネルには認証済みowner・ScriptLock・信頼できるゲート取得器が必要。
これらの実運用接続まで完成したと見なさない。

Google公式の実行API仕様はパラメーター送信を説明し、ログ仕様は永続ログや未処理例外の記録を説明している。
これらから、API引数として渡すPATが基盤の全障害経路で非露出になるという保証は確認できない。
これは「GoogleがPATを必ず記録する」という主張ではない。
[実行API](https://developers.google.com/apps-script/api/how-tos/execute)、
[ログ仕様](https://developers.google.com/apps-script/guides/logging)。

## PATをGoogleに渡さない代替案（未実装）

既存PCだけでGitHubゲートを確認し、許可ID・期限・SHA・Script・ブックを含む非秘密の許可へ、
用途専用のHMACで認証を付ける。受信側は[Apps Scriptの標準HMAC機能](https://developers.google.com/apps-script/reference/utilities/utilities#computehmacsha256signaturevalue,-key)で照合し、同じ期限・再利用フェンスを適用する。
PATはPCからGitHubへの接続だけで使用し、Googleへ送らない。

この案では、受信側の「GitHubへの直接確認」を「認証されたPCの証明」へ変更する。
独立確認と同一の安全境界とは主張しない。専用鍵の作成・保管・失効・定数時間照合、PC侵害時の扱い、
ゲート変更との競合・期限・再送の検証とownerの別判断が必要。鍵や新規環境は今回作成しない。
本案を勝手に代替実装しない。

## 変更していない実状態

実Scriptは再配備せず、GitHubゲート・実PAT・Google継続認証・Windowsタスクは変更しない。
登録Enable/Disable/Start/Stopを実行しない。実レシート・旧STOP・旧恒久フェンス・既存履歴に触れない。
mainへmergeしない。コード・CIのPASSが実配備、継続認証、PAT管理、タスク有効化の承認にはならない。
