# Issue #207 implementation and acceptance record

Normative requirements: [architecture-contract.md](architecture-contract.md).
Base main: `fa7ac9378ef51e4a51258dd6053afe52ed0a2bb8`.
Reviewed #205 `b69590d298c36895945327ff1c0f9412e22b5e7e` and #206
`7f583b67d919e8d439f5e1608af7e28287af60d8`. No wholesale stack adoption.

## Implemented offline connection

Existing `kakeibo-production -> production_flow -> production_source.receipts`
remains the owner/host/queue. The existing receipt scan plan selects the new
composition only when the existing private receipt store explicitly contains
enabled `receipt_hosted_v1`. Missing/corrupt state is never bootstrapped.
No public workflow flag, workflow, resource, OAuth client or credential is added.
Private setup itself is unperformed and needs separate connection approval.

`receipt_hosted` uses `ReimportStore.save` and the same Google Drive receipt file;
all old manifest/records/confirmation keys are preserved. AI, case publication,
writer and result-publication intents are durable before effects. New instances
restore them; incomplete attempts and STOP deny replay. Source ID/hash/version
and existing confirmation ownership are checked. This requires the existing
Actions lock; read/write/read is not advertised as CAS.
The existing fixed ten-source reimport manifest cannot represent arbitrary new
sources or registration consumption. A namespaced record section in that same
file is the minimal extension; the fixed manifest/old records are not rewritten,
and no new database or Drive resource replaces their history.

Normal receipts bypass review. Exceptions use saved parsed output, candidate
rows and the unchanged 98-column/single-registration snapshot contract extracted
from #205. Only four pure files are selected: item correction planner, cell
decoder, source checks and case/snapshot validator. The case file omits the old
all-receipt ReviewPipeline and reuses its UUID check without importing the P1
trial authority/context machinery. Windows task/DPAPI/SQLite/permit code and its
new CI/workflow/credentials are not imported. Existing original PRs stay intact.
The existing #201 test-only httpx compatibility dependency is also selected;
current GenAI SDKs no longer install the older test import transitively.

Registration consumes a frozen management snapshot, claims once, revalidates
source/category/amount/tax/duplicate conditions, reaches the unchanged
`ReceiptPipeline._materialize_result` through its existing in-memory PlanningDB,
independently compares every generated row, and publishes the same-case result.
Synthetic completion is permanently distinguished from accounting completion;
it grants neither source movement nor deletion. Financial write remains OFF.
Existing medical complete-manual confirmations keep their native writer during
the normal-owner cutover. Unconnected multipage ownership remains held rather
than bypassing the whole-source owner through the old Unit branch.

## Connection dependency explicitly stopped

The old Apps Script receiver requires a Google-held PAT gate. It cannot be used
under A13 or bypassed without changing that safety contract. Production therefore
does NOT instantiate that transport. Pending review processing is wired to the
existing receipt entry, but its live port remains closed; analyzed data is kept
for later connection without AI replay. Existing owner OAuth/Execution API is
the first candidate, not newly implemented authentication. No HMAC/OIDC/new
permit infrastructure or fake financial authority is introduced.

The native live financial authority adapter is also deliberately disconnected.
The isolated writer is the only executable new sink. A configuration value
cannot activate financial writes, and an existing unposted request cannot become
posted merely by changing a flag. Live transport, authority/whole-Unit ownership
and source/readback acceptance need separate connection approval and proof.
This is offline end-to-end implementation, NOT a complete production connection.

## Original protection

`needs_review`, legacy already-imported IDs, validation-only and synthetic results
cannot archive an original. Only explicit accounting-readback completion allows
the ordinary archive predicate. Existing native confirmation archiving retains
its source, durable intent and exact-readback checks.

Retention now requires an independent completion callback on selection AND
immediately before deletion. CLI uses fresh existing Google confirmation/source/
ledger checks. Legacy processed originals without proof are retained. PDFs are
retained until complete native all-member proof is connected; one Unit or one
case never authorizes deleting a whole PDF. No original is moved/deleted here.
Normal auto imports without historical proof may consequently accumulate in
inbox/processed until reconciled; this conservative behavior is intentional.
The existing ordinary production materializer now opts into exact independent
receipt/item/import row readback before returning archive permission. Its writer
body and identifiers are reused; failed readback cannot yield completion.

## Separate change units (not mixed into receipt implementation)

* Cash: `MANUAL_GITHUB_TOKEN` in Script Properties; existing instructions request
  repository-scoped Actions read/write + metadata. Category:
  `CATEGORY_GITHUB_TOKEN` in User Properties (legacy staging via Script Properties),
  Actions write + metadata. Receipt legacy gate also has a token property.
  Actual presence, current permissions and deployed versions are UNVERIFIED.
  Migration should retain UUID/cancel/captured inputs/result UI and replace
  dispatch with the existing hosted queue pattern. Inventory, readback, drain
  and only then separately authorized token removal/revocation. No token is
  inspected, issued, removed or revoked here.
* Receipt buyback: old `receipt_buyback` income conflicts with A11. Keep historical
  rows unchanged. Separate change must make bank deposits authoritative and hold
  unmatched buyback evidence without adding receipt income; do not silently
  classify a later deposit as a second income or rewrite prior cash history.
  Existing income code is not changed in this PR.

## Restart dependencies (no activation performed)

Core processes Amazon/au PAY balance and common review/reconcile/auto-expense.
PayPay's dedicated 08:11 scope imports CSVs only; core must later run for common
accounting. Enabling PayPay alone does not complete posting. Existing card/native
pending can block readiness; #193's isolation fix remains a separate Draft.
Amazon needs a valid checkpoint/overlap window, otherwise the existing bounded
catch-up. Bank and card require their current native authority/window/checkpoint,
recovery evidence and ready state. Receipt requires the normal privacy/OCR
runtime and bound private receipt state. Payroll remains independent.
All workflows require their own flags and validated/current SHA contracts;
actual Variables/enabled states/pending/ACL are not inferred from old logs.

## Post-change A01–A15 status

| IDs | Implementation evidence / remaining acceptance |
|---|---|
| A01/A02 | Existing hosted receipt entry and explicit github-hosted/Linux guard; no Windows/local-state dependency in new composition. Actual PC-OFF schedule untested. |
| A03/A08 | Existing one-registration snapshot through revalidation/materializer/readback/result in isolation. Live port/native authority/iPhone acceptance outstanding. |
| A04 | Existing inbox/Gmail workflows unchanged; flags remain unchanged. Actual current operation unverified. |
| A05/A06/A07 | Normal bypasses review and reaches existing isolated materializer; exceptions retain/reuse parsed output. Live financial adapter remains OFF. |
| A09 | Missing/all-member-unknown completion denies archive/retention, including legacy processed files. Actual originals untouched. |
| A10 | Existing privacy/manual medical boundaries retained and negative tested; no real AI. |
| A11 | Bank/Payroll separation unchanged; receipt-buyback issue explicitly outstanding as separate unit. |
| A12 | Existing Google receipt store, pre-effect intents/reconstruction/STOP/UNKNOWN in isolated transport. Real Google recovery/ACL not tested. |
| A13 | No new PAT relay; incompatible old receipt port is closed. Existing cash/category PAT migration outstanding separately. |
| A14 | No infrastructure/workflow/auth service added. Actual quota/latency not measured. |
| A15 | Old state keys/inputs/snapshots/financial components retained; impacted regression and existing CI verify code. Actual data not accessed. |

## Validation and evidence reuse

Tests exercise the real production source selection, materializer, frozen
snapshot validator and ReimportStore with fake external transports; live sockets
remain denied by existing conftest. Negative cases cover forced review/missing
writer terminal through endpoint assertions, privacy, invalid registration,
copy/replay, lost AI/result replies, fresh-runner restore, missing/STOP state and
retention revocation. Architecture checks run within existing synthetic CI.

Reuse prior evidence only when the component blob, invoked path, principal/
authority, source identity and safety contract are unchanged. #206's 5,329-pass
CI is a reference for retained pure logic, not a claim that this changed tree
passes. Existing bank/card/Amazon/PayPay/manual/medical/Payroll tests are retained;
normal CI runs the repository suites once. No old real UNKNOWN case is retried.
No new full device/Cloud experiments or repeated unchanged real canaries.

Local/CI outcomes are recorded in the Draft PR against the actual head SHA.
Remaining acceptance: authenticated PAT-free review port, existing native writer
authority adapter, all legacy/whole-Unit ownership/in-flight audit, real Google
crash recovery, iPhone registration, normal unassisted posting and exception-only
display, all-member original completion, independent ledger readback, medical
AI 0/duplicate 0/UNKNOWN resend 0, unrelated finance unchanged, and real scheduled
execution with PC OFF. Limited proof, financial permission and recurring
activation remain separate owner GO decisions. Production **NO-GO**.
