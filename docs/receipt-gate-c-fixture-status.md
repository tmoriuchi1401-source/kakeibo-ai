# Gate C：オフラインfixture完了と未解決条件

今回の基準：PR #200、`30a70fc59814dcf1e5295da9e13cedaa17802ac7`。過去工程の検証結果は各節に区別して記す。
この文書は非機密の検証結果と作業状態だけを記録する。認証・運用情報を含む別紙は非公開を維持し、GitHub公開・commitの対象にしない。別紙の公開承認は求めない。

## 作業状態

| 作業 | 状態 | 完成範囲・残条件 |
|---|---|---|
| オフラインfixture | **完了** | 既存合成PNGを再利用するシナリオ、候補2行×11列、取込1行×12列、解析hash/ID/行順、3明細以内、既存decoderとの整合性を検証済み |
| policy承認 | **完了（fixture v1限定）** | ownerの今回の明示指示に基づき「その他／その他」1pair・rules=[]だけを承認。本番適用なし |
| 本人確認・actor | **未完了** | 対象caseに結合した真正な確認証跡なし。mock actorを実接続に流用しない |
| 実接続 | **NO-GO** | 上記未成立条件と既存の接続前条件が残る。オフライン完了は接続許可ではない |

実接続用catalogと独立catalog pinも**未完成**。候補データの内部整合性が通ることを、policy承認や本人確認の成立とは扱わない。

## 非機密の検証結果

- [fixture定義](../tests/fixtures/receipt_gate_c_candidate_v1.json)と[テスト](../tests/test_receipt_gate_c_fixture.py)は基準commitに保存済み。実BOOKから期待値を逆算していない。
- 意図的誤読730円の候補を868円へ修正する合成シナリオ。初期差額138円、修正シミュレーション後0円。新しいOCR結果や実際の本人確認を装わない。
- candidate_rows、parse_import_rows、parse_rules、verified_candidate_range、strict catalog validator、decoderを既存関数のまま検証。本人確認のないdraftのstrict拒否は期待どおりであり、live catalogの受入成功ではない。
- 初回fixtureのローカル検証は**新規10件＋既存関連380件＝390件成功・失敗0**。今回のpolicy記録変更後の検証は末尾に記す。実環境・iPhone導線の受入成功は未証明。

## 次に必要な最小作業・承認

1. **policy承認は成立済み**：fixture v1限定の「その他／その他」1pair・rules=[]。他fixture・本番policyへ拡張しない。
2. **真正な確認証跡の取得・検証範囲の承認**：既存の本人確認・actor証跡の再利用を優先し、対象case・原本版・総額への結合を確認する。既存機能の再実装、新認証基盤、広範な再調査は追加しない。証跡がなければ未完了を維持する。
3. **成立後のcatalog/pin確定**：1と2が成立してから最終データを照合し、独立したpinを固定する。それまでは値を補完せず、catalog/pinを完成扱いしない。

その後の接続実装・設定変更・実環境試験は既存設計の最小差分に限り、別承認とする。今回その実装を開始せず、別紙公開を次工程の条件にしない。

今回の公開変更はfixture定義・既存fixtureテスト・このstatus文書のみ。前工程の認証コード差分と機密別紙はworkspaceに保持する。Google write、Actions dispatch、Secrets変更、金融write、main変更・mergeは行わない。**Gate B総合・Gate C実環境・本番NO-GOを維持する。**

## policy承認対象の確定（準備時の基準5a46d26）

承認対象は **gate-c-offline-candidate-v1限定の「その他／その他」1pair、rules=[]（ルール0件）**。既存合成fixtureの2商品・総額1,368円の試験だけを対象にする。

準備時は `PROPOSED_NOT_APPROVED` を維持した。その後、下記のowner明示指示によりfixture限定の承認を記録した。本番policyの採用・本人確認・実接続の承認は含めない。

## 非金融確認adapterのオフライン検証（基準64f8da9）

既存認証処理を再利用する薄いadapterと合成テストを作成した。新しいWeb認証基盤・接続先・金融authorityは追加しない。adapterはオフラインtest treeに限り配置し、通常コンテナ・Web経路には組み込まない。認証・運用の詳細評価は非公開のまま保持する。

対象原本版・総額1,368円・非金融の確認目的と、検証されたactor・確認時点を同じ保護記録へ結合する方式をオフラインで検証した。合成認証テストの成功を実本人の確認証跡と扱わない。実接続用actor、本人確認済み総額、catalog/pinは未完成。

ローカル検証は、新規39件を含む既存認証・確認経路の回帰402件と、現行fixture／Gate C関連419件が成功。構文・whitespace・通常コンテナへのadapter非収容も確認した。Google接続・実本人認証・実writeの検証結果ではない。

この工程のPR反映は本statusのみ。オフライン実装差分とテストはworkspaceに保持し、PR上の実装・CI完了とは扱わない。

このオフラインadapter工程の終了時はpolicy未承認だった。今回policyだけが成立し、既存経路で真正な本人確認記録を取得・照合する操作範囲の別承認は未成立。その成立後に既存処理でcatalog/pinを確定する。UNKNOWNの永続停止・送信一回性など既存の接続前条件も継続する。**実接続NO-GOを維持する。**

## fixture v1限定のowner policy承認記録

2026-11-09の本会話でのowner明示指示を根拠に記録する。承認対象は `gate-c-offline-candidate-v1` の「その他／その他」1pair・rules=[]。本番適用なし。承認情報はfixtureの `policy.approval` に保存し、基準commit、元のfixture定義digest、policy digest、対象版を結合する。この記録はGoogle本人認証済みactorや原本総額確認の証拠ではない。

policy状態と承認情報だけを更新する。元の解析payload内の未承認注記は作成時の履歴として固定し、解析hash・候補ID・取込行は変更しない。現在のpolicy状態は `policy.approval` を参照する。他fixture・本番カテゴリ・本番ルールは非変更。

## 本人確認証跡取得の実施計画と次の承認

| 項目 | 実施前に固定する範囲 |
|---|---|
| 対象 | このfixture v1に対応する既存合成PNG 1件・同一source/hash/version・総額1,368円。実ID等は非公開資料だけで照合する |
| 結合内容 | 検証済みactor、source/hash/version、1,368円の明示確認、非金融目的、request、確認日時、保護された証跡 |
| 経路・保存先 | 既存認証adapterと保護request保存先を使用する計画。実際の入口・保存資源は未指定であり、ownerによる指定と別承認まで実施保留。既存のGate C用JSONを保存先と推測しない |
| 副作用 | 将来の実施にはGoogle認証・原本読取の通信に加え、確認要求の状態・証跡・必要なsessionの保存が伴う。非金融でもGoogle writeが含まれ得るため、今回の承認範囲外 |
| 停止 | 対象・保存先未確定、既存経路未対応、認証／版／目的不一致、期限切れ、競合、応答不明、読戻し不一致で停止。UNKNOWNから自動再送・自動再開しない |

次の承認対象は、具体的な既存入口と保護保存先を固定した上での**当該1件の実本人認証、非金融確認、証跡保存・独立読戻し**。host再開・設定変更が必要なら、その差分も先に個別承認する。詳細な実施計画はworkspaceの非公開資料に保持し、新Web・新DB・常駐serverは追加しない。

ownerは入口・保存先について「未指定のため実施保留」と回答した。今回のpolicy承認から実施許可を推定しない。

今回は実認証・Google通信・Google write・Actions dispatchを実施しない。真正な本人確認が未成立の間、**actor・total_confirmation・実接続用catalog/pinは未完成**。policy承認だけで補完しない。

今回のローカル検証：既存fixtureテスト10件を含む関連419件PASS。承認情報がfixture版・基準定義digest・pair/rulesに一致し、本番適用falseであることを検証した。既存の非公開fixtureもpolicy以外の全データが不変で、actor・total_confirmation・catalog pinは未設定のままである。新たな認証実装は追加しない。
