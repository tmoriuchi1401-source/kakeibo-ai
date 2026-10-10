# Confirmed PDF receipt read-only evaluation

`app/pdf_receipt_readonly.py` is an explicit operator evaluation interface, not
a scheduled ingestion path. It has load-only authority/source dependencies and
a receipt analyzer callback. It does not import a receipt pipeline, accounting
writer, Medical processor or archive client, and provides no authority-save API.
No workflow, schedule, Apps Script or spreadsheet is installed by this module.

The caller must obtain a validated `pdf-grouping-authority-v1` from the existing
private Drive store and pin source identity, page count, revision, proposal and
confirmation digests and Unit IDs before processing. Local files and Sheet cells
do not establish authority. Only confirmed singleton groups in the general
scope with current human normal answers are eligible. The automatic Unit
classification must also be normal: human normal does not erase an earlier
sensitive_unknown, medical or payroll observation. Restricted Units are reported
as privacy_blocked without rendering or transmission.

For each eligible Unit, the runner verifies fresh whole-source bytes, opens only
the selected page, uses its bounded observation scale and compares the new RGB
PNG hash with the confirmed member page hash. PNG metadata and multiframe data
are rejected. Local OCR completeness and sensitive evidence precede the exact
payload privacy gate. Source bytes and durable authority are rechecked before
the analyzer. The production Gemini adapter must retain its own mandatory
exact-payload gate; use the existing `GeminiAI.analyze_receipt` with bounded API
attempts and its existing maximum three correction readings.

One Unit's image lives only inside its WorkBudget page scope. The operator calls
`run` sequentially, saves only private diagnostic data, and releases responses
between Units. No original PDF, neighbouring page, source metadata or attachments
are passed to Gemini. The read-only caller must fence every Drive/Sheets request
to GET, pin the Gemini destination and verify submitted image bytes against the
current single-page hash. No SDK error bodies, raw OCR or image bytes belong in
diagnostics. Do not print keys or credential configuration.

Validation reuses `validate_receipt_result`, checks purchase/buyback evidence,
and retains item/total mismatches. A changed date/merchant/total across bounded
readings, an unverified balancing item, or a payment method absent from local
visible text requires review. These diagnostic checks are conservative; a
local OCR match is not proof that every field is correct. `would_import` means
the current receipt checks pass; it grants no accounting permission. An API or
schema failure is analysis_failed, and an authority mismatch is authority_held.
Privacy failures are not retried in search of a normal result.

All grouping/page-kind flags stay unchanged. Medical manual inputs, source
location and existing UI are read only. A later accounting canary needs a
separate explicit human approval and a fresh authority/privacy/source check.
