# Local PDF grouping review (Draft PR #91)

Privacy observations and accounting transaction boundaries are separate. The
existing all-page local PDF observation/classification path is unchanged. Normal
single-page PDF intake still uses a freshly rendered PNG and the existing receipt
pipeline. PDF archival remains disabled. Multi-page production intake still stops
at `grouping_required`, even after a local grouping confirmation.

## Candidate generation

`PdfGroupingService` uses the existing `observe_pdf` function and existing page
privacy manifests. A known sensitive source can also be supplied explicitly.
Persisted restrictions cannot be bypassed by a later normal OCR reading. No
Medical observer/review/storage or amount/identity/HMAC code is called or changed.

`GroupingProposer` separates candidate generation from review logic and UI; the
existing `PageGrouping` interface is retained. `AdjacentPageGrouping` v1 uses
local OCR on already rendered normal pages. Store/facility name, date and receipt
number must all match. An adjacent page pair also needs consistent printed page
numbers (e.g. 1/2, 2/2) or a continuation marker. A preceding total closes the
chain. Store/date alone, pixel/layout similarity or mere adjacency do not suffice.
Weak evidence produces singleton candidates; sensitive pages remain singleton
candidates. Layout similarity and item continuity inference beyond explicit
continuation markers are deferred. These are suggestions for human review.

Raw OCR, names, dates and receipt identifiers are transient and are not saved.
Only categorical reasons and confidence are persisted. Proposal generation
failure retains `grouping_required`, without units or external effects.

## Human review CLI

The command is local and creates no Sheets/Drive/Gemini clients. Use the same
source file ID and privacy manifest directory as ordinary intake. Review the
actual pages locally before confirming; confidence is not confirmation.

```powershell
python -m app.receipt_pdf_grouping show receipt.pdf --source-file-id DRIVE_FILE_ID
```

`show` emits the source hash, every page's privacy/extraction metadata and
proposed groups, including page numbers, classification, confidence and reason.
Copy its `proposal_digest` as `REVIEWED_DIGEST`. Explicit confirmation:

```powershell
python -m app.receipt_pdf_grouping confirm receipt.pdf --source-file-id DRIVE_FILE_ID --expected-proposal REVIEWED_DIGEST
```

Editing replaces the partition and creates a new **unconfirmed** proposal. Each
page must occur exactly once, in source order; every group must be contiguous.
For example, split p1-p2 and retain p3 as a separate group:

```powershell
python -m app.receipt_pdf_grouping edit receipt.pdf --source-file-id DRIVE_FILE_ID --expected-proposal REVIEWED_DIGEST --groups '[[1],[2],[3]]'
```

Review the resulting new digest before confirming. `reject` saves rejected group
statuses, revokes confirmation and leaves the source held. Rejected proposals
remain rejected on replay; edit them to create a new confirmable proposal.

```powershell
python -m app.receipt_pdf_grouping units receipt.pdf --source-file-id DRIVE_FILE_ID
python -m app.receipt_pdf_grouping payload receipt.pdf --source-file-id DRIVE_FILE_ID --unit-id CONFIRMED_UNIT_ID --output offline-unit.png
```

`payload` exports only an all-normal confirmed unit as a fresh, vertically stacked
RGB PNG. It verifies member PNG hashes and privacy/completeness for each member,
then rechecks the exact combined image with the existing mandatory privacy gate.
Size limits apply. No PDF objects, embedded text, metadata or attachments are
copied. The output path must not already exist. This is for offline/mock analysis;
the command never sends it or records a transaction. The real Gemini adapter's
mock transport is tested with the resulting PNG, and originals remain forbidden.

`--state-dir` defaults to `.private/pdf-grouping`; `--observation-dir` defaults to
the existing `.private/pdf-document-units`. Optional `--source-classification`
accepts only restrictive classifications. These directories contain sensitive
source/page metadata and should remain private, outside sharing or public UI.
Custom state directories must be trusted local directories. The CLI does not
overwrite single-page accounting manifests.

## Proposal versus confirmed authority

Each source has one current atomic JSON record, addressed by a digest of its file
ID. `proposal` and `confirmation` are distinct fields; a proposal's status cannot
grant authority. Proposals include source file ID/content hash, algorithm version,
monotonic grouping revision, page count, ordered page snapshot and group records:
group ID, page numbers/range, member hashes, member classifications, group type,
confidence/reason and status. The digest binds the entire reviewed proposal.

`review(..., action='confirm', expected_digest=...)` is the separate human review
boundary used by the CLI. It revalidates the current source snapshot and reviewed
digest, then saves a confirmed record with exact group membership and
`authority_scope: rendered_payload_only`, `accounting_allowed: false`. Editing or
rejecting removes that record. Atomic replace and a per-source exclusive lock
prevent partially written or concurrent conflicting reviews; a busy or corrupt
store fails closed. A stale lock must be investigated locally rather than ignored.

The store trusts the local operator/filesystem; it is not a signed authority for
arbitrary remote callers. A future management UI must authenticate its reviewer
and invoke the same logic, rather than trusting spreadsheet status cells.

## Unit identity, privacy and replay

Only a matching confirmation yields `ConfirmedDocumentUnit` objects. Identity
is SHA-256 over source file ID/content hash, confirmed ordered page numbers,
member PNG hashes, grouping revision and proposal digest. Replay produces the
same unit IDs and reuses confirmation without asking again. Changing the partition
creates a new revision and new unit IDs. Revision survives observation/proposal
failure so old unit identities cannot be revived after a grouping change.

Content hash, page count, member hash, classification or extraction metadata
changes revoke confirmation and return `grouping_required`. Corrupt or incomplete
observations cannot yield units. A stale review digest is rejected, and source
change invalidation is persisted before the error is returned.

Grouping never mutates page classification. Unit classification is fail closed:

| Member classifications | Unit classification / local status |
| --- | --- |
| All normal | normal / normal_preview_ready |
| Any sensitive_unknown or invalid classification | sensitive_unknown / privacy_pending |
| Medical and payroll together | sensitive_unknown / privacy_pending |
| Any medical, without the above conflicts | medical / medical_pending |
| Any payroll, without the above conflicts | payroll / privacy_pending |

Every sensitive unit is barred from payload generation, including a human-made
normal + medical group. `medical_pending` is saved as the confirmed group's local
`unit_status` only; no
Medical handoff/review/data is generated. No confirmed grouped unit has production
ledger or archive authority.

## Next phase

The management Spreadsheet UI needs authenticated reviewer identity, local safe
page previews, exact source/digest conflict checks, complete partition editing,
review audit history and explicit confirmation. UI actions should call the review
service rather than make status cells executable authority. Any sensitive page
previews require access control; they must not be sent to general AI.

Normal multi-page production processing needs a separately reviewed accounting
authority boundary, whole-unit extraction/validation of totals and items, stable
ledger dedupe plus concurrent reservation/recovery, and explicit handling of
re-grouping after an imported unit. Do not enable writes merely because the local
payload confirmation exists. Medical integration, archive activation, production
Actions and deployment remain separate authorized phases.

## Verification

`tests/test_receipt_pdf_grouping.py` covers adjacent/continued same-transaction
candidates, distinct/weak transactions, sensitive mixtures in both orders,
three-page partitioning, proposal failure, edit/confirm/reject, stable replay,
snapshot invalidation, invalid partitions, corruption/concurrency failure,
inherited sensitive provenance, exact payload gates, fresh PNG mock transport,
CLI review/export and zero external effects before confirmation. Existing PDF
tests also preserve single-page processing and prove multi-page intake makes no
AI/ledger/Medical/Drive calls, including replay. All tests use synthetic data and
mock external boundaries.

Validated on this phase:

- Full suite: **3196 passed**, two existing dependency deprecation warnings.
- Medical selection: **687 passed**, including unchanged existing Medical tests.
- New grouping suite: **44 tests**, included in the full suite; existing PDF
  safety suite: **64 tests**, also all successful.
- `git diff --check` passed. No changes against e78c2a0 in Medical code/dedicated
  tests, page observation, privacy gate, receipt pipeline, Gemini adapter, Drive
  routing, legacy text extraction or `.github`.

```text
python -m pytest -q --basetemp ../.pytest-confirmed-grouping-full
python -m pytest -q -k medical --basetemp ../.pytest-confirmed-grouping-medical
```
