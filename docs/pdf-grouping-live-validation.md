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

Each authority mutation uses the dedicated v2 conditional transport and exact
read-back. Audit records operation/time/source identity, before/after revision,
proposal/confirmation digest, result and captured-request identities only.
No reviewer actor is guessed. The mutation fence permits only conditional PUT
to the dedicated state and POST to the two owned review tabs; source reads use
existing read-only Drive credentials. ReceiptPipeline, AI clients, Medical
observer/store/handoff and move writers are never invoked by this path.

## Actual PDF canary

Pending selection of one known non-Medical multipage PDF from Receipt Inbox.
Only metadata has been listed. No actual PDF has been downloaded, observed,
displayed or confirmed during this validation yet. The operator must display
the fresh proposal and obtain the human's explicit partition confirmation
before saving authority. General authorization to run the canary is not that
partition confirmation.

Replay must re-observe the original read-only, reuse the same confirmation and
Unit identities, and require no second confirmation. Actual source files must
never be modified for invalidation testing.

## Verification and next phase

All **3287 Python tests** passed, including all existing Medical tests, and
**18 Node tests** passed. The added Node regression fixes default capture-only
behavior (missing/false/malformed opt-in, no token requirement, no HTTP, no local
confirmation and no duplicate capture). The focused authority/UI/v2 suite also
passed (91 cases). Existing two dependency deprecation warnings remain.

No Medical code, privacy gate, normal Gemini route, ReceiptPipeline, v3 state
transport, source mover, existing recurring workflow or PDF archive setting was
changed. The next confirmed-normal-unit read-only Gemini phase remains gated
on the actual canary and a separately authorized, privacy-checked send/results
path. No AI-send permission is implied by this installation.
