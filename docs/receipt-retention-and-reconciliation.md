# レシートの長期保存・対応確認

原則は **原本1コピー・長期履歴は軽量metadata・短期ログはTTL**。
原本のPDF、PNG、crop、比較画像を確認履歴ごとに複製しない。必要時に
`source_file_id` の原本から表示する。会計行を物理削除しない。

## 実装範囲と有効化境界

この変更は新規metadataのschema、保存adapter、認証済み対応確認サービス、既存
「PDFページ確認」のカード生成・非表示リクエスト、cleanup **dry-run** を提供する。
本番サービス、Apps Script、trigger、schedule、既存会計writerへはまだ接続しない。
Firestore adapterは `write_enabled=False` が初期値。実データのmigration、削除、
新Sheet・新日常UI作成、HGA発行、会計write、Medical処理、AI送信、PDF移動は行わない。

既存HGA endpointは変更しない。`reconcile_receipt` のlive受付には、このadapterを
既存Google OIDC認証hostへ接続する別canaryが必要。未接続のコードを「live完成」
とは扱わない。履歴eventやUI projectionはGemini送信・会計writeのauthorityではない。

## 保存クラス

`app/receipt_retention.py` の `POLICIES` が正本。時刻はtimezone付きUTC。
`schema_version` / `retention_class` / `created_at` / `expires_at` を明示する。
永久履歴・current stateにはTTLを設定しない。

| データ | class | 方針・TTL |
|---|---|---|
| 会計ledger、原本、source/page/Receipt Unit binding、ledger identity | permanent | 10年以上。cleanup対象外 |
| HGA decision、同一／別・duplicate判断、Medical manual confirmation | permanent | actor・時刻・digest・識別子のみ |
| replay用confirmation receipt、current state、provenance enum | permanent | 過去の記帳・確認identityを失わない |
| 将来のvoid / replacement decision | permanent | 元ledgerを保持。今回の実行handlerなし |
| 解析・segmentation詳細 | medium | 90日。私的な必要最小限の診断のみ |
| 障害調査metadata | medium | 180日 |
| OCR reason code・retry詳細 | medium | 30日。OCR全文は保存しない |
| 暗号化Actions diagnostic | medium | 既存の最短1日を維持 |
| debug log | ephemeral | 3日。Secrets・token・個人情報・raw requestを含めない |
| OAuth session、one-time確認request | ephemeral | 600秒 |
| temporary processing state | ephemeral | 1時間。ledger pending recoveryはこれに分類しない |
| fresh PNG / crop、Gemini raw response、OCR全文 | ephemeral | TTL 0、処理終了時に破棄。恒久保存しない |

medium詳細も、原本bytes・token・Medical内容をコピーする許可ではない。
長期provenanceは `gemini / human / human_override / missing` のenumだけ。
HGAのcurrent grant（操作許可）は対象pageごとのdurable recordとして保持し、認証session
とは別管理する。Medical durable confirmation、writer intent、pending recovery、ledger
照合に必要な記録は **temporary processing** として削除しない。

Firestore TTLの物理削除は即時ではないため、認証・requestはserverで期限を即時検証する。
既存session encryptionと600秒の論理期限を維持する。
このpolicy/reportのJSON時刻は交換用のISO文字列。将来Firestore temporary writerへ
接続する際はnative timestampへ変換し、専用temporary collection groupだけにTTLを
設定する。文字列のまま保存してTTLが動くと扱わない。永久event/current/replayの
collection groupへTTLを設定しない。今回TTL infrastructureは変更しない。
参照: [Google Firestore TTL](https://docs.cloud.google.com/firestore/native/docs/ttl)。

## 軽量append-only event

`app/receipt_audit.py`。最大UTF-8 JSON **2,048 bytes/event**、未知field拒否。

```text
schema_version, retention_class=permanent
event_id, event_type, confirmed_action
source_file_id, source_content_hash, page_count, page_number, page_identity
receipt_unit_id, review_identity, revision
ledger_id, candidate_ledger_id, decision
actor_id, confirmed_at, authority_digest, request_digest
reason_code, provenance, event_digest
```

`event_id = SHA256([receipt-confirmation-v1, request UUID])`。同じUUIDは年が変わっても
同じevent IDとなる。`actor_id` は検証済みGoogle issuer/subのdigest。自己申告の
email/name、Sheet内のactor、script ownerは本人証明に使わない。
`event_digest` はその他の全fieldのcanonical JSON digest。`reason_code` は定義済みenum。
原画像、OCR/AI全文、HTTP全文、token/cookie、UI/Sheet全snapshotはfield自体を許可しない。

eventは作成のみ。変更・削除interfaceを提供しない。IAM運用も履歴の更新・削除を認めない
専用権限・監査とする必要がある（今回IAM変更なし）。改ざん耐性はdigestだけに依存しない。

## Current stateと年partition

`FirestoreAuditRepository` の配置:

```text
kakeibo_receipt_audit/{private scope digest}/
  years/{UTC year}/events/{event_id}   # append-only
  current/{entity_key}                # 現在状態、最大2,048 bytes/doc
  receipts/{request_key}              # permanent replay receipt
  pairs/{source/page/receipt + comparison ledger digest}
```

currentはidentity、state_revision、status、ledger_id、last_event(year/id)、
last_authority_digestのみ。履歴arrayを持たず、日常処理は1 current docを読む。
監査はevent ID / ledger / sourceの既知referenceから該当年を読む。
大量検索用index・監査UIは今回追加しない。

event・permanent replay receipt・current pointer・pair判断を同じFirestore transactionで
保存。比較対象current revisionが異なれば `HTTP_412`。transactionの最大試行は1回。
書込み結果不明なら `audit_commit_unknown_readback_required` として停止し、receipt/eventを
read-backして `written / not_written / unknown` を判定する。自動retryしない。
保存後はreceipt/event/currentをexact read-backする。

Google Sheets会計writeとFirestoreは跨るatomic transactionではない。将来writer接続時は
既存durable intent・read-backで会計完了を証明し、その後metadataを記録する。
history保存やUI更新が失敗してもledgerを再追記しない。既存writer recoveryを維持する。

## 同一／別／判断不能

既存「PDFページ確認」に縦カードとプルダウンを生成する。新Sheet・PC専用menuは不要。

| 選択 | event / current | 会計・duplicateへの意味 |
|---|---|---|
| 同一レシート | reconciled_existing、terminal | current source/page/receiptを既存ledgerへ結び付け。会計write 0 |
| 別のレシート | confirmed_distinct、未終端 | このsource/page/receiptと比較ledgerのpairだけ除外。その他duplicate検査は維持 |
| 判断できない | reconciliation_required | terminal eventなし。確認一覧に残る |

別のレシートは記帳完了まで確認一覧に残る。duplicate確定も既存の証拠・認証が必要。
同一確認は **本人による取引の対応判断**。異なるPDF bytesが同じであるとの偽の証明や、
privacy変更、HGA発行、会計追記許可にはならない。

`ReconciliationConfirmation` はprotected request record → `VerifiedActor` → fresh source/
page/review/revision → exact既存ledger snapshot → conditional metadata保存を辿る。
bindingはUUID、identity、比較ledger ID/snapshot digest、decision、current state revision。
actorは本人1人allowlist、request UUID/digest/検証時刻に固定。期限は600秒。
Google署名・issuer/audience/nonce等は既存OIDC verifierの責務。hostはOrigin/CSRF/state/
nonce/PKCEを維持し、明示POSTだけをtrusted assertionとしてこのサービスへ渡す。
`explicit_post=True` を外部JSONから直接採用してはいけない。

同じrequestのreplayはpermanent receiptへ一致確認しevent追加0/current変化0。
期限後のURLは拒否する。request/sessionが物理TTL削除されてもpermanent receiptは残り、
UUIDの別binding流用や翌年partitionへの二重appendを防ぐ。

## 確認一覧から消す方法

pending / requires_review / reconciliation_required / needs_human_completion /
needs_review / ready_to_write / confirmed_distinct は表示。
imported / manual_imported / medical_manual_imported / reconciled_existing /
duplicate_confirmed / intentionally_skipped / 将来のvoidedは通常一覧から非表示。

`visible_cards()` はoffline view。既存入力を持つlive Sheetをこのfiltered subsetで
`publish_cards()` し直してはいけない（既存gridの再生成は入力消失を招く）。
`visibility_requests()` はtrusted current stateに基づく `hiddenByUser` だけを生成する。
セル値・数式・入力済みMedicalカード・履歴をclear/deleteしない。hidden row markerは
表示位置を探すためだけで、authorityではない。投影更新失敗後も同じeventを再appendしない。
実際のlive非表示処理への接続は次canaryとする。

## p1と互換性

p1の入力と既存Medical ledgerが一致しても、別原本との対応が未証明なら勝手にterminal化
しない。既存MedicalのAUTO/candidate値を採用せず、元行を更新・削除・再追記しない。
本人が双方を見て認証付き「同一レシート」を明示すれば、current p1 identityと既存ledgerを
reconciled_existingで結び付ける初回実ケースにできる。今回はそのlive操作を行わない。

Gateはentity単位。p1が判断不能でもp4/p10/p14の別page処理を全体停止しない。
ただし親PDFは全Page/Receipt Unitの会計read-back済みterminalを確認するまで移動禁止。
履歴eventだけを根拠にarchiveを許可しない。

新規eventから採用する。既存v1 authority、Medical durable state、legacy diagnosticは
自動変換・自動TTL付与・削除しない。新UI adapterは既存card schemaを利用し、本人検証は
既存VerifiedActorを再利用する。旧「一般」を新HGA/Gemini permissionへ昇格させない。
全旧運用の肥大化対策を完了したとの判断には、個別のcutover・既存保存場所の棚卸しが必要。

## Cleanup dry-run

```powershell
python -m app.receipt_retention_report
python -m app.receipt_retention_report --inventory private-metadata-only.json --now 2026-10-08T00:00:00Z
```

初期実行はoffline synthetic。実inventoryは明示したmetadataのみ、最大1MiB/1万件。
filesystem再帰検索・原本推定・token読取はしない。永久kindまたはpermanent classの
どちらかなら保護。未知legacy、class/TTL不整合、別namespaceは拒否・保持。
削除executor・scheduleを実装しない。将来有効化前に対象件数/bytes/kindをreviewし、
正確なobject generation/digest/policyを再照合して短期artifactだけを削除する。
permanent ledger/source/decision/Medical confirmation/actor/time/digestは対象外。

実測synthetic dry-run: 候補3件、74,752 bytes（解析1、retry1、session1）、
永久保護2件、legacy拒否1件、**削除0・live write0**。実保存場所の削除候補件数とは異なる。

既存workspace `.private` の直下だけも、内容を読まずfile metadataで棚卸しした。
123 files / 9,124,178 bytes。旧fileには新retention metadataがないため全123件を
未分類として保持し、削除候補0・削除0。名前からTTLを推定していない。Drive/Firestore/
Git履歴・subdirectory全体の棚卸し完了を意味しない。結果はrepo外private reportへ保存。

## 10年容量試算

`receipt_retention_report.samples()` の代表8 eventをUTF-8 canonical JSONで計測。
平均 **1,244.25 bytes/event**、最大 **1,275 bytes**。実行時に再計算可能。
replay receipt平均480 bytes、current平均885.625 bytes、pair sample216 bytes。
全eventが別entity/pairを新設する保守的な合計を併記する（通常currentは再利用）。

| 10年間event数 | event本体 | replay/current/pair込みの保守的metadata |
|---:|---:|---:|
| 10,000 | 12.4425 MB | 28.25875 MB |
| 100,000 | 124.425 MB | 282.5875 MB |
| 1,000,000 | 1.24425 GB | 2.825875 GB |

十進bytes換算。Firestore document/index/backupのoverhead、原本、ledger、有限TTL診断は
含まない。上限2KiBのevent本体だけなら100万eventでも2.048GB以下。
家庭用のmetadata容量として10年以上を支えられる規模。ただし原本/会計件数は自然増加し、
「容量が一定」にはならない。年partition・有限diagnostic TTLで1巨大JSONと画像複製を避ける。
コスト見積りやlegacy全保存場所のcleanup完了をこの数値だけで保証しない。

## 将来のvoid / replacement backlog

今回void event実行・button・ledger/report変更は追加しない。将来は元ledgerを保持し、
`event_type=void, ledger_id, source/page identity, reason_code, actor_id, voided_at,
authority_digest` をappend-onlyで保存する。日常一覧/集計/グラフは別の検証済みprojectionで
除外する。取消後もsource/ledger/replay履歴は永久保持し、自動再取込を許可しない。
再記帳は `void → explicit replacement/re-entry authority` を別設計する。

## 検証

`tests/test_receipt_retention_audit.py`: 本人actor、UUID/decision/source/page/review/revision/
ledger tamper、expiry、same/different/unknown、pair限定、replay、年越し、atomic CAS、
保存前後timeout/read-back、live write初期拒否、permanent cleanup保護、未知legacy保護、
currentのみ参照、既存カード非破壊非表示をsyntheticで確認する。
Python/Node既存suite、OCR/sensitive/general CIを併せて通す。
