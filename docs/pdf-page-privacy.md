# PDF page privacy foundation (v1)

Base: `ae4c8a4c91e4dd8b591782b72b029b9712925a83`.
Development branch: `codex/pdf-page-privacy`. No merge or production execution.
Grouping-safety follow-up base: `917fed5cb207ecf0187f4689aed1201e0dbaacdc`.
Automatic accounting is now limited to single-page PDFs. A page observation
does not establish an accounting transaction boundary in a multi-page document.
The follow-up changes only `app/receipt_pdf_units.py`, `app/receipt_pipeline.py`,
`app/cli.py`, `tests/test_receipt_pdf_units.py`, and this document.

Changed files:

| File | Change |
| --- | --- |
| `app/receipt_pdf_units.py` | Separate privacy observations from accounting units; grouping-required state and compatible manifests |
| `app/receipt_pipeline.py` | Single normal page routing; multi-page stop before AI, ledger access or Medical |
| `app/receipt_privacy_gate.py` | Unconditional PDF transport rejection |
| `app/gemini_ai.py` | Adapter-level PDF rejection |
| `app/drive_receipts.py` | PDF archival remains disabled |
| `app/cli.py` | Direct PDF rejection and safe grouping-required page summaries |
| `tests/test_receipt_pdf_units.py` | Synthetic PDF security and replay coverage |
| `tests/test_receipt_pipeline.py` | Retained legacy image-gate coverage |
| `tests/test_gemini_ai.py` | PNG/JPEG adapter expectations |
| `docs/pdf-page-privacy.md` | Diagnosis, flow, boundaries, verification and remaining work |

## Phase 0 diagnosis

The original `receipt_text_extraction.py` joined embedded text across PDF pages.
Any nonempty embedded text bypassed scan OCR, including unobserved image pages.
Its OCR fallback rendered at most three pages and rejected larger documents.
`receipt_pipeline.py` gated this document-level text, then passed the original
PDF bytes to `GeminiAI.analyze_receipt`. The adapter gated again and sent a
`document` interaction containing the same PDF. Medical classifications branched
in the pipeline before Gemini, optionally invoking the existing Medical shadow
observer. `drive_receipts.should_archive_result` archived imported/review results.

These sources and related tests were read before implementation. The unchanged
baseline passed all 675 tests selected by `-k medical`. Medical tests require
temporary paths outside the repository; the verified run used
`--basetemp ../.test-baseline-medical`.

## Processing

1. Identify PDF MIME or a PDF header, before legacy document-level dedupe.
2. Hash the source bytes. Enumerate every page locally with pypdf and PDFium,
   verifying matching page counts. Encrypted/corrupt/empty PDFs go to review.
3. Check resource limits before sending anything: 50 pages, 50 MiB source,
   12 million pixels per page, 100 million document pixels at 3x render scale,
   and 64 MiB of generated PNG payloads. A document exceeding any limit is
   rejected as a whole, with no partially submitted pages.
4. Render every page. Reconstruct fresh RGB images from the rendered samples
   and encode PNGs without original PDF objects, metadata, or attachments.
   Observe each PNG locally for OCR completeness and through the existing
   privacy gate. The separate completeness observation keeps Medical payment
   diagnostics separate from document-kind completeness. Sensitive text/token
   evidence from that first observation restricts every subsequent reading;
   a later normal reading cannot erase it. Read embedded text
   for that page as a restriction: it never authorizes a page by itself.
5. Finish all page observations before any Gemini resolution or ledger access.
   OCR/render/extraction failures are `sensitive_unknown`. Successful sibling
   observations remain recorded, but no multi-page document can auto-post.
   Medical/payroll/unknown page payloads are discarded locally.
6. Store every page's privacy metadata under `pages`. For a multi-page PDF,
   store no accounting units (`units: []`), return `grouping_required` and stop.
   This is `observed -> grouping_required`, regardless of whether all pages are
   normal, mixed, or partially unreadable. No Gemini initialization/submission,
   ledger read/write, Medical observer call or Drive move follows this state.
   Replay performs local observation/manifest persistence only, even with legacy
   imported markers. Old imported markers cannot authorize new grouping.
7. Only a single-page PDF automatically gets one unit. `PageGrouping` remains
   the interface for future candidates, without providing accounting authority.
   Unit identity hashes source file ID, source content
   hash, inclusive page range and a content-derived unit hash anchored to the
   immutable source hash and page range. The separate page hash audits the
   actual PNG; raster encoding changes do not change ledger identity. Failed
   rendering has a deterministic unavailable-page hash derived from source
   content and page number. All metadata retains source ID and page number.
8. Save a local manifest before processing a single normal unit. Pass its PNG
   into the existing receipt materialization code, using unit IDs for its
   receipt/import/expense identities. Recheck the exact PNG in the pipeline
   and again at the adapter boundary. A later gate failure holds that unit.
9. Single-page Medical units remain `medical_pending`. Other blocked units remain
   `privacy_pending`. Neither the existing Medical observer nor its review
   store is invoked for PDF units, even if the later image gate becomes medical.
10. Save the single unit's outcome atomically. Its replay relies on existing ledger IDs, including
   receipt/expense IDs after interrupted writes. A replayed review import marker
   remains `needs_review`; it cannot establish terminal success. Prior local
   sensitive provenance can restrict a replay but never authorize it. Corrupt
   manifests stop processing before external AI or ledger writes.
11. A single-page document is `completed` only when its unit is terminal;
    otherwise it is `needs_review`. Multi-page `grouping_required` cannot be
    promoted by page-level imported/confirmed markers. Terminal
    states are `imported`, `confirmed`, and `intentionally_skipped`. V1 does not
    provide a new approval/skip UI or automatically resolve pending units.

## Persistence and boundaries

Default local manifests: `.private/pdf-document-units/<source-identity-hash>.json`.
Tests inject a temporary `PdfUnitManifestStore`. These files hold source/page
identity, classification, extraction status and (single-page only) unit status, with no extracted
text, image bytes, merchant, amount, or Medical identity/payment evidence. They
are separate from all Medical stores and ignored by the repository's existing
`.private/` rule. Preserve the manifest directory between runs if retaining
previous sensitive provenance is required. Ledger markers remain dedupe authority.
The returned report also carries the source and page identities for a caller.
Legacy PR #91 manifests without `pages` remain readable for restrictive privacy
provenance. Both original page observations and later unit gate restrictions are
retained conservatively; neither creates grouping/accounting permission.

`archive_allowed` is always false, even for completed PDFs, and the Drive
archival predicate independently rejects every `pdf_page_units` report.
Drive CLI output omits PDF filenames and source/unit IDs. No new production
Drive move is enabled. Existing image handling is retained.
The user explicitly retained the archive ban for single-page normal PDFs in
this follow-up as well; activation is a separate phase.

Whole PDFs are unconditionally blocked at the pipeline's analysis boundary,
`require_receipt_ai_permission`, and the Gemini adapter, including MIME spoofing.
The direct `analyze` CLI and fixed-source `reanalyze_bytes` reject PDFs with
`pdf_requires_page_units`; the ingestion `receipt` path uses the page layer.
They require a future page-aware read-only analysis interface to support PDFs.

No Medical implementation, HMAC identity, amount resolution, admission policy,
review persistence, or Medical production integration changes are included.
`receipt_text_extraction.py` and its legacy three-page path remain unchanged;
the new page path operates on single rendered PNGs and handles more than three
scan pages. No workflow/scheduler configuration is changed.

## Verification

`tests/test_receipt_pdf_units.py` exercises real synthetic PDF parsing and
rendering, the existing gates with local OCR adapters mocked, and mocked ledger
and Gemini transport. Coverage includes normal/normal, normal/medical in both
orders, normal/unknown/payroll, embedded-text plus scan, five scan pages, OCR and
render failures, incomplete tokens, replay across pipeline lifetimes, interrupted
ledger writes, review marker replay, identity changes, sensitive provenance,
resource rejection, encryption, corrupt inputs/manifests, direct transport/CLI
blocks and Drive no-move behavior. The received bytes are decoded as PNGs and
checked for page content and absence of PDF metadata/attachments. A real Gemini
adapter with a mocked transport also verifies the final encoded payload.
Single-page normal documents exercise that transport; multi-page documents
assert zero AI/ledger/Medical calls, including replay and a two-page continued
receipt with the same receipt reference. Tests also retain legacy manifest
restrictions, prevent terminal page markers from establishing grouping authority,
and verify Drive no-move and safe CLI page summaries.

Existing adapter tests now cover PNG/JPEG; the old whole-PDF-transport expectation
is replaced by unconditional PDF rejection tests. The legacy non-normal pipeline
test targets the retained image route; new synthetic PDFs exercise page routing.

Historical verification on the initial PR #91 implementation (917fed5):

- Full suite: **3131 passed**, with two existing dependency deprecation warnings.
- PDF safety suite: **43 passed**.
- Medical selection: **675 passed before**, **681 passed after** (six new tests
  selected by the Medical keyword). The final full suite includes all of these.
- `git diff --check`: passed.
- Medical implementation/tests, legacy text extraction and `.github` diff
  against the specified base: empty.
- All accounting/Drive/Gemini effects in PDF tests use fakes or mocks; no live
  Sheets write, Drive move, Medical data generation or external AI submission.

Verified grouping-safety follow-up results:

- Full suite: **3152 passed**, with two existing dependency deprecation warnings.
- PDF safety/grouping suite: **64 tests**, all included in the successful suite.
- Receipt/PDF/Drive/CLI/Gemini focused run: **122 passed**.
- Medical selection: **681 passed**; Medical implementation and dedicated tests
  have no changes from either 917fed5 or the original main base.
- `git diff --check`: passed. Privacy gate, Gemini adapter, Drive archive
  predicate, legacy OCR and `.github` are unchanged in this follow-up.
- Single-page normal PDFs retain safe PNG accounting. Multi-page PDFs retain
  local observations only, no accounting units, and zero external side effects.
- Single-page and multi-page PDF archival remain disabled by user instruction.

Commands:

```text
python -m pytest -q --basetemp ../.test-grouping-full
python -m pytest -q -k medical --basetemp ../.test-grouping-medical
git diff --check
git diff ae4c8a4c91e4dd8b591782b72b029b9712925a83 -- app/medical* tests/test_medical* .github app/receipt_text_extraction.py
```

## Remaining work

- Multi-page receipt grouping is intentionally deferred. All multi-page PDFs
  remain `grouping_required`; their pages cannot become separate transactions.
- Pending Medical units require a later authorized integration; none is created
  here. Confirmation/intentional skip and a page-aware review UI are future work.
- PDF archive activation needs a separate reviewed change, even after all units
  are resolved. Production deployment and scheduler changes are outside this work.
- The current ledger replay model serializes intake operationally; simultaneous
  independent writers are not an atomic cross-process reservation mechanism.
  Renderer changes should still be reviewed before upgrading the ingestion
  runtime, although PNG compression changes cannot bypass replay dedupe.

## Next-phase grouping design (not implemented)

Keep `PageGrouping` as the candidate-generation boundary. Extend the candidate
model to represent ordered page membership/ranges and bind each proposal to
source ID, source content hash, page hashes and a grouping-manifest hash. Store
candidate evidence and uncertainty separately from confirmed `DocumentUnit`s.
Generating a candidate must grant no Gemini, ledger, Medical or archive authority.

A separate human confirmation step must validate the exact source version and
grouping manifest, page order, complete coverage, and absence of duplicate/overlap
assignments. Unassigned/unreadable pages keep the source unresolved. A changed
source or proposal invalidates the old confirmation; replay uses stable confirmed
unit identities and existing ledger markers.

Only confirmed units may enter the later accounting branch. Every constituent
page must satisfy privacy requirements; mixed/sensitive groups cannot enter
general receipt AI. Payloads must still be rebuilt from safe rendered pixels.
Medical branching requires separately authorized work; this patch provides no
Medical connection. Archive activation is also a separate authorized phase.
