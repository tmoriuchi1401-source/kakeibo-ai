# PDF production rollout preparation

This branch starts at validated main. Draft PR 91 is not merged or copied as a
whole. Its bounded observation, grouping/schema and stable identity modules are
selected dependencies of the Unit adapter. No workflow, schedule, Secret,
existing Drive binding, bank, PayPay, Amazon, Payroll, Medical AUTO or existing
spreadsheet UI is changed here. The Medical detector remains unchanged.

The shared ordinary receipt policy requires a valid payment date, positive
original total and existing categories. Existing item/total consistency,
transaction-kind, adjustment and privacy checks remain. Merchant/payment are
optional; a payment unsupported by exact local original text becomes blank.
The existing manual-entry writer likewise preserves an explicitly typed optional
payment without inventing cash. It verifies the complete appended row and stops
on conflicting content. A source-bound caller can inject a pre-append freshness
barrier. This callback does not itself create PDF authority.

PDF-bound manual requests now require a verifier; the ordinary manual worker
rejects their marker and cannot cancel/void their accounting rows. The explicit
PDF service reuses that writer for one source-bound expense append, without a
new ledger implementation. It requires existing category membership, explicit
owner request verification and fresh source/authority both before claiming and
before append. OCR/candidates never supply input. Its writer fence permits only
the exact new expense row and the existing hidden manual queue projection.

`pdf-unit-processing-v1` is a separate private completion journal, using the
established Drive v2 If-Match transport. It does not grant grouping, AI, Medical
or archive permission. ACL checks, strong ETag, strict JSON/schema and exact
read-back are required; no missing-state initialization/local fallback exists.
The journal saves pending intent before a write and applied only after independent
accounting read-back. An ambiguous pending write is reconciliation-only, never
an automatic resend. An overlapping new grouping revision is held. Projection
failure replays the view without applying intent or accounting again.

The journal also records sticky privacy holds, separately from accounting
completion. Holds contain source/page/Unit identity and fixed reason codes, not
OCR or amounts. Grouping edits and new service instances cannot erase them;
normal automatic processing refuses any held member. Unknown pages may use
explicit owner-complete general input with durable human page-kind evidence.
Medical/payroll evidence cannot enter that general manual route.
Late analyzer/planning/writer privacy failures keep their restrictive class as
well. The common permission exception carries only that class, never Medical
amounts/text; its allow conditions and detector thresholds are unchanged.

`DrivePdfAuthority` re-reads Drive v1/v2 authority and current original PDF bytes,
including page count/ordinal identity. Its v2 projection preserves existing Unit
IDs and does not compare fresh PNG compression to historical render hashes.
The single-source v2 migration must still match that source's complete legacy
intent; an unrelated PDF never inherits its migration or permission.

`PdfUnitIntake` is an opt-in service injected at the existing ReceiptPipeline PDF
boundary. It completes all local page observations before selecting confirmed
normal groups, discards observation PNGs, then freshly renders one bounded Unit
at a time. Multi-member groups use one bounded RGB canvas and one member scratch;
original PDF objects/attachments cannot reach Gemini. A shared work budget spans
observation, fresh rendering and downstream checks; normal gate/parser/planning/
writer checks are charged conservatively without raising pixel/work caps.
Every SDK call verifies exact PNG bytes, normal authority, current original
source and the fixed Google destination. Existing receipt rereads are bounded
at three, and changed date/total/kind readings remain review-only.

Live accounting scope is confirmed single-page purchase Units, matching the
existing live canary evidence. Confirmed composite Units remain pending; their
bounded payload interface supports a separate read-only/write canary. Buyback/
unknown results stay on review; the existing non-PDF income writer is unchanged.

The existing ReceiptPipeline writer first materializes a frozen plan in memory.
The private journal persists pending intent before any literal append to only
the existing three receipt tables. A fence prohibits unplanned rows or other
writer methods; every append and completion requires exact accounting read-back.
A pending unknown/partial append is reconciliation-only, never automatically
resumed. Total/date duplicate candidates are held for a full source comparison,
never declared duplicates by amount alone. Legacy parent import IDs or Unit IDs
without durable intent cannot be reposted. All PDF archive remains disabled.

`adopt_canary_intents` can migrate already posted records from the existing
Drive canary state after exact authority/source/Unit and full accounting-row
checks. Only the new completion journal changes; Unit IDs, old ledger rows,
old state and grouping confirmation remain intact. It accepts no local
diagnostic/artifact restoration and performs no new accounting or Gemini call.
The operator's isolated live journal has now adopted ten previously posted
canary intents after fresh Drive/source/authority and exact accounting-row
checks. Replay through GET-only clients preserved its generation, bytes, ETag,
intent IDs and timestamps. Existing grouping state and accounting rows did not
change. This is migration evidence, not scheduled intake activation.

Medical completion contains only identity and an existing backend reference,
never Medical input fields or a Medical accounting plan. A page-kind completion
reference can cover a Medical page outside the general grouping partition;
it does not change its existing Medical source/review identity. General grouping
Unit IDs use the exact existing v2 digest and stay unchanged.

The operator has provisioned a separate completion journal under the existing
owner/service-account ACL. Strong conditional update and exact read-back passed;
the stale ETag returned HTTP 412. This branch does not provision state on import
and its manual service is not wired into a live workflow. It needs the production mutex,
validated main boundary, projection journal, Drive authority provider and current
owner request verifier before activation. No live state/authority or UI is
created by importing these modules.

The normal receipt factory and source scan seams are now implemented, with an
independent `PDF_UNIT_PROCESSING_ENABLED=true` opt-in. The current workflow does
not pass this switch or the grouping binding, and no live Variable is changed.
Enabling requires the existing hosted production workflow, exact validated main,
normal logging and a valid projection journal before AI or accounting. Existing
RSA-wrapped grouping IDs are decoded with their original labels; no existing
binding or Secret value changes. The completion file is discovered by its exact
schema/legacy-binding filename in the same private folder; missing/duplicate or
changed ACL stops. Only its v2 conditional media upload is permitted by the new
HTTP fence; grouping authority and source movement are read-only capabilities.

The preflight scanner branches PDFs before any whole-file Medical/owner route.
It finishes local observation and stores only source/ordinal/classification
metadata plus an authority snapshot digest in the ephemeral source plan. The
ordinary worker rechecks current Drive authority/source and cannot use that
snapshot to grant permission; newly sensitive/incomplete observations remain
restrictive across the two processes. Preview never resolves AI, claims completion
or repairs pending writes. Applied replay verifies accounting rows without
reanalysis, state writes or derived-view refresh. Count-only progress carries no
filename, Unit IDs, amount or OCR. An unknown delivery stops subsequent sources.
The existing one-page bounded PNG route remains separate; multi-page PDFs without
the opt-in/durable authority stay on hold. Both ordinary intake and legacy
confirmation archive paths refuse PDF moves pending the separate all-Unit canary.

The parent-completion component is read-only. It requires complete, disjoint page
coverage, stable source/ordinal page identities, durable completion intent and
independent accounting read-back. A Medical page can only be completed by the
Medical manual path. Sensitive automatic provenance cannot count as an automatic
normal import. One pending/unknown Unit always keeps the parent incomplete.
Even all-terminal returns only an archive candidate, with archive permission
false. Actual source movement requires its separate first-PDF canary.

`ManualTerminalReadback` verifies already-applied general and Medical manual
references independently of UI labels. It requires current Drive authority,
the immutable input/intent reference and exact accounting rows. Medical remains
in its separate existing durable backend, with the original page/review identity,
explicit complete manual confirmation and bound accounting plan. This read-only
component cannot create reviews, capture inputs, invoke a writer or move a PDF.

## Remaining before merge/activation

* Finish owner input and durable read-back/replay for the remaining manual pages.
* Review and canary the opt-in receipt factory/source-plan seams with real Drive
  state, under the existing main/validated SHA, projection and mutex boundaries.
  Add only their encrypted binding/switch passthrough to the existing workflow
  after completing remaining owner inputs; no new schedule or Secret.
* Retain the already verified private completion journal and adopted intents;
  do not recreate or reapply them when activating the service.
* Reconcile manual/Medical terminal references through their existing writers;
  the normal adapter does not generate Medical reviews or amounts.
* Fresh exact PNG privacy remains mandatory for every externally analyzed Unit.
* Keep Medical/privacy/unconfirmed Units on hold and retain partial PDFs.
* Require full CI, a single production PDF canary and verified validated-main
  before connecting the existing receipt entry point. Add no new schedule.

This preparatory change does not activate scheduled PDF processing or archive.

## Shared review to the existing general manual writer

`GeneralManualReview` resolves a singleton from the real Drive authority and
completion store; it does not trust technical cells as authority. It accepts
only automatic unknown with durable human normal and confirmed singleton
grouping. Strong Medical/payroll holds reject the route. Legacy fingerprints
link the existing review intent, while current original SHA/count/ordinal Unit
identity supplies freshness. No page images, OCR or AI are used by this adapter.

The owner request remains in the existing `_PDF確認受付`. A one-request adapter
presents its verified snapshot to `manual_entry.execute`, reusing its parser,
duplicate checks and exact expense append/read-back without another physical
queue or ledger writer. The Drive Unit journal saves intent before append and
completion only after read-back and fresh source/owner checks. A second UUID
with identical typed values reconciles the original intent without new writes;
changed values are refused. Ambiguous pending delivery is never resent.

This adapter requires a protected hosted dispatcher and an independent owner
snapshot callback from the common review controller. It is not a standalone
cell-to-writer command. No workflow, UI, Medical backend or schedule is enabled
by its presence. The remaining four pages still require actual owner input and
explicit confirmation, including actual iPhone operation verification.
