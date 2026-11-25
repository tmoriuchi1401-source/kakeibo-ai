# 税区分不明の旧保留を金額優先版で再評価

コード・CIのみ。現行Script・実シート・旧受付は変更しない。

新しい通常登録は税区分「不明」を維持し、金額と他の必須検証を通過した場合だけ
`検証済み・未記帳`、`warnings=[tax_treatment_unknown]` とする。
金額差や内税二重加算は保留。原本の支払総額は本人の登録時確認と固定snapshotに紐づけ、
backendは分析された原本の支払総額との一致・明細合計・重複・原本版/hashを検証する。
税区分を推測して補完しない。

旧案件の再評価は元UUID・snapshot・結果を不変とし、`_receipt_policy_reviews` に
元結果digest、元snapshot digest、判定版 `receipt-review-amount-v2`、新結果を1行追記する。
元受付につき1回のみ。永続ローカルフェンスをRPC前に消費し、応答不明時は停止して再送しない。
税区分修正履歴がある案件とは併用しない。確認行G列は履歴の結び付きを確認して有効結果を表示し、
CS列は「不明」のまま。新規intake・Gemini送信・登録チェック再操作・金融writeはない。

将来、別途本番操作が許可された際の最小順序：

1. 固定SHA・CI、旧受付/原本/STOP/両受付OFF/既存フェンス・継続本人認証を照合する。
2. 同じ独立Scriptへ `Review.gs`、`ReviewSetup.gs`、`PolicyRecheck.gs` を含む修正版を配備する。
3. 両受付OFF中に `receiptReviewPreparePolicyRecheck()` を明示的に1回実行し、
   新しい隠し台帳とG列表示を独立読戻しする。旧要求・元結果・入力を変更しない。
4. 対象の旧結果が `tax_treatment_unknown` だけで、金額証拠と取引内容が十分か確認する。
   条件が揃った場合だけ、既存DB・既存OAuth・既存PATを使い、有限の限定枠で
   `python -m scripts.run_receipt_review_policy_recheck --execute --config PRIVATE_CONFIG --attempt-db EXISTING_DB --state-dir PRIVATE_OPERATION --request-id ORIGINAL_UUID`
   を1回実行する。元結果・新履歴・同じ確認行・両受付OFFを独立読戻しする。

今回の実原本はカード売上票で支払総額1,550円は印字されているが、税区分や商品明細の
印字根拠は不足している。再評価の技術経路は準備できても、取引内容と明細金額の根拠が
確認できるまでは成功扱いにしない。推測して税込/税別に変更しない。
