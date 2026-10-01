# Private Drive grouping authority and management review UI

Draft PR #91 implements a manual, disabled-by-default review path. The initial
implementation did not install a live UI or state. The later
[limited live validation](pdf-grouping-live-validation.md) provisions the
dedicated state and capture-only UI; the Actions worker remains disabled.
Confirmation stops at authority storage. No main merge or accounting activation
has occurred.

## Existing infrastructure

Read-only investigation found the existing `KakeiboAI_system_state` folder with
only owner and existing service-account writer grants. Management Spreadsheet
metadata confirmed the existing Drive-backed `領収書確認` and the
`カテゴリ操作`/captured-request mechanism. No business or Medical data was read
for that investigation.

The new path reuses the private state bindings, existing service-account scopes and
credential selection, RSA-wrapped bindings, category-submit Apps Script user
credential/edit trigger, and the `kakeibo-production` concurrency group. It uses
a dedicated precreated JSON file in the same private folder. Existing recurring,
receipt, Medical and run-ledger files/schemas are not extended or overwritten.
There is no new database, auth, OAuth scope, sharing grant or Medical review path.

## Durable state and conditional writes

`DriveGroupingStore` trusts only the pinned Drive file, with schema
`pdf-grouping-authority-v1`. Runtime never initializes missing state and never
imports a local confirmation. `.private/pdf-grouping` is development/cache only;
its local store refuses Actions authority when `GITHUB_ACTIONS=true`.

The envelope contains a binding digest (private folder/file/management
spreadsheet), generation, per-source records, and audit. Each record holds the
current proposal/revision, review status and optional confirmed authority.
The existing proposal version/serialization/digest is retained.

Confirmation fields:

```text
source_file_id, source_content_hash, page_count
pages: page_number, page_hash, classification, extraction_status
proposal_digest, grouping_revision, confirmed_partition
confirmation_digest, confirmed_at, unit_statuses
authority_scope: [grouping_confirmed, rendered_payload_only]
accounting_allowed: false
medical_handoff_allowed: false
archive_allowed: false
```

The confirmation digest covers all fields except itself. Validation reconstructs
the exact expected confirmation from the current proposal. Raw OCR, names,
Medical contents, amounts, receipt identifiers and image bytes are never stored.
Proposal reasons and extraction states are categorical codes only. Unconfirmed
views have an empty authority scope and produce no units.

Missing media, corrupt JSON, wrong schema/binding/source/member hash/proposal
digest/revision or malformed scopes return `grouping_required` with no authority.
ACL checks before every state read/write require exactly owner + existing SA
writer. They never modify permissions. The binding uses an owner identity digest,
not a reviewer identity or a plaintext owner address in Actions Variables.

PDF grouping alone uses `ConditionalDriveStateTransportV2`. Drive v3's File
resource has no `etag`; a live metadata probe also returned no ETag header.
The existing v3 `DriveStateTransport` and its legacy callers remain unchanged.
The separate v2 adapter uses the File `etag` and rejects missing, weak, malformed
or wildcard tags. `read_versioned` binds media to that strong ETag using metadata
reads around download. `replace_versioned` compares expected bytes/tag, sends
`If-Match` on a single file update, and requires exact read-back. A server 412 becomes
`stale_proposal`. Writes never retry; ambiguous delivery is acknowledged only by
exact read-back. No ETag or weak ETag means `state_conditional_write_unavailable`
and zero writes, with no unconditional fallback.

Request shapes were checked against [Drive's conditional-update guidance](https://developers.google.com/workspace/drive/api/guides/performance)
and the [official Python HttpRequest callback interface](https://googleapis.github.io/google-api-python-client/docs/epy/googleapiclient.http.HttpRequest-class.html).
The v2 [isolated live preflight](pdf-grouping-v2-preflight.md) verified current-tag
HTTP 200, stale-tag HTTP 412, new-tag HTTP 200 and exact content read-back using
the same service account. The preflight file contains synthetic data only and
is not an authority file. The subsequent limited authority/UI installation is
documented separately; worker activation remains a separate phase.
A failed preflight must stop, not trigger new auth/permissions or a weaker path.

## Spreadsheet projection

`GroupingSheet.install()` adds two owned tabs with explicit sheet ID/header
checks. It never calls `ensure_schema` or writes existing accounting headers.

- `PDFページ確認`: state, page count, group numbers/page ranges, privacy classes,
  categorical reasons, original link, operation, target group and processing
  result. Source ID/hash, proposal digest/revision, row token and confirmation
  digest columns are hidden.
- `_PDF確認受付`: hidden request UUID, delivery state, captured snapshot,
  submission time, result and completion time.

The UI uses wrapped text, frozen headers, yellow input cells and native dropdowns,
consistent with existing confirmation UI. It shows no OCR, merchant/facility,
amount or Medical details. Original links retain existing Drive permissions.
No public page previews or sharing changes are created.

`PdfGrouping.gs` joins the existing category-submit Apps Script project. Its
updated `Code.gs` delegates PDF edits through the existing installed edit trigger
and provides a manual candidate refresh menu. The auth manifest is unchanged.
Dispatch, when separately enabled, uses the existing `CATEGORY_GITHUB_TOKEN`
user property and UUID-only inputs. By default, the absent
`PDF_GROUPING_DISPATCH_ENABLED` script property means capture only: no token is
needed and no HTTP request is made. A manual operator processes the captured
UUID. No new token/scopes/triggers or source values in GitHub inputs are needed.

After reviewing the original and candidate, choose **確定** in 操作: one edit
captures the confirmation request. Automatic dispatch requires explicit opt-in.
For 分割/結合, select the target
first: `1` splits Group 1 into singleton pages; `1+2` joins adjacent Groups 1/2.
Edits with an actually changed partition allocate a new unconfirmed proposal and
revision; review it and explicitly confirm. An unchanged partition preserves
revision, confirmation and Unit IDs. 拒否/保留 revoke confirmation. Rejected
proposals remain rejected; held proposals can be confirmed again by a new request.
Bulk edits do not dispatch. Busy/ambiguous submissions cannot be replaced or
automatically resent. HTTP 204 indicates dispatch acceptance only.

## Operation and recovery

1. Load/validate Drive state and re-fetch original metadata/bytes with read-only
   credentials. Check the pinned inbox, PDF type and version around download.
   Run the unchanged all-page local privacy observation. Preserve restrictive
   provenance from the Drive正本, never from local confirmation.
2. Generate/reuse a proposal and save/read-back before displaying it.
3. Compare captured intent with Drive's source ID/hash, proposal digest/revision
   and row token. Displayed page count/ranges/privacy/reasons/link must also match.
   Spreadsheet values provide intent, never authority.
4. Re-read source/state at review, validate complete contiguous partition
   coverage, then conditionally save authority and audit together.
5. Display `確定済み・処理保留` only after exact read-back.

Source content, count, member hash, classification/extraction or proposal/revision
changes revoke the old confirmation and reject old operations as `stale_proposal`.
Server conditional updates prevent overwriting a save in the read/write gap.
The manual workflow also reuses existing production exclusion; schedules do not
change. Grouping changes never rewrite privacy classification.

Drive is the正本. A failed Spreadsheet update cannot roll back authority. The
saved request ID/intent digest/audit result permit retrying projection after
confirm or edit without a new revision/Unit ID. If Drive cannot be verified,
the row is marked `authority未確認・再表示必要` and its displayed confirmation
digest cleared. Save/read-back failure cannot produce a success display.
Replaying an earlier confirmation after a subsequent hold cannot revive it.

The same confirmation reuses timestamp/digest/revision/Unit IDs across independent
workers and empty local caches. New requests confirming the same unchanged
proposal also preserve these values. Only a partition change allocates an edit
revision; source changes require a new proposal and human confirmation.

Audit stores operation, UTC timestamp, source identity, before/after revision,
proposal/confirmation digests, result code, request UUID and intent digest.
No OCR, Medical content, amount, image or credential is stored. Actor is omitted:
the existing mechanism does not securely attest an individual Google reviewer,
and the SA/owner is not inferred as actor. Stale attempts are audited when the
Drive state is valid/writable; unavailable/corrupt Drive cannot save its own audit.

## Worker activation (not performed)

The added `pdf-grouping-review.yml` is workflow_dispatch only, defaults to
disabled, requires explicitly enabled review and validated main, and has no
schedule, Gemini key or processed-folder binding. Existing scheduled workflow
files, existing Variables/Secrets and Apps Script auth scopes are unchanged. The
limited installation adds only `PDF_GROUPING_BINDING`; review enablement remains
unset. It does not alter the validated main SHA.

After a separately authorized deployment, the owner must precreate the dedicated
file using existing inherited private grants. Operator initialization uses
`encoded(empty_state(_digest([folder_id, file_id, spreadsheet_id])))`; it is not a
runtime missing-state fallback. `PDF_GROUPING_BINDING` holds `folder` wrapped
with `KAKEIBO_STATE_FOLDER_ID`, `file` wrapped with `PDF_GROUPING_STATE_FILE_ID`,
and `owner_digest = _digest(owner_email)`, using the existing SA key.

Verify isolated ACL/ETag/conditional-update/read-back, install the owned tabs and
update the existing Apps Script files before enabling the dedicated manual path.
Stop if that requires authentication/permission changes or conflicts with the
existing state platform. The limited live validation has provisioned state and
installed the capture-only UI. Workflow activation has not occurred.

## Closed boundaries and next phase

Confirmed Medical groups have only `medical_pending` local metadata. No Medical
observer/review/amount/identity/HMAC/admission/store/policy change or handoff exists.
Confirmation invokes no receipt pipeline, AI adapter, ledger or processed mover.
The Sheet adapter reads/writes only its two owned tabs. Ordinary multi-page intake
still stops before AI/DB/Medical access; single-page handling and PDF archive ban
are preserved.

`DurablePdfGrouping.confirmed_units(source_id)` is the next-phase read-only
interface. It validates Drive plus fresh original observations, returns no units
on mismatch, never initializes/repairs state and never consults local authority.
Read-only normal-unit analysis still needs separately authorized AI/destination
scope, fresh member/composite gates and results storage without accounting writes.
The live canary and explicit human confirmation remain prerequisites for the
next phase. Neither provisioning nor successful conditional writes grant
accounting, Medical, archive or AI-send authority.

## Original authority/UI verification

- Full Python suite: **3254 passed**; two existing dependency deprecation warnings.
- Medical selection: **690 passed**; all unchanged existing Medical tests are
  also included in the full successful suite.
- New Drive authority/transport/UI suite: **58 passed**. Covers display,
  confirm/edit/reject/hold, source/member/count/revision/digest mismatch,
  conditional-write races, missing/weak ETag, corruption, local-only state,
  replay, revoked-request replay, read-back/Drive save/UI failures, audit,
  permissions, source bounds, UI ownership/ranges and closed side effects.
- Node synthetic suites: **17 passed**, including four new PDF submission tests
  and unchanged category/manual-entry/cache-run checks.
- Compilation and `git diff --check`: successful. Medical code/dedicated tests,
  page observation, privacy gate, receipt pipeline, Gemini adapter, Drive intake,
  legacy OCR, existing workflow files and Apps Script OAuth manifest unchanged.
- Independent fake workers share one durable state; synthetic transport requests
  are checked for `If-Match`, single media update, no move/create/permissions
  mutations and exact read-back. Native live deployment was not run. The later
  v2 preflight and its regression checks are documented separately.

```text
python -m pytest -q --basetemp ../.pytest-drive-authority-verified
python -m pytest -q -k medical --basetemp ../.pytest-drive-authority-medical-verified
node --test apps-script/category-submit/Code.test.cjs apps-script/manual-entry/Code.test.cjs .github/scripts/verify-state-cache-runs.test.cjs
python -m compileall -q app tests
git diff --check
```
