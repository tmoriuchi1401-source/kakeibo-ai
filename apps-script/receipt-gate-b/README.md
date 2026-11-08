# Gate B：合成BOOK専用のイベント観測

`Code.gs`は登録・検証・dispatchを呼ばず、許可されたBOOKの非表示`_gate_b_events`へ観測行を1回appendするだけのコード。既存`receipt-validation/Code.gs`とは別project用。公開templateはID未設定・enabled=falseで停止する。

実装・試験は[監査記録](../../docs/receipt-gate-b-event-acceptance-results.md)、操作/権限/停止範囲は[Gate B計画](../../docs/receipt-gate-b-event-acceptance-plan.md)を参照する。

## 設置前の確認

1. 指定済み合成BOOKにbound projectを1件だけ作成する。既存projectがあれば再作成や上書きをせず内容を照合する。
2. `Code.gs`と`appsscript.json`を専用projectへ配置する。private manifestからBOOK/sheet/log ID・試験UUID・固定機械bindingを設定した版を使い、実値をGit/PR/CIに転記しない。非公開の配置用ファイルもenabled=falseで準備する。
3. 宣言scopeは`spreadsheets.currentonly`だけ。`receiptGateBPreflight`は対象BOOK/タブ/ログ見出しをread-only確認する。初回認可が要求されたら、**実際の表示scope・実行owner・用途を提示して停止**する。宣言値と実認可画面を同一視しない。
4. 認可が個別承認・完了した後だけ、private configをenabled=trueへ変更し、ownerがproject画面で`receiptGateBObserveEdit`のinstallable onEditを最大1件作成する。project ID・trigger UID・ownerを非公開に記録し、既存triggerは変更しない。
5. 人のiPhone操作を案内する前に、専用projectが当該BOOKにboundし、triggerが1件だけであることを確認する。manual/APIで作ったeventは実機証拠にしない。

`event.value=register:<既知UUID>`と`oldValue=ready:<同UUID>`、単一登録セル、BOOK/sheet、固定machine bindingの一意性を検査する。行番号は診断にだけ使い、UUIDから案件を選ぶ。全行sortは一意bindingを再探索し、部分sort/重複/欠落は拒否する。e.user/編集者メール・自由入力・メモを読取ログへ保存しない。通常TRUE/未知値は固定の無害な文字列へ置換する。

ScriptLockは同project内のログ処理だけを直列化し、人の編集を止めるCASではない。ログは改変可能な観測であり金融authority/不変journalではない。append/flushの例外は固定UNKNOWNエラーで停止し、再appendしない。プラットフォームの配送保証や、ログ消失時のexactly-onceを主張しない。専用triggerを停止して照合し、同UUID/別UUIDで再送しない。

## 合成試験

`node --test apps-script/receipt-gate-b/Code.test.cjs`

既存pytest discoveryから`tests/test_receipt_gate_b.py`も同じ17件を実行する。テストはネットワークも実Google identityも使わず、実ID・実UUIDを含まない。実機未確認の境界を合成成功と区別する。
