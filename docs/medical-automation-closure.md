# Medical＝完全手入力

2026-10-02の正式方針。Medicalの自動化研究を終了し、医療原本を人間が確認して必要情報を完全手入力・確定する。OCR候補による半自動記帳、OCR authority、Vision、外部AI、Gemini、crop・mask・allowlist画像送信、Medical AUTOは本番へ導入しない。新しい確認Web UIやiPhone UIは今回の範囲に含めない。

## 本番の境界

通常の受付は既存のprivacy gateを通す。`medical`は既存のMedical reviewを作り、原本リンクと完全手入力の案内を表示する。`sensitive_unknown`などは従来どおり保留し、normalへのfallbackはしない。分類に必要な既存のローカルOCRは維持するが、Medicalの日付・施設・金額を候補入力したり、確定の根拠に使ったりしない。

既存確認シートのH:K（支払日・施設名・実支払額・カテゴリ）を原本を見て入力する。支払方法・メモは任意。「医療費を確定」は完全な入力・本人判断に対してのみ有効。「候補で医療費を確定」は廃止し、過去にその判断が保存されていても採用しない。通常レシートの「候補明細で確定」は変更しない。

source/review identity、原本の鮮度とhash、owner入力の再照合、duplicate、durable intent、会計読戻し、replay/processed管理は維持する。正常な完全手入力の確定後は従来の記帳・原本整理を利用する。過去のMedical AUTOに基づく原本移動は実行しない。過去のMedical AUTO pending intentは状態を凍結し、会計行の有無にかかわらず自動確定・再追記・削除しない。通常レシートと完全手入力のpending intentは既存の会計読戻しで照合し、不足行を再追記しない。既存review、候補、検証記録、本人入力、会計行を削除しない。

## 稼働を停止した入口

| 入口 | 最終状態 |
|---|---|
| production_flowのMedical準備・送信・自動確定hook | 削除。通常のkey-free確認子プロセスのみ |
| receipt_confirmation_productionのMedical OCR/crop/AI準備と自動記帳 | 削除。privacy分類とmanual reviewのみ |
| medical_local_reading.apply_local | 常にFalse。state・writerを呼ばない |
| medical_auto_posting.apply_automatic | 常に0。state・writerを呼ばない |
| medical_candidate_runtime.run_prepared/send_derived | 入口で停止。認証・画像・subprocessに到達しない |
| medical_image_sender / medical_image_candidate.request_payment | 入口で停止。Geminiや他APIを構築・送信しない |
| 既存確認writerのMedical automatic plan | 拒否。完全手入力のdurable confirmationのみ |
| 本番workflowのMEDICAL_DERIVED_AI_POLICY転送 | 削除 |
| Medical専用OCR施設モデルの本番準備 | 削除。通常レシート/privacy用OCRは維持 |
| Medical Vision synthetic workflow 2本 | GitHub登録をdisabled_manuallyへ。現在mainには存在しない |

repo variable `MEDICAL_DERIVED_AI_POLICY` は削除した。旧mainでは未設定により実験hookへ入らず、現行本番コードもこの変数を転送・使用しない。`off`は旧mainで無効値となり通常確認を停止するため、最終状態は未設定とした。通常レシート・銀行・PayPay・Amazon・Payrollのworkflow、schedule、Secrets、既存writerは維持する。通常レシートのOCRモデル・pip cacheも維持する。MedicalのOllama/Qwenモデル取得・cache・定期実行は本番にない。

研究用の`MEDICAL_REVIEW_SHADOW_ENABLED`とreview store設定はライブラリに残るが、既定falseで本番workflowには転送しない。GitHubにMedical shadow変数・Secretや環境単位の上書きはない。通常の本番intakeから研究用observerを作らない。

## 保存と稼働の分離

privacy判定、数字・日付・label/bbox、identity、duplicate、admissionの純粋な評価ライブラリとsynthetic fixtureは比較研究・安全回帰用として保存する。これらの存在は本番AUTOの許可を意味しない。旧crop確認ツールや研究用比較関数は通常intake/本番workflowから呼ばない。`research/medical-automation/legacy-tests`に旧AUTO成功テストのsynthetic snapshotを保存し、現在のCI対象から分離した。旧意味での再現はbaseline `cdf1e77ef05317c9587b8e93c8e50a9a1fa66d0d`の隔離checkoutで行う。現行CIには送信・自動記帳の停止、privacy、完全手入力、duplicate/replayのテストを残す。

既存workspaceのbenchmark、shadow、A/B、negativeのレポート・JSONは元の場所を維持する。研究branchのVision synthetic workflowも履歴として残すが、GitHub登録は無効化する。ローカルの半手入力UI試作はmainへ取り込まず終了する。研究の再開には別Goalが必要。本変更に実画像、PII、患者情報、実画像パス、個人PCパス、Secretsは含めない。

## 研究結論

| 検証 | 結論 |
|---|---|
| crop | PII残存あり |
| mask | PII残存あり |
| allowlist画像 | privacyは改善したがutility不足 |
| Qwen3-VL 2B | Actionsで合成画像の実行は可能 |
| Vision独立抽出 | 10件中8正解・0誤答・2 UNKNOWN。一定の精度あり |
| OCR＋Vision A/B | 同じ10件・authorityで増分AUTO 0、増分確認削減率0ポイント |
| OCR-only fresh shadow | 4/10 AUTO候補、6 REVIEW、誤AUTO 0。採用しない |
| REVIEW改善 | 採用できる汎用改善なし |
| 最終判断 | Medical＝完全手入力。医療自動化開発を終了 |

上記は過去の隔離検証の結果であり、現在の本番AUTO性能を示すものではない。現在のMedical自動記帳数は常に0。closure検証はsynthetic/mockとsafe no-opで実施し、実Spreadsheet/Driveへの書込み・原本削除・移動は行わない。
