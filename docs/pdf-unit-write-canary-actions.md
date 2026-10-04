# Fixed PDF accounting canary

Manual-only registration is separate from Draft PR 91 implementation. No schedule
or automatic activation. No Gemini Secret reference. The registered main workflow
requires current validated-main and the existing production-enabled flag, and owns
the exact same `kakeibo-production` mutex as ordinary production/read-only runs.
The implementation is checked out from an exact reviewed Draft PR 91 head after CI.

Reuse of an earlier read-only analysis requires both successful encrypted Actions
proofs, exact current source/authority, and an ancestor comparison allowing only
canary entrypoint/tests/workflow/docs changes. Any parsing/validation/privacy/model
diff rejects the old proof. This never enables stale analysis after policy changes.

Preflight is GET-only. p11 is first. Small stage is fixed to p5/p6/p8; remaining to
p2/p3/p7/p9/p12/p13. Every write invocation starts with a fresh exact p11 replay, and
every new Unit is replayed before the next. Privacy-held p4/p10/p14 and Medical p1
are never admitted. The owner authorized p3 original comparison. Its same-amount
candidate is admitted only after local original date/merchant evidence, different
dates/merchants/items and receipt/import/source linkage establish distinct
transactions. The private intent retains comparison fingerprints. Current rows
and original bytes are rechecked before each append and on replay; any additional
duplicate candidate remains held. No threshold or privacy rule is changed.

The existing ReceiptPipeline row materializer and SheetsDB RAW append are reused.
The writer keeps its normal ProjectionJournal invalidation before expense append.
Only that precreated journal file may be updated; projection refresh, dashboards,
existing other state, UI helpers, source move and archive APIs are unavailable.
The production mutex prevents a concurrent derived refresh from clearing the dirty
append flag during the canary. The usual later production run can refresh views.

Detailed results are encrypted to the existing SA key with one-day retention.
Logs show counts/status only. No image/OCR/raw Gemini/Medical/Secret artifact.
Grouping and page-kind authority files are load-only and their bytes/ETags unchanged.
