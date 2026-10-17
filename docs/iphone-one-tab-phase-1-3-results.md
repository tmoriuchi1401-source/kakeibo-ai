# iPhone確認UI Phase 1～3 実施結果

2026-10-17。1タブの継続仕様、並べ替えを通常提供しない下書き安全契約、既存validatorへのオフライン接続を作成した。**実Sheets adapter・認証付き確定受付・本番接続は未完成**。この報告は本番有効化の承認依頼ではない。

| Phase | 実施・commit／PR | 検証と再利用 | 残課題／最小追加作業／次の承認 |
|---|---|---|---|
| 1 | AGENTS.mdと継続仕様を1タブへ統一。旧4タブは履歴。文書commit `3cc9364`、[PR #126](https://github.com/tmoriuchi1401-source/kakeibo-ai/pull/126)、main merge `d62e1e2` | 文書チェック5項目、[CI 39016805811](https://github.com/tmoriuchi1401-source/kakeibo-ai/actions/runs/39016805811) 3ジョブ成功。pytest 3,808件（一般3,103＋sensitive705）、Node13件を各matrixで成功、OCR成功。既存安全仕様を保持、処理変更0 | 文書反映完了。認証・本番接続・並べ替え安全性の未完成を明示。文書mergeの追加承認不要 |
| 2 | `iphone_review_drafts`、独立メモリ環境、公式仕様調査。commit `ca46274`、[Draft PR #127](https://github.com/tmoriuchi1401-source/kakeibo-ai/pull/127) | 新規38件成功。ID／原本identity／種類／snapshot、検索中心・構造変化停止、G103入力保留・H301隔離、再生成・同時更新契約。既存StateError／digest／隔離定数を再利用 | 実Sheetsの保護・規則・列schema reader、永続journal CAS、ライブ再生成排他は未実装。owner直接編集・有効な値のみ交換は完全検知不可。独立Sheets資源作成・合成実機試験を別承認 |
| 3 | `iphone_review_preview`、dataflow／再利用・不足一覧／受渡し契約／受付比較／承認ゲート。code commit `8a6293e`、同Draft PR | 新規19件＋既存回帰359件成功。医療手入力・一般明細snapshot・PDF構造purpose契約を実コードで検証。purpose／native writer／duplicate／replay／revision／read-back／認証hostの既存契約を再利用 | 既存ホストへのdraft acceptance、exact snapshot再表示・明示POST、全legacyとの共有CAS gate、一般明細カテゴリ契約、追加ユーザー認可が不足。adapterと実ホスト接続実装の範囲を別承認。main merge／Cloud／本番有効化／実canaryは別承認 |

## 合成検証の証跡と限界

[CI 39017523271](https://github.com/tmoriuchi1401-source/kakeibo-ai/actions/runs/39017523271)、実装commit `8a6293e`：新規57件をcollect-onlyで列挙し、同じ57 node IDが同順でPASSED。Phase 2＝38、Phase 3＝19、重複0、skip／xfail／failed各0。既存回帰359件、計416件成功。compile／diff check成功。

既存回帰の6ファイルは`test_unified_receipt_review`、`test_unified_receipt_eligibility`、`test_unified_receipt_write`、`test_unified_receipt_purposes`、`test_unified_receipt_shadow_host`、`test_production_workflows`。既存fake DBへのwriter動作や使い捨てメモリauthorityの検証と、本番作用を区別する。新規previewの成功時は書込み関数をfail-on-callで塞ぎ、DB／状態の前後一致、authorized=false、operation_targets=[]を確認している。

ローカルはPhase 2の38件とPhase 3の一般／医療14件が成功。PDF5件はWindowsでhash固定bundleの構築が失敗し、native処理未到達。hash照合・tar filter・native fixtureは変更せずLinuxで全件検証した。Windowsで全件成功したとは記録しない。CIは実Sheets／実iPhone／実OAuthの証明ではない。

## 変更範囲と禁止事項の維持

- 独立ブランチはPR #124のコードを継承し、mainの文書仕様を取り込んだもの。PR #123 `1db04ac`／#124 `936388f`はhead・base・Draft状態を維持。#127もDraft。処理コードのmain mergeは行わない。
- 既存validator／writer／authority／auth-session bundle／本番runner／本番workflowは無変更。新規CIだけを追加し、その全体を完全一致で検証するassertionを追加した。CIはcontents:read、資格情報なし、deploy／Cloud／image buildなし。
- 本番Spreadsheet変更0、承認済み合成ブック変更0、新規Google Drive資源0、本番記帳0、原本移動0、AI送信0、実authority更新0、Cloud作業0。本番認証情報・実医療データの利用0。
- Sheetsには下書きだけを置く。セル編集・状態変更を記帳へ直結しない。PDF構造確定を会計許可と混同せず、医療を外部AIへ送信しない。任意ソート可能な本番UIを完成扱いしない。

詳細は[並べ替え安全性](iphone-one-tab-sorting-safety.md)と[既存処理への接続契約](iphone-one-tab-existing-processing-connection.md)。次は独立Sheetsで実列schemaと保護を確認する範囲、および既存ホストの最小adapter／draft acceptanceを実装する範囲について承認を得る。本番接続の有効化や実データ記帳テストは、その承認に含めない。
