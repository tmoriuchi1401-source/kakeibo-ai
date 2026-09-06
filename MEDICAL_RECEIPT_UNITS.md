# Receipt-unit shadow boundary

The input contract is one receipt per image or PDF page. This experiment does not
detect or split multiple receipts within one page. PNG/PDF correspondence and
reference amounts are not inputs to candidate generation or unit selection.

## Existing document-level behavior (unchanged)

- `receipt_text_extraction._extract_pdf_embedded_text` joins page text. A nonempty
  document takes the text path without per-page OCR coverage.
- `_extract_pdf_ocr_text` renders pages individually and preserves token page IDs,
  but joins text and returns one document observation. Empty page text may be
  omitted; structured channel completeness is document-wide.
- `receipt_privacy_gate.evaluate_receipt_privacy` classifies that document and
  invokes one preview/resolution, not one result per receipt page.
- Structured labels/geometry group by page, but `collect_payment_evidence` has
  page-less text regions and document-wide text/structured correspondence.
- `resolve_payment_evidence` pools all relevant regions. Separate receipt amounts
  can conflict; equal amounts on distinct receipts can resolve as one amount.
- `ReceiptPipeline.process_bytes` returns one document result/source identity.
- The old `medical_layout_local._pdf_input` similarly concatenates observations
  before `evaluate_medical_layout`; its geometry is page-aware, its baseline
  payment resolution is document-wide. Numeric/region multi-pass modules isolate
  source/page groups but do not change that older bytes-entrypoint contract.

These are diagnostic findings, not authorization to change the production resolver
or privacy boundary. Existing medical/sensitive external-AI denial stays intact.

## New opt-in shadow path

`evaluate_receipt_units` validates immutable bytes and MIME/size constraints. PDF
validation checks encryption/page count without extracting embedded text. Each
bounded render immediately uses the same `_evaluate_image` as a single PNG/JPEG.
OCR, classification, numeric observations, payment evidence and existing resolution
are computed only from that image. Internal coordinates use local page 1; the
original source page ordinal stays separately on the returned unit. Mixed-page
tokens are rejected before collection. No tokens/text/candidates are concatenated
across units, and no amount is returned or summed by the container.

Every source page has an explicit result. A rendering/OCR failure stays failed;
successful siblings cannot repair it. The container reports incomplete observation
coverage while retaining successful unit diagnostics. Complete means all units
were observed, not that their classifications or payment amounts are proven.
Classification is local observed classification; it is never an AI permission.
All shadow outputs explicitly deny external AI and have no production callers.

Results contain enum-like statuses and counters, no raw text, tokens, filenames,
source URLs, image bytes, reference amounts or payment values. A resolver-confirmed
counter is only a shadow diagnostic, never a production confirmation. Low/malformed
numeric observations and possible/negative/competing payment regions are retained.
The pure observation helper requires exactly one image observation and must not be
used as an arbitrary document-text entrypoint.

Local evaluation reuses the fixed PSM 6/3/11 at scales 0.50/0.75/1.00. Both text
and structured OCR use the specified pass settings. No setting is chosen using a
reference value. Proposals and diagnostics are built first; reference comparisons
are downstream and anonymous. Passes stay correlated and are not confirmation votes.

Production integration remains a separate design decision: receipt-unit identities,
source privacy provenance, partial-failure handling, per-page results and downstream
persistence must be designed before replacing document-level processing.
