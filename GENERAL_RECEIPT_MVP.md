# General receipt read-only MVP

## Boundary and reused assets

This feature is intentionally one receipt to one transaction preview. It is not
a generic document framework and does not extract product lines.

Reused unchanged:

- `receipt_text_extraction`: bounded in-memory image/PDF materialization,
  embedded PDF text, rendered PDF OCR fallback, and private OCR geometry.
- `medical_receipt_privacy`: local medical/payroll/sensitive classification.
- `medical_rapidocr_shadow`: optional pinned, offline, source-bound RapidOCR
  adapter through its engine-neutral `OcrObservation` envelope.
- `reconciliation`: the existing 12-column import transaction parser, merchant
  normalization, and payment/Amazon duplicate resolution.
- the existing `取込データ` row contract from `sheets.py`.

Not reused as policy:

- Medical payment labels, Medical production authority, Medical issuer/facility
  markers, and Medical promotion rules. They remain Medical-only.
- External Gemini receipt analysis. This preview never calls external AI.

The general receipt parser borrows only the safe design pattern: every candidate
is bound to the source SHA-256; unique evidence can produce a preview; missing or
competing evidence becomes `needs_review`.

## Output and safety

The minimum fields are purchase date, merchant, and total. Strong total labels
are deliberately small: `合計`, `合計金額`, `お買上金額`, `お支払金額`, `現計`,
and `総合計`. `小計`, tax-only, deposit, change, point, coupon, discount, and
balance contexts are excluded before ranking. Different surviving totals are
ambiguous and withhold the total.

`transaction_row` follows `取込データ!A:L`. `write_plan_rows` is `1` only for a
complete, nonduplicate preview, but `write_authorized` is always false. There is
no writer in this module. With a read-only Sheets connection, exact source and
semantic receipt duplicates are checked and the hypothetical transaction is
passed through existing reconciliation.

Run without Sheets or external services:

```powershell
python -m app.cli general-receipt-preview path\to\receipt.jpg
```

Add read-only duplicate/reconciliation diagnostics:

```powershell
python -m app.cli general-receipt-preview path\to\receipt.pdf --with-sheets
```

The default OCR route uses the existing local Tesseract image/PDF path. A caller
with the already provisioned, pinned RapidOCR manifest may inject
`RapidOcrShadowAdapter` into `GeneralReceiptPreviewPipeline`; the observation
must be complete and match the exact source digest or parsing stops.

## Phase checkpoint

Phase 1 decision: **A — ship the read-only preview behind human review; keep
production writes disabled**.

- Synthetic, human-readable layouts: 8/8 minimum-field success.
- Local runtime materialization check: 2/6 ready with exact ground truth and 4/6
  safely reviewed across PNG and image-only PDF. The ready pair used a clear
  Latin merchant header; the review pairs exposed a Japanese merchant OCR
  confidence failure and a total-label OCR miss. No wrong total was accepted.
- Review cases: conflicting total, conflicting date, possible semantic duplicate.
- Safety cases: non-total money excluded; Medical input privacy-blocked; arbitrary
  non-receipt rejected; incomplete RapidOCR observation rejected; low-confidence
  selected merchant rejected from the write plan.
- Private real-receipt evaluation: 8 ordinary receipts plus 2 medical controls,
  all visually checked before comparison. The media and raw OCR stayed in the
  ignored local evaluation directory and were not added to Git.
- Ordinary receipts: 2/8 produced an exact minimum-field preview; 6/8 stopped at
  review/not-receipt/privacy-blocked because OCR could not safely establish every
  required field. No incorrect preview was placed in the write plan.
- Medical controls: 2/2 were blocked before general parsing (one `medical`, one
  fail-closed `sensitive_unknown`). No external AI was used in any evaluation.
- Observed tuning stayed bounded: transaction-copy evidence, invalid/whitespace
  thousands separators, tax/deposit total exclusions, explicit corporate
  merchant lines, trailing OCR punctuation, and all-conflicting-date review.
- Next-phase cost/benefit: local OCR quality is now the limiting factor. Improving
  rotation/preprocessing or the already-bound RapidOCR route is higher value than
  broadening amount or merchant guesses. Product-line extraction and production
  writes remain out of scope.
