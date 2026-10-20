# R2-3 検証・成果物記録

独自変更の基点は #149 `9ba5d11d75aeb55d21b3540ff06ee11dc937ac68`。新branch `codex/r2-3-read-only-audit`。新Draftのbaseは #148 `codex/r2-2-isolated-mandatory-gate` とし、既存 `r2-2-m5-m6.yml` のPR専用合成CIをそのまま使う。既存workflowのbranch filter・schedule・permissionsには触れない。#149差分は継承されるが今回独自の処理変更ではない。

## 実施する検証と判定範囲

- JSONの構文、元39 ID/順序/grade完全一致、A01〜A13の網羅、workflow50件/運用22件、取得成功とページ件数、job/run SHA対応、機微情報の非混入、相対リンク、whitespaceをローカルで検査する。データ取得用scriptは作業領域だけに置き、運用scriptとしてPRへ追加しない。
- #149基点から `app/`、`staging_shadow/`、`auth-session/`、`.github/workflows/`、AGENTS.md/継続仕様、既存資料/原本が不変であることをGit diffで確認する。
- 既存静的検査の正確な許可パス集合3か所に、本監査の文書/JSONパスだけを追記する。旧本番不変assert・元39行の過去判定・固定SHA/行根拠・workflow安全条件・金融拒否assertを維持する。wildcardや全docs許可へ緩和しない。
- 新Draft作成による既存Ubuntu CIはhead SHA checkout / contents:read / production secret・environmentなし / UNIFIED_RECEIPT_WRITE_ENABLED=false。CI job/runの確定URL・commit・結果は新Draft本文と完了報告に記載する。過去#149 CIを今回のCIとして数えない。
- CIは新規M5/M6、継承M1〜M3、39入口静的監査、既存安全回帰を実行する。今回は新しいruntimeテストを追加していない。合成CIの成功で本番gateやpending照合完了を主張しない。

## 監査の終了時照合

ローカル検証済み: JSON 14ファイル、39 ID/順序/grade、13group、50workflow、運用22query/22job query、全取得成功、ページ件数、リンク、秘密値/未マスク識別情報のパターン検査は成功。既存assertを挿入前へ戻すと#149と完全一致し、保護対象の処理/workflow/仕様/既存資料差分は0。既存read-only静的監査は **16 tests成功**（7.61秒）。初回はcloneにhistorical固定objectがなく2 errorとなったが、指定SHAのソースをfetch後に全16件成功。テスト条件は緩和していない。

再照会は2026-10-20 21:17:58〜21:17:59 UTC、5状態とも0件。#146〜#149/mainは開始時と一致。Cloud/端末の未確認項目を埋めた証明ではない。

[開始時](r2-3-evidence/pr-baseline.json) と [終了時](r2-3-evidence/pr-final.json) でmainと#146〜#149のhead/base branch/SHA・Draft/state/mergedを照合する。監査で既存PR/mainを書き換える操作はない。

[開始時のActions状態](r2-3-evidence/active-runs-start.json) と [切替操作を伴わない再照会](r2-3-evidence/active-runs-end.json) はstatusごとの時点観測。新Draftの合成CIが実行中なら本番jobと区別する。全native/requestの完了待ちを実行した記録ではない。

## 未実施

本番write/金融POST/authority発行・消費、原本取得/移動/AI、Sheets会計値/Firestore/Drive本文取得、実データexport、Cloud deploy/API有効化、IAM/OAuth/credential変更、job dispatch/cancel/re-run/停止、schedule/validated更新、native migration/pending解除、main merge、既存PR更新は実施しない。新Draftとそのbranch/commitおよび自動合成CIのみ作成する。
