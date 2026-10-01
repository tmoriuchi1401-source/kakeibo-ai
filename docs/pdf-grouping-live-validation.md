# Limited live PDF grouping validation

This validation started from Draft PR #91 head `eea39b7c6a667e18ed2bd5f4e75da8c4956e1ef0`
on 2026-10-01. All three PR checks passed before any live mutation. Main remains
unmerged. Grouping confirmation grants only rendered-payload construction;
Gemini, accounting, Medical handoff and source movement remain closed.

## Private authority and binding

Exactly one dedicated `pdf-grouping-authority-v1.json` was provisioned inside
the existing private state folder. Existing state files were not downloaded or
updated. File/folder permissions matched the existing owner and service-account
writer only. No ACL, credentials or scopes were changed.

The initial empty valid envelope was read back and validated against the pinned
folder/file/management-spreadsheet binding. The same service account then used
the v2 adapter for a strong ETag, `If-Match` update and exact read-back. A direct
server update with the old ETag returned **HTTP 412**, with unchanged content
after rejection. No retries or unconditional fallback were used.

`PDF_GROUPING_BINDING` contains only existing RSA-OAEP wrapped folder/file IDs
and the owner identity digest. The local encrypted copy is an operator
configuration, never a local authority fallback. Actual IDs and diagnostic
artifacts stay in the operator workspace rather than this repository.

The state schema strictly permits page/source identities, privacy/extraction
metadata, proposals, confirmed partitions, digests/revisions, categorical
statuses and audit. Confirmation scope is exactly `grouping_confirmed` and
`rendered_payload_only`; accounting, Medical handoff and archive flags are false.
Raw OCR, merchant/facility names, amounts, Medical contents and image bytes are
absent. Drive is the sole durable authority.

## Capture-only management UI

The existing management Spreadsheet received only `PDFページ確認` and hidden
`_PDF確認受付`. The visible tab displays state, page count, partition, privacy,
categorical reasons, original link, operation, target group and result. A and
K:P technical columns are hidden; users do not enter hashes, digests, revisions
or UUIDs. Operation/target cells have native dropdowns.

Before installation, all 35 existing tab structures, named ranges and bounded
sentinel cells/formulas/formats/validations were captured. They matched after
installation and synthetic operations. The HTTP mutation fence rejects writes
outside the two owned tabs, including every accounting tab.

The existing Apps Script project received `PdfGrouping.gs` and one delegation
line in `categorySubmitEdited`. The full saved source was read back and compared.
Existing `onOpen`, manifest and other category code were preserved. The two
existing edit/time triggers remain unchanged; none was created or replaced.

`PDF_GROUPING_DISPATCH_ENABLED` is absent, so edits capture intent in the hidden
queue without GitHub HTTP dispatch. `PDF_GROUPING_REVIEW_ENABLED` remains unset,
the validated main SHA is unchanged, and no workflow or schedule was run or
enabled. Confirmation is displayed only after manual processing has validated
and saved/read back Drive authority.

## Synthetic live operations

The only synthetic source is a non-Drive test identifier with two normal pages
and hashed synthetic evidence. No real receipt/source ID or document content is
used in that fixture.
Synthetic identifiers are displayed as `テストデータ` with
`テストデータ（原本ファイルなし）` instead of a non-existent Drive link. Real
source links retain their existing URL format and permissions. This projection
correction does not mutate Drive authority or enable actual PDF processing.

| Operation | Live result |
| --- | --- |
| Display | One p1-p2 proposal, revision 1, no authority |
| Select 確定 once | Hidden request captured; manual processing saved/read back authority, then projected confirmed/processing-held status |
| Replay same confirm UUID | Same confirmation digest/time, Unit ID and revision; no Drive update or extra audit |
| Select target 1 + 分割 | New p1 / p2 proposal, revision 2; old confirmation revoked |
| Reconfirm | Revision 2 retained; two stable confirmed Unit IDs |
| Old revision 1 request | `stale_proposal`; current revision 2 authority unchanged |
| Select 保留 | Confirmation revoked; authority, audit and projection all held |
| Redisplay | Held state retained, no Units or authority revival |
| Inject projection failure after a fresh synthetic confirmation | Drive authority/audit saved once; captured request remained recoverable |
| Replay that request to repair projection | UI repaired; zero further Drive updates/audit additions; confirmation and Unit identities unchanged |
| Change synthetic source hash | Old confirmed authority revoked; `grouping_required`, revision 3; real originals untouched |

Each authority mutation uses the dedicated v2 conditional transport and exact
read-back. Audit records operation/time/source identity, before/after revision,
proposal/confirmation digest, result and captured-request identities only.
No reviewer actor is guessed. The mutation fence permits only conditional PUT
to the dedicated state and POST to the two owned review tabs; source reads use
existing read-only Drive credentials. ReceiptPipeline, AI clients, Medical
observer/store/handoff and move writers are never invoked by this path.
The final synthetic state is unconfirmed at revision 3 after the hash-change
test. The hold/redisplay result above describes that earlier test step.

## Initial actual PDF canary stop

The human selected one Receipt Inbox PDF. Its identity/parent/type/version were
verified around read-only download. The unchanged local observer rejected it as
`pdf_resource_limit_exceeded` before rendering or OCR. No proposal, page privacy
classification, confirmation or real-source authority was created; the two
review tabs and Drive state/audit were not updated by this attempt.

A separate read-only geometry diagnostic (no rendering/OCR) found 14 pages and
15,157,565 source bytes. At the existing scale of 3, every page exceeds the
12,000,000-pixel limit (20,610,882 to 46,520,334 pixels); the document total is
422,829,774 pixels against a 100,000,000-pixel limit. Original parent/type/version
remained unchanged. Actual source identifiers and receipt contents are not
included in this repository; private operator diagnostics hold the binding.

The canary stopped without relaxing resource limits or the privacy gate. Gemini,
accounting writes, Medical handoff and source movement were zero. Confirmation
and replay of an actual source remain unverified. Another suitable source or a
separately scoped bounded-rendering phase is required before continuing. The
operator must display the fresh proposal and obtain the human's explicit
partition confirmation before saving authority. Selecting a source is not that
partition confirmation.

Replay must re-observe the original read-only, reuse the same confirmation and
Unit identities, and require no second confirmation. Actual source files must
never be modified for invalidation testing.

## Bounded observation follow-up

The separately authorized [bounded rendering](pdf-bounded-observation.md) phase
reused the same 14-page source read-only. Every page completed rendered local
OCR/privacy observation. Ten pages were normal and four remained
`sensitive_unknown` (privacy unresolved despite complete OCR); none was promoted
by the grouping step. The conservative proposal has fourteen singleton groups,
revision 1, `grouping_required`, and no confirmation. It was displayed in the
existing owned review tab, with the real original link and native dropdowns.

There were 23 render calls, 163,965,563 cumulative render pixels and
1,535,793,378 conservatively reserved OCR pixel-work, all within separate work
budgets. Peak live page count was one; the largest live raster/scratch reservation
was 48,000,000 pixel-equivalents. Observation retained no multi-page PNGs.
The run took 398.4 seconds and Windows reported a peak observer-process working
set of 328,765,440 bytes (this metric does not include separate OCR subprocesses).

The only mutations were one strong-v2-ETag conditional PUT to the dedicated
grouping JSON and two POSTs to the owned review tab (row and dropdown projection).
Both proposal state and projection were read back exactly. The new audit entry
is `observe`, revision 0 to 1, `observed`. Existing synthetic records/audit stayed
unchanged, all confirmations remained absent, and original parent/type/version
were unchanged. No source PDF bytes, OCR text, amounts, names or images were
persisted in state/audit. Actual source IDs and private binding remain outside
this repository.

Before this follow-up, three existing accounting-tab row/filter extents had
already increased relative to the earlier installation snapshot. Read-only
diagnostics found unchanged sentinel formulas/formats/validations and zero
operator writes at that point. A fresh pre-canary snapshot preserved those
existing changes instead of resetting them.
Post-canary read-back matched all 35 non-owned sheet structures, named ranges,
sentinel formulas/formats/validations, hidden technical columns and hidden queue.
The review account's existing source grant and real Drive link were verified;
no ACL or sharing change was made.

All **3360 Python tests**, including every existing Medical test, and **18 Node
tests** passed. Medical code, the existing privacy gate, Gemini/ReceiptPipeline,
Drive mover/archive and all workflow schedules remain unchanged. Gemini,
accounting writes, Medical handoff and source moves were zero for this canary.
This phase stops at proposal display; human partition review is next, and no
confirmed authority or AI-send permission has been granted.

## Verification and next phase

All **3288 Python tests** passed after the synthetic-link correction, including all existing Medical tests, and
**18 Node tests** passed. The added Node regression fixes default capture-only
behavior (missing/false/malformed opt-in, no token requirement, no HTTP, no local
confirmation and no duplicate capture). The focused authority/UI/v2 suite also
passed at introduction (91 cases). The authority suite passed again after the
link correction (40 cases), including rejection of stale captured link values.
Live read-back verified only B2/G2 changed, the fake hyperlink is absent, and
Drive authority/audit are unchanged. Existing two dependency deprecation
warnings remain.

No Medical code, privacy gate, normal Gemini route, ReceiptPipeline, v3 state
transport, source mover, existing recurring workflow or PDF archive setting was
changed. The next confirmed-normal-unit read-only Gemini phase remains gated
on the actual canary and a separately authorized, privacy-checked send/results
path. No AI-send permission is implied by this installation.
