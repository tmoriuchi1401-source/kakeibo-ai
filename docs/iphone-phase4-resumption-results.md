# Phase 4A～4B：取得経路の切分け、7原本照合と行挿入時の再停止

> 以下は2026-10-17の再停止時点の履歴。後続の承認で復旧・構造変更検証を実施した。
> 現在の状態は[Phase 4A実Sheets復旧結果](iphone-phase4a-live-recovery-results.md)を参照。

2026-10-17（停止後のread-back：10:47 UTC以降）。Draft PR #128。
**原本7件の照合は成功。Phase 4A～4Bは途中で再停止しており未完了。本番接続完了ではない。**

## 停止理由と現状

専用ブックの空行挿入テストで、追加行にGoogle Sheetsが原本リンク数式を自動補完した。
案件ID・原本ID・hashが空の追加行 C5 に、SYN-G103と同じHYPERLINK数式が入った。
「空行になる」というテスト前提と異なるため、ユーザーの異常時停止条件を適用し、以降の実ブック操作と追加実装を停止した。

readerは `iphone_sheet_structure_changed` で停止し、挿入後snapshotをGit除外の `.private/` に隔離した。
不明な行・入力・数式を下書きに採用していない。修復・再生成・削除・並べ替えも実行していない。
専用ブックには元の8案件と追加テスト行1行が残る。実業務に使用しない。

既存8案件の A:Q のuserEnteredValue（数式を含む）・注記・入力規則は、案件IDで照合し全セル一致した。
G103の保留・既存値・途中メモ、H301のLEGACY_UNMAPPED_HOLDを保持している。
この一致は、追加行の原本リンクを正しい案件対応として承認するものではない。

## 取得経路の切分け

- Driveコネクタのraw-file取得要求は成功し、プロバイダのファイル参照が返っていた。
- Nodeのglobal fetchは公開Google URLでも `ENOTFOUND`、`fetch failed`。ファイルhash不一致の証拠ではない。
- sandbox内PowerShell HTTPSも失敗した一方、承認済みの外側Windows HTTPS経路は疎通した。
- 既存の認証済みDriveコネクタが発行するraw-file参照を、Windows .NET HttpClientでメモリへ直接読み、SHA-256を独立計算した。redirectは禁止、timeoutを設定し、計算後はbyte配列を消去・破棄した。
- 新たなOAuth設定、認証情報の抽出、原本のローカル保存は行っていない。Google Drive APIの新規credentialによる直接取得を実装したわけではない。
- 一時ダウンロードURL・原本bytes・base64・認証情報はGit、ログ、CI成果物に保存していない。出力は件数・長さ・hash・照合結果だけ。ファイル参照の一時保持も破棄した。
- **Nodeの取得経路自体は修復されていない。** 既存connector + Windows HTTPSによる代替経路を検証した。

## 7原本の独立照合

作成時の `prototype-manifest.json` と `native-eight-case-ready-report.json` の原本ID対応記録を、
今回のシートとは独立した照合元として使用した。manifestの期待hash、記録の原本ID/hash、
専用ブックのP/Q列、今回取得した実bytesのSHA-256が全7件一致した。
ユーザーがChatGPT側で検証したG101も、Codex側で改めて取得・計算した。

| 案件 | bytes | 実bytesからのSHA-256 | manifest・ID対応・シート |
|---|---:|---|---|
| SYN-G101 | 47,845 | `d2eeb9d9c3246c514dc9b617d51f7b2a69070e2cffa38a96d720769984bc6c7e` | 一致 |
| SYN-G102 | 60,096 | `2c99f69229d876d16b836ba3e58249d93e9b12e64e5cd766c5ddc9e20405adf7` | 一致 |
| SYN-G103 | 49,938 | `7228787c830b23ce3ad31da35ad1eea63c95650069671d25d194645aba21e772` | 一致 |
| SYN-M101 | 52,145 | `279b9ac966a825d52d64319a18c13a18f814994983acf29a6216983a0b742811` | 一致 |
| SYN-M102 | 67,776 | `74679f47963b7675643366c9de46f3cc99b205a2e07cda4677fbd6973d7c5761` | 一致 |
| SYN-P201 | 184,310 | `121c457a461a69ecc0f7507d5c71abab8ab7ca0340c683cd12d40b40fce2b62c` | 一致 |
| SYN-P202 | 210,689 | `7ee841e9c73fefaa9b3e4b008612063b16af002fe8a34487396db3cb5c36c50f` | 一致 |

版識別は **`drive_revision_id`** として明示する。
各原本のrevisionをbytes取得前後に読み、全7件で不変、各1 revisionであることを確認した。
その後のreader検証直前にも7件を再読取りし一致した。
コネクタの正規化済みfiles.getは数値 `version` を返さないため、
Driveの数値versionを取得・照合したとは扱わず、後段の既存version契約との変換は未解決とする。

| 案件 | 検証した原本ID | 取得前後・reader前で一致したrevision ID |
|---|---|---|
| SYN-G101 | `12X5gkNTSdcQRyU25GEPnsj0kaKJwy67Y` | `0B3hV1kvKGgkbS1poWnBjRkg3cWxZSUNtc1p4WXVQamFBdmRFPQ` |
| SYN-G102 | `1HNLujkuCz3uAKDFdhPfMMGWYawGb6sVG` | `0B3hV1kvKGgkbcEVWUFBpbnRIQW43M3BHOG9CcitNWDgyU2dVPQ` |
| SYN-G103 | `1RwyWXHDEp2TDVjGcmtJM2sWt24x5bhLJ` | `0B3hV1kvKGgkbTGZMSkMvZWtLajhtVkI4OU5mSGt6KzlIVXM0PQ` |
| SYN-M101 | `1ZwrZ-wXjuBLFeSCWn1o7oK5oR28YaD0G` | `0B3hV1kvKGgkbRE1IVzBjajA1L2lZQjV2VFh4Um13N0VDOW13PQ` |
| SYN-M102 | `1ul2NR9Yq6fqG7K_9BQu7Kecys51LnXg4` | `0B3hV1kvKGgkbOU5wS3p5U0hzdWRpMEF4aEs4ZHdIY1Vaa2pZPQ` |
| SYN-P201 | `1D7juWARt3PUW172bLd2GPO8b0oLQWE14` | `0B3hV1kvKGgkbWmkrWGdqc1hPTG4weEQ3MDFlZUZTMENmenlNPQ` |
| SYN-P202 | `1jEQ4K8vnYtFp3VmWOlcSTpPRCuvPoHwr` | `0B3hV1kvKGgkbcHpIOS8zRkcwU2R1ZUFlejRZc0NnTFc5Q1lJPQ` |

## 実Sheets検証（Phase 4Aの実施済み範囲）

対象は既に作成済みの非公開専用ブック1冊だけ：
[独立安全検証ブック](https://docs.google.com/spreadsheets/d/1pmqtTu4NcNrZ-xwF7V0zyp6lKrIAMB8vUwUM8SDJeWg/edit)。
新たなDrive資源・共有権限の追加は0。

| 検証 | 結果 |
|---|---|
| 事前baseline | 「確認」1タブ、8案件、ID列・見出し固定。コピー元の保存済みraw値・注記・規則と一致 |
| raw値・数式・注記・規則 | Sheets API CellDataでA1:Z12を限定読取り。表示値を入力値として採用していない |
| 原本identity | 7件のP/Q列を独立manifest・bytes attestationと照合成功。H301は対応を生成しない |
| 保護 | シート全体の実保護1件、warningOnly=false、入力例外34セル。G103/H301の例外0 |
| 読取り | 案件順序・原本対応・保護・規則を照合して8件のdraft capture成功 |
| 挿入 | 0-based rowIndex=3に1行挿入。rowCount 1000→1001、table範囲9→10行 |
| 保護の挿入追従 | 入力例外34件はdimension挿入に追従して移動。これはsortの追従性を示すものではない |
| 追加行 | ID/hash/原本IDが空なのに、C5にSYN-G103の原本リンク数式が自動補完された |
| 既存入力の保持 | 8案件の全17列raw値・注記・規則は案件ID別比較で全て不変 |
| 検出・停止 | 構造変更で拒否。snapshotを隔離、下書き自動採用・修復0 |
| 削除・並べ替え | 異常時停止のため **未実施**。保護範囲のsort時ずれは今回まだ実測していない |

保護の詳細がmetadataコネクタから省略されるため、専用ブックだけで同一タイトルの
updateSpreadsheetPropertiesをmetadata probeとして1回実行し、
updatedSpreadsheetのprotectedRanges/tablesを確認した。セル値の変更はない。
行挿入もupdatedSpreadsheetのread-backを取得し、停止後にCellDataを再読取りした。

コピー元承認済みブックはread-only。停止後のmodified_timeは
`2026-10-17T06:02:02.042Z` で事前値と同じ。
本番ブックにはアクセス・変更していない。

## adapterの途中成果（Phase 4B未完了）

異常検出前に追加したコードを、非接続のDraftとして保存する。追加実装は停止。

- `app/iphone_sheet_reader.py`：注入済みGoogle clientのGET専用raw transport、
  管理側で明示発行するcanonical contract、保護・規則・注記・header・table・case order・
  原本ID/hash/revision/scheme照合、2回読取りの観測競合検出。
  案件bindingはID/kind/identityのdigest。行番号は契約の一時配置確認にのみ使う。
  ownerによる直接編集は防止できず、セル保護を安全境界としない。
- `app/iphone_draft_journal.py`：既存StateError/digest/strong_etagとversioned-IOの考え方を再利用。
  専用ローカルJSON1個、OS排他lock、CAS、request ID、同一再送の照合、atomic replace、
  flush/fsync、read-back、応答不明時のread-only recovery、別ファイルへのquarantine。
  新しい常駐サービス・Drive資源・独立DBは作っていない。
- `app/iphone_native_dry_run.py`：既存purpose別previewへの受渡し途中実装と、
  医療の既存手入力parser／result validatorによる項目だけのread-only確認。
  native側のcanonical page/Unit、保存済みinput digest、version schemeが必要で、
  Sheetsの状態セルをnative actionへ変換しない。
  **完全なnative受渡し関数の新規adapterテストは未完了**。本番からimport/接続していない。

ローカル永続journalへbaseline8件をrevision=1として保存し、別Python実行で再読取りした。
隔離後もrevision=1、request=1、8案件で、挿入後snapshotを採用していない。

### 実API payloadを使ったread-only確認

| 案件 | 結果 |
|---|---|
| SYN-G101 / G102 | native_snapshot_required（一般の複数明細・Unit context未接続） |
| SYN-G103 / H301 | held。入力・legacy mappingの変更なし |
| SYN-M101 | manual_fields_validated_only（既存医療手入力parserとresult validator） |
| SYN-M102 | incomplete_or_invalid_manual_fields。途中入力を保持し停止 |
| SYN-P201 / P202 | native_snapshot_required（信頼済みページ・構造操作契約が必要） |

これは **全案件の既存native validatorへのdry-run完了ではない**。
医療項目の合格も、重複検査・native plan・本人認可・記帳許可を意味しない。
原本revision IDを既存nativeの数値versionと同一視しない。

## テストと実施していない検証

| 範囲 | 件数 | 結果 |
|---|---:|---|
| 新raw reader合成unit tests | 46 | PASS |
| 新永続journal/CAS合成unit tests | 19 | PASS |
| 合計 | 65 | PASS（Windows、Python 3.12環境のvenv） |
| compileall / whitespace | — | PASS |
| 新adapterのLinux CI | 0 | 専用CI workflow未追加・未実施。CI完了とは扱わない |
| 全purposeのnative handoff新テスト | 0 | 未完了。既存#127のCI実績57件と混同しない |

readerの46件は並べ替え・挿入・案件削除・重複ID・部分identity変更・hold改変・
原本版/hash変更・formula入力・不正額・metadata/ACL変更・観測競合などを拒否する。
journalの19件は永続再読込み、再送、CAS競合、2 thread競合、通信応答不明の前後、
lock保持、破損時の無修復、隔離を確認する。
OS process crash・power loss・ネットワークfilesystem・分散hostの耐久性は未検証。
Windowsのfile fsyncは、実ホストの電源断耐久性の保証としては扱わない。

最初のpytestはsandboxの既定tmp directoryに作成できず、journal19件がsetup errorとなった。
workspace内のGit除外された専用basetempで再実行し65件全てPASSした。
合成原本bytes・一時URLはテストfixtureやCIへ持ち込んでいない。

## 未解決事項と再開に必要な承認

1. 追加行の原本リンク自動補完がtableの計算列設定に由来するかをread-onlyで特定する。
   断定はまだしていない。列の自動補完に依存してidentityを作らない方針が必要。
2. **隔離中の専用ブックをどう扱うか、再開前にユーザー承認が必要**。
   追加行の削除やtable列の設定変更を自動で行わない。
   UI確定仕様に影響する設定変更なら、その理由・影響・入力保持を提示し事前承認を得る。
3. その後、同じ専用ブックだけで案件保持を確認して削除・sortの実測と隔離を再開する。
   並べ替え可能な本番UIは未完成。検索中心・管理外構造変更で停止する暫定方針。
4. native handoffの全purposeテスト、永続CASの追加stale/process crash/response-unknown検証、
   未送信入力とjournalが異なる再生成の保存手順、専用Linux CIを完了する。
   Sheetsとjournal間の原子的transactionはなく、2回同値読取りだけでは後続編集を防げない。
5. 既存hostのcanonical page/Unit/明細snapshot読取り、Drive revision IDとnative versionの
   明示的な対応付け、一般の複数明細カテゴリ、PDF構造選択のnative操作への対応が必要。
6. iPhone実機では、今回の専用ブックの検索・原本往復・入力保持・縮小表示・hold・
   横スクロール・固定表示・種類別対象外欄と、構造変更禁止の運用を再確認する。
   隔離中の現ブックでは入力テストを再開しない。
7. 実host接続にはowner/session/CSRFと追加ユーザーの明示認可、
   明示POST要求・目的別validator・source/input再検証・duplicate/replay/revision/read-back・
   既存writerだけへの接続について別途設計確認と有効化承認が必要。
   認証付き実POST受付、本番OAuth、Cloudデプロイは今回実施していない。

#123/#124/#127はhead/base/Draft不変。#128もDraft維持、main mergeなし。
本番記帳、原本移動、AI送信、実authority更新、権限変更、Cloud変更は全て0。
Apps Script / Cloud Run / 新サービスの実装・デプロイはしていない。
