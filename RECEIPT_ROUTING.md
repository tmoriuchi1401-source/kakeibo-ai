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
| `classification=medical` | MEDICAL | forbidden | existing privacy-block/review result |
| `classification=sensitive_unknown` | UNKNOWN/BLOCKED | forbidden | hold/review; fail closed |
| extraction failure or malformed gate state | UNKNOWN/BLOCKED | forbidden | hold/review; fail closed |

This uses the existing medical/privacy classifier and gate. There is no parallel
production classifier and no user-maintained General/Medical folder split.

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

- ordinary: 5 `normal / ready_for_gemini`, 3 blocked/review; no unnecessary
  Medical classification was observed;
- Medical controls: 1 `medical / needs_review` and 1
  `sensitive_unknown / blocked`; Gemini eligible 0/2;
- no receipt image or OCR text is stored in Git or sent to external AI;
- no production Sheets write was performed.

