# Source / Page / Receipt model (read-only introduction)

This phase is isolated from production intake, accounting and source movement.
The existing classifier, Medical manual writer and grouping confirmations are
unchanged. No schedule or Secret is added. PR 91 remains Draft.

`SourceRef` fixes file ID, original-byte SHA256, page count and PDF/image kind.
`PageUnit` retains automatic classification/reason, independent human kind,
stable page identity, diagnostic render hash, completeness and review revision.
PDF page identity reuses `pdf-page-v1`; image sources have one stable image page.
Pages are never joined in this path. Suspected cross-page continuations are held.

`PageReceiptExtraction.receipts` contains 1..20 independently located receipts,
including a one-element array for an ordinary image. Two bounded independent
readings are required. Sort spatial row bands (top to bottom, then left to right),
not response order. Detect changed counts/positions, overlap, shared or reassigned
item regions, duplicate results, incomplete separation and mixed page kinds.
Any such problem holds the whole page as `receipt_segmentation_review`.

The API generation grammar inlines local references and omits array repetition
limits/default/title annotations to keep the nested grammar small. The strict
local model still enforces 1..20 receipts, at most 300 items/boxes, finite valid
coordinates and all semantic checks. API 400s never cause automatic transport,
schema or permission fallback. Safe diagnostics contain fixed reason labels,
HTTP status and redacted schema paths only, never the API message/body.

After corroboration, freeze the initial spatial manifest. Receipt ID binds source
ID/hash, stable page identity, manifest digest and spatial index. The optional
`ReceiptManifestStore` uses the existing private Drive If-Match transport and ACL
preflight. It stores only identity, position and not-written metadata; no OCR,
merchant, date, amounts, image or accounting permission. No automatic provisioning
or local JSON authority fallback. A changed partition is rejected, never silently
assigned new identities. The existing accounting journal must independently verify
future posted outcomes; this manifest is not evidence that anything was written.

Each receipt uses the existing `apply_receipt_policy`: valid date, positive total,
master categories, actual items summing to total, no invented adjustments,
transaction-kind and reread checks. Merchant/payment are optional. Payment evidence
is obtained by sequential **local-only** OCR of the matched receipt region, never
by mixing text across receipts or sending crop images to AI.

## Explicit Human General Authority

`human-general-page-authority-v1` is separate from legacy page-kind/grouping state.
Its scope is `human_general_receipt / single_page_ai`; accounting, Medical handoff
and archive are all false. Bindings include source ID/hash/kind/count, page number,
stable page identity, automatic result/reason, current review identity and revision.
Confirmation includes an aware timestamp and independently verified Google actor.
Replay preserves timestamp/digest/revision. Strong ETag + If-Match, exact read-back,
strict schema/audit linkage and no unconditional retry are mandatory.

Only complete unknown observations with explicit insufficient-evidence reason
codes are eligible. Medical, payroll, clear personal data, known sensitive source,
conflicting sensitive evidence, extraction failure and incomplete observations
remain blocked. Aggregated legacy `privacy_unresolved` lacks sufficiently specific
provenance and is held. Do not infer a new consent scope from an old human-normal
answer that explicitly recorded AI disabled.

The unchanged normal-only gate still controls auto-normal submissions. The new
adapter independently rechecks unknown authority, source freshness, exact local
OCR and sensitive evidence. It does not manufacture a normal classification.
Only fresh metadata-free single-frame RGB PNG for one page enters the SDK; its
SHA256 must match the exact gated bytes immediately before each of the two calls.

## Existing review UI

`general_review_card` is a vertical card in `PDFページ確認`, using the existing
hidden `_PDF確認受付`. No new tab, web UI or receipt-count input. A clear notice
states that choosing 一般レシート grants **this page's external AI analysis only**.
Medical/payroll/unknown choices never grant general AI permission. Existing
Medical/general manual cards and their owner inputs must be preserved.

Apps Script captures intent only, and deliberately does not dispatch this new
kind to the old page-kind worker. `process_general_request` reloads trusted page
identity, verifies the current owner snapshot and delegates to the new authority
service. A separately authenticated `verified_actor(request_id)` adapter is
required. Neither hidden cells nor the trigger/service-account owner's email can
substitute for it. Missing verified actor is a deployment blocker, not a fallback.
A projection failure after authority read-back can replay without another grant.

## Actions canaries

The existing manual-only `pdf-unit-readonly.yml` supports separate `page_p2`,
`page_replay` and `page_remaining` modes at the reviewed Draft PR head after all
three CI checks pass. Remaining mode requires the exact matching successful
page_p2 encrypted proof and runs one bounded batch: p3-p6, p7-p10 or p11-p14.
Existing p2/remaining modes retain their schema/behavior. New diagnostics use
`page-receipt-readonly-diagnostic-v1`, which legacy accounting canaries reject.
Existing Secrets stay in hosted runner memory; encrypted artifacts retain one day.
No PNG, OCR text, raw Gemini response, Secret or Medical data is persisted.

All Google clients in this runner have a GET-only transport fence. The canary has
no Human General live binding yet: legacy unknown pages are held, not sent. Medical
p1 is excluded before document open/render/AI. Diagnostic statuses are never
posting authority. A partial receipt/page/PDF cannot become terminal or movable.
`source_terminal` requires exact complete page enumeration, unique child IDs,
current authority, terminal outcomes and verified accounting read-back for every
child. No mover or production integration is enabled in this phase.

## Remaining deployment gates

Before live unknown-page AI: provision a separate private state under existing ACL,
establish a verifiable authenticated confirmer event, deploy the reviewed capture
handler in the existing Apps Script project, and obtain new explicit page-scoped
AI consent after a current local review. The old p4/p10/p14 kind confirmations
cannot authorize this migration. Before any posting: existing writer/journal
integration, duplicate reconciliation, one-page write/read-back/replay canary and
complete PDF terminal/move checks require a separate phase and authorization.
