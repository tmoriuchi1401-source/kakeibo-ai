# R2-3 39入口の実稼働分類

元の #147 A〜E は継承し、本番観測状態を別列に追加する。全行の固定コード根拠/更新対象/迂回判定は [JSON](r2-3-ingresses.json)。
「実稼働」は確認したserviceのavailable/traffic設定の範囲。「起動可能だが実行なし」はactive workflowに条件付きdispatch経路が残り、監査時点の実行が0という意味。資格情報や入力条件を満たして起動試験した意味ではない。
「現在停止中」は対象workflowに限定。親runnerや手動CLIまで停止した意味ではない。過去successは現在のwriter成功や未完了0の証明でない。

| ID / #147判定 | 入口 | 観測分類 | 範囲・証拠・不足 |
|---|---|---|---|
| R01 / B | 通常レシート自動取込 | 本番未確認 | 実request・source別stageは未確認。親の最新job skipped/過去successだけから当該writerの稼働・停止を断定しない。 |
| R02 / B | 買取レシート収入 | 本番未確認 | 実request・source別stageは未確認。親の最新job skipped/過去successだけから当該writerの稼働・停止を断定しない。 |
| R03 / C | 旧一般レシート確認 | 本番未確認 | 実request・source別stageは未確認。親の最新job skipped/過去successだけから当該writerの稼働・停止を断定しない。 |
| R04 / C | 医療の旧確認・完全手入力 | 本番未確認 | 実request・source別stageは未確認。親の最新job skipped/過去successだけから当該writerの稼働・停止を断定しない。 |
| R05 / B | R1本人確定の本番候補 | コード上のみ存在 | 新R1はmain未統合・production provider未接続。外部コピーの配備有無は未確認。 |
| S01 / A | R1隔離合成compositionのみ | コード上のみ存在 | 隔離memory composition。#149 CI成功履歴は本番実行でない。 |
| R06 / B | PDF自動Unit | 本番未確認 | 実request・source別stageは未確認。親の最新job skipped/過去successだけから当該writerの稼働・停止を断定しない。 |
| R07 / B | PDF一般manual Unit | 本番未確認 | 旧host/旧treeとのコード経路は存在。稼働imageのソース対応・native/authority・実呼出しは未確認。 |
| R08 / D | 複数ページgrouped Unit | 本番未確認 | 旧host/旧treeとのコード経路は存在。稼働imageのソース対応・native/authority・実呼出しは未確認。 |
| R09 / C | p14固定canary | 起動可能だが実行なし | p14-posting active/dispatch定義、最新run37927668606 success、現在Actions実行なし。flag/権限成立は未確認。 |
| R10 / C | p4/p10明示Unit canary | 起動可能だが実行なし | receipt-unit-posting active/dispatch定義、最新run38005016533 success、現在Actions実行なし。flag/権限成立は未確認。 |
| R11 / C | 歴史的PDF Unit write canary workflow | 起動可能だが実行なし | pdf-unit-write-canary active/dispatch定義、最新run37174760247 success、historical checkout実値未確認。 |
| R12 / C | historical/test_tree一般pipeline | 本番未確認 | 旧host/旧treeとのコード経路は存在。稼働imageのソース対応・native/authority・実呼出しは未確認。 |
| R13 / E | 固定10件normal再取込・過去再解析 | 本番未確認 | 実request・source別stageは未確認。親の最新job skipped/過去successだけから当該writerの稼働・停止を断定しない。 |
| R14 / C | 旧CLI・local cron・旧host・直接DB/API | 本番未確認 | Windows schedulerはCIM閲覧不可。他端末cron/CLI/SDK・配布済みcredential・Apps Script実配置は未確認。 |
| D01 / D | 対応不明旧医療・旧native履歴 | 本番未確認 | H301/p1等の実native所在・pending/UNKNOWN本文は未取得。旧mappingは推測しない。 |
| E01 / E | 旧認証host・構造/明細確認・registered proof | 実稼働を確認 | Cloud Run service available・最新revisionへtraffic100%。現在0rps表示。実request/authority/金融完了は未確認。 |
| E02 / E | plan-only・構造/privacy下書き・probe | 本番未確認 | 実request・source別stageは未確認。親の最新job skipped/過去successだけから当該writerの稼働・停止を断定しない。 |
| A01 / B | 通常review反映・除外・統合 | 本番未確認 | 実request・source別stageは未確認。親の最新job skipped/過去successだけから当該writerの稼働・停止を断定しない。 |
| A02 / B | 自動照合・既存支出除外 | 本番未確認 | 実request・source別stageは未確認。親の最新job skipped/過去successだけから当該writerの稼働・停止を断定しない。 |
| A03 / B | auto-expense・銀行最終反映 | 本番未確認 | 実request・source別stageは未確認。親の最新job skipped/過去successだけから当該writerの稼働・停止を断定しない。 |
| A04 / B | Amazon旧取込・商品展開・分割払い | 本番未確認 | 実request・source別stageは未確認。親の最新job skipped/過去successだけから当該writerの稼働・停止を断定しない。 |
| A05 / B | Amazon recurring・money writer・canary | 本番未確認 | 実request・source別stageは未確認。親の最新job skipped/過去successだけから当該writerの稼働・停止を断定しない。 |
| A06 / B | au PAYカードone-shot/batch/recurring | 本番未確認 | 実request・source別stageは未確認。親の最新job skipped/過去successだけから当該writerの稼働・停止を断定しない。 |
| A07 / B | 銀行canonical・income recurring/backfill | 本番未確認 | 実request・source別stageは未確認。親の最新job skipped/過去successだけから当該writerの稼働・停止を断定しない。 |
| A08 / B | PayPay・au PAY速報/CSV・旧card CSV | 本番未確認 | 実request・source別stageは未確認。親の最新job skipped/過去successだけから当該writerの稼働・停止を断定しない。 |
| A09 / B | 現金手入力・取消 | 本番未確認 | manual-entry最新job skipped。Apps Script deployment/trigger/実行者・受付nativeは未確認。 |
| A10 / B | 日常UI固定ID修正・money review | 本番未確認 | 実request・source別stageは未確認。親の最新job skipped/過去successだけから当該writerの稼働・停止を断定しない。 |
| A11 / B | 分類過去反映・restore・管理手動実行 | 本番未確認 | 実request・source別stageは未確認。親の最新job skipped/過去successだけから当該writerの稼働・停止を断定しない。 |
| A12 / B | 台帳整列・直接Sheets/Drive/DB保守 | 本番未確認 | 実request・source別stageは未確認。親の最新job skipped/過去successだけから当該writerの稼働・停止を断定しない。 |
| E03 / E | 原本archive・backup・retention | 本番未確認 | 実request・source別stageは未確認。親の最新job skipped/過去successだけから当該writerの稼働・停止を断定しない。 |
| E04 / E | 給与preview・純粋projection・schema/UI保守 | 本番未確認 | 実request・source別stageは未確認。親の最新job skipped/過去successだけから当該writerの稼働・停止を断定しない。 |
| E05 / E | 旧医療automatic helper | コード上のみ存在 | 現行helperはreturn 0。別SHA/外部医療処理の停止証明ではない。 |
| E06 / E | 銀行用途確認・分類ルール受付 | 本番未確認 | 実request・source別stageは未確認。親の最新job skipped/過去successだけから当該writerの稼働・停止を断定しない。 |
| E07 / E | retired Amazon legacy workflow | 現在停止中と確認 | amazon-daily-import API disabled_manually、mainはechoだけ。過去runはretire前。CLI/親のAmazonは別入口。 |
| S02 / B | 通常/買取の合成既存writer接続（本番再利用候補） | コード上のみ存在 | 合成自動/買取writer。production未接続。 |
| S03 / B | 旧snapshot native v2合成guarded writer（本番再利用候補） | コード上のみ存在 | 合成v2 guarded writer。production未接続。 |
| A13 / B | 銀行status単セル修復・カード行修復 | 本番未確認 | 専用workflowなし。CLI/SDK/運用者の資格情報・実実行は未確認。 |
| R15 / C | 管理state移行・pending解除・初期化 | 本番未確認 | 実request・source別stageは未確認。親の最新job skipped/過去successだけから当該writerの稼働・停止を断定しない。 |

分類件数: 本番未確認=29、コード上のみ存在=5、起動可能だが実行なし=3、実稼働を確認=1、現在停止中と確認=1。

共通の阻害条件: financial_write_possible_in_legacy_code=true の経路はR1と同じ原本/台帳へ届くコードを持ち、実保存先・資格情報が未確認。別source/別IDでも全ledger snapshotを変える。旧SHA/旧host/直接SDKにwrite権限が残る、または残存を否定できない限りNO-GO。
E01の公開invoker、E03の原本移動/削除、R13の結果native、R15のpending解除も金融行を直接書かないという理由で統制から除外しない。
