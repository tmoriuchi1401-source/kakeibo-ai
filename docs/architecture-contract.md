# KakeiboAI architecture contract

Version 1 — 2026-11-26, Issue #207 owner instruction.

This is the single normative architecture source. Apply it to all KakeiboAI
subsystems, production entries, helpers promoted to production, and later PRs.
Product automation is the goal; routine human work is limited to exceptions.
Existing implementation or a passing CI job does not redefine these requirements.

## Permanent owner requirements

| ID | Mandatory requirement |
|---|---|
| A01 | GitHub-hosted runnerで本番実行する。 |
| A02 | Windows PCは開発・保守専用。電源OFF・ログアウトでも本番運用可能。 |
| A03 | 日常操作はiPhoneで完結する。 |
| A04 | Google Driveの各inboxを定期巡回し、Gmail等の既存自動取得経路も維持する。 |
| A05 | 未処理データを自動取得・解析・分類し、金額・カテゴリ・重複等を検証する。 |
| A06 | 正常案件は人の承認なしで自動記帳する。 |
| A07 | 不整合・判断不能案件のみ確認シートへ表示する。 |
| A08 | 人が必要箇所を修正し登録を1回行えば、backend再検証、記帳、読戻し、結果表示まで自動で完結する。 |
| A09 | 記帳完了後に原本を処理済み管理する。未完了・結果不明原本は保持する。複数ページ・複数案件は全対象の完了を要する。既存processed内も同じ。 |
| A10 | 医療は完全手入力、外部AI送信禁止。医療・プライバシー判定不能原本もAIへ送信しない。 |
| A11 | 収入は銀行入金を正本とし、Payrollは独立システムとして維持する。 |
| A12 | Google側に永続状態を保持し、runner消失後も重複記帳を防止する。 |
| A13 | PATのGoogle側への中継は禁止する。 |
| A14 | 原則として既存サービスの無料枠を活用。Cloud Run、常駐サーバー、self-hosted runnerを採用しない。 |
| A15 | 既存機能、履歴、カテゴリ、iPhone UI、未送信入力、STOP・UNKNOWN・恒久フェンスを破壊しない。 |

These requirements cannot be changed by a PR, a local workaround, or a temporary
test configuration. Any change requires separate explicit owner approval naming
the affected IDs, scope and safety impact. Preserve the decision reference in
version control. Never infer an exception from credentials being available.

## Phase restrictions, implementation choices and approvals

* **Current phase:** financial writes OFF; no real AI, new credentials, original
  moves/deletes or fence release. The subsequent 2026-11-26 owner instruction
  permits fixed isolated Google connections/settings and synthetic hosted E2E
  dispatch. Production Script overwrite, scope expansion, new grants/infra,
  production schedules, main merge and validated-SHA promotion remain excluded.
  A later explicit owner instruction permits a test-only standard Google
  project, dedicated OAuth client and grant within the existing scopes if no
  safe existing isolated Script is available. This exception is limited to
  authentication isolation; existing production OAuth/project/Secrets remain
  unchanged. See [isolated authentication setup](receipt-isolated-auth-setup.md).
  Isolated memory writers may exercise the real materializer. Their
  successful readback is NOT production posting/completion authority.
* **Temporary safety bounds:** limited intake/pending counts, capacity stops,
  fixed test cases, time windows and existing permissions remain enforced.
  They are not the permanent product definition. Do not relax them implicitly.
* **Implementation choices:** prefer existing receipt scope, workflow, validator,
  writer, category master, Google store and UI. Polling interval/workflow split
  is a measured choice, not a new invariant. Do not fix a 30-minute interval.
* **Separate approvals:** main merge, production connection/deployment/state
  migration, scope/new credential changes, real canary financial authority and
  scheduled activation. The fixed isolated connection authorization is recorded
  in [live connection status](receipt-live-connection-status.md); it grants none
  of those production operations.
* New infrastructure, a major authentication change, or weakened safety contract
  stops that dependency only; continue independent authorized implementation.

## Required execution and safety paths

```text
existing hosted Actions receipt owner + kakeibo-production concurrency
  -> fresh original/privacy/duplicate checks -> Google reservation BEFORE AI
  -> existing analysis/validator
       normal -> existing financial contract/writer -> exact ledger readback
       exception -> existing 1-tab UI -> one registration/frozen snapshot
                 -> backend revalidation + financial contract
                 -> same writer -> exact readback -> same-case result
  -> all-member native completion -> original completion management
```

Human registration is an input request, not a fabricated OIDC identity or posting
grant. A normal receipt does not require that request. A stored analysis must be
reused to create its exception case; no AI replay just to change UI pathways.
Medical manual entry keeps its native validator and source/duplicate safeguards.

Use the same source ID/bytes hash/version and whole-source/Unit ownership across
normal, exception, legacy confirmation, reimport and PDF/canary paths. An absent
local SQLite file or a new UUID cannot reset ownership. Pending/UNKNOWN/STOP or
missing/corrupt Google state fails closed. Reserve before each side effect;
unknown delivery is read-only reconciliation, never automatic resend.

The existing Drive read/write/read store is NOT a distributed CAS lock. The
single Actions owner and its shared concurrency are required; Apps Script
registration/claim/finish retains its ScriptLock. Do not bypass that lock with
direct Sheets PUT or enable an uncoordinated legacy/remote writer.

Original retention requires fresh independent accounting completion for ALL
members, immediately before deletion. Folder position, normal provenance,
processed timestamp, import ID, one completed Unit, and synthetic completion are
insufficient. Missing historical proof retains originals without migration.

PATs stay out of Google arguments, properties, logs and HTTP adapters. Public
GitHub run metadata is not authentication. Existing owner OAuth is the first
connection candidate; do not add signatures/permits unless existing mechanisms
cannot satisfy the independently verified gate. Authentication failure, STOP,
expiry or unknown authority denies effects. Started writes cannot be cancelled
merely by expiring a permit.

## Enforcement and current status

`tests/test_receipt_hosted.py` exercises the actual materializer, correction
validator and existing durable receipt-store implementation. Architecture tests
also inspect the real workflow/entry wiring; document existence is insufficient.
Existing sensitive/network-denial, writer/state and subsystem regressions remain.

Implementation and limitations: [Issue #207 implementation record](issue-207-implementation.md).
Subsequent adapter evidence and activation conditions:
[PR #208 connection results](receipt-hosted-connection.md).
Existing cash/category PAT migration and receipt-buyback income reconciliation
are separate changes. Their known violations do not become approved exceptions.
Production remains **NO-GO** until the implemented native financial adapter and
PAT-free review transport are accepted live, and actual Google durability,
iPhone and PC-OFF schedule acceptance are independently demonstrated under
separate owner approval.
