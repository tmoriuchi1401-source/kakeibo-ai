# Receipt inbox routing contract

`receipt_inbox` is the single input folder. Callers do not need to classify files
in advance.

## Production authority

The production call chain is:

1. `app/drive_receipts.py:process_inbox` lists supported image/PDF files and
   downloads each file into memory.
2. `app/receipt_pipeline.py:ReceiptPipeline.process_bytes` calls
   `app/receipt_privacy_gate.py:evaluate_receipt_privacy`.
3. The privacy result is the only routing authority immediately before the
   external AI call. If its classification is not `normal` (or its derived
   `gemini_allowed` is false), `process_bytes` returns `privacy_blocked` before
   `self.ai.analyze_receipt(...)`.
4. Only `normal / ready_for_gemini` reaches the existing Gemini receipt path.
   That path owns the existing Sheets writes to `レシート`, `取込データ`, and,
   after its existing checks, `支出明細`.
5. `app/drive_receipts.py:should_archive_result` owns the existing move to
   `receipt_processed`; this routing work does not alter it.

The exact external-AI safety boundary is therefore the `evaluate_receipt_privacy`
return check in `ReceiptPipeline.process_bytes`, immediately before
`GeminiAI.analyze_receipt`. `require_receipt_ai_permission` independently
re-checks exact bytes for direct AI adapters.

## Routing contract

| local gate result | document route | Gemini | production action |
|---|---|---:|---|
| `classification=normal`, `status=ready_for_gemini` | GENERAL | allowed | existing Gemini → existing Sheets path |
| `classification=medical`, `status=needs_review` | MEDICAL | forbidden | privacy block + optional local shadow review item |
| `classification=medical`, `status=confirmed` | MEDICAL | forbidden | existing complete local privacy preview; no unresolved item |
| `classification=sensitive_unknown` | UNKNOWN/BLOCKED | forbidden | hold/review; fail closed |
| extraction failure or malformed gate state | UNKNOWN/BLOCKED | forbidden | hold/review; fail closed |

This uses the existing medical/privacy classifier and gate. There is no parallel
production classifier and no user-maintained General/Medical folder split.

## Medical local shadow handoff

`ReceiptPipeline` accepts an optional `medical_review_observer`. It is invoked
only after the existing privacy authority has classified a source as Medical and
before the blocked result is returned. The observer receives only the source
identity and the data-minimised gate result; raw bytes and OCR text never cross
this contract. Observer failures are contained and cannot change the privacy
block, Gemini eligibility, Sheets authority, or Drive archive behaviour.

`app/medical_inbox_handoff_shadow.py:MedicalInboxHandoffShadow` is the opt-in
adapter. With `store_path` configured, it persists only a versioned JSON
document containing value-free review metadata. Writes use a same-directory
temporary file, `fsync`, and atomic replace; malformed stores fail closed.
For unresolved Medical decisions it calls the existing canonical
`build_medical_review_item` and `reconcile_medical_review_item` functions. The
source identity becomes a keyed opaque digest, repeated observations are
suppressed across process restarts, and the retained item contains no amount,
issuer, patient data, or OCR text. A missing/short identity key is rejected;
the key itself is never serialized. The adapter performs no external AI call,
Sheets write, Drive move, or production decision handoff. `sensitive_unknown`
remains on hold and is not coerced into the Medical queue.

## Role of `general_receipt_preview.py`

`app/general_receipt_preview.py` remains a local, read-only diagnostic preview.
It is useful for measuring date/merchant/total candidates, ambiguity, duplicate
signals, and a hypothetical `取込データ!A:L` row. It is not the production
parser, does not call Gemini, has no writer, and must not replace
`ReceiptPipeline`.

The preview's field extraction and OCR tuning are intentionally not part of the
privacy routing contract. General OCR accuracy and store-specific rules are
outside this phase.

## Private routing evidence

Using 8 ordinary receipt pages and 2 Medical controls from the user-provided
private Drive folder, after visual confirmation and local OCR only:

- ordinary: 7 `normal / ready_for_gemini`, 1 blocked/review; no unnecessary
  Medical classification was observed;
- Medical controls: 1 `medical / needs_review` and 1
  `sensitive_unknown / blocked`; Gemini eligible 0/2. The Medical control
  produced one safe review item; replay was `duplicate_suppressed`. The unknown
  control remained on hold;
- no receipt image or OCR text is stored in Git or sent to external AI;
- no production Sheets write was performed.
