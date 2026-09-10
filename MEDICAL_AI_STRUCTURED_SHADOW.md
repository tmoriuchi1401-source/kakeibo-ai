# Medical AI structured extraction shadow

`app.medical_ai_structured_shadow` is an isolated, review-only experiment. It
reuses the private RapidOCR observation DTO and the existing medical evidence
helpers, but it has no production caller, writer, Drive/Sheets adapter, selector,
allowlist mutation, or apply surface.

The boundary has two explicit local source classes:

- `synthetic_fixture`: may receive a single-use outbound capability only when
  transport is explicitly enabled and the separate kill switch is disengaged.
- `real_medical`: may be projected and evaluated offline, but the final outbound
  gate always rejects it, even under an otherwise enabled policy.

Both policy controls default to no transport. The module never infers source
class from OCR text, a filename, a path, or provider metadata.

## Anonymous request schema

```json
{
  "schema_version": "medical-ai-structured-shadow-v1",
  "synthetic_document_id": "doc_<32 random letters>",
  "unit_ref": "unit_<32 random letters>",
  "provenance_ref": "provenance_<32 random letters>",
  "ocr_provenance_schema_version": "medical-ocr-observation-v1",
  "tokens": [
    {
      "token_id": "token_<32 random letters>",
      "unit_ref": "unit_<same request value>",
      "anonymous_text": "<PAYMENT_LABEL> | <NUMERIC> | <NEGATIVE_CONTEXT> | <REDACTED>",
      "normalized_geometry": {
        "x": 0,
        "y": 0,
        "width": 0,
        "height": 0
      },
      "confidence_bucket": "high | medium | low"
    }
  ]
}
```

Geometry is normalized locally to integer ten-thousandths of the image frame.
OCR text is never copied to the request. Only fixed semantic placeholders are
allowed. Patient/facility names, patient IDs, addresses, filenames, paths,
Drive IDs, image bytes, OCR engine/model hashes, source IDs, exact amounts, and
raw OCR strings are absent. The random document/unit/token/provenance references
carry no source identity.

The corresponding raw OCR, exact geometry, amount parse, unit/page provenance,
source fingerprint, and request integrity seal remain local and repr-hidden.

## Fixed AI response schema

```json
{
  "schema_version": "medical-ai-structured-shadow-response-v1",
  "request_schema_version": "medical-ai-structured-shadow-v1",
  "document_id": "the request synthetic_document_id",
  "unit_ref": "the request unit_ref",
  "provenance_ref": "the request provenance_ref",
  "document_type": "medical_receipt | not_medical | unknown",
  "payment_amount_token_id": "an existing token_id or null",
  "payment_label_token_ids": ["existing token_id"],
  "decision": "select | abstain | unresolved",
  "reason_code": "a fixed enum"
}
```

The response has no amount field and no free-text reason. A model can only
select request-scoped OCR token IDs. Unknown/duplicate fields, duplicate JSON
keys, invalid UTF-8, non-finite values, an oversized response, invented token
IDs, mismatched document/unit/provenance/schema values, and inconsistent
decision fields fail closed.

## Local evidence binding

A `select` response is accepted as a shadow candidate only when all of the
following local checks pass:

- the sealed build still matches the original OCR observation;
- document, unit, provenance, and request/response schema bindings match;
- every selected token ID exists in that sealed build;
- the selected amount token contains exactly one locally valid numeric OCR run;
- every selected label token is an existing exact strong payment label;
- amount and label evidence belong to the same receipt unit and page and have
  overlapping vertical geometry (or are the same OCR region);
- the amount is not in existing excluded/negative context; and
- no second locally valid amount competes for the same strong-label evidence.

Failure is data-free and remains `needs_review`. Success also remains
`needs_review`; the local amount is not sent back to the provider and is never
passed to the production resolver. `production_authorized` and
`write_authorized` are literal `false` in every result.

## Transport and offline evaluation

The real transport uses the existing fixed Gemini HTTPS executor, response
envelope parser, content-type checks, bounded reads, and the dedicated
`MEDICAL_GEMINI_SHADOW_API_KEY`. It accepts only a gate-issued
`ValidatedSyntheticRequest`; a mapping, observation, image, or real-medical
build cannot be submitted. There is one request and no retry or fallback.

Tests use only synthetic observations, fake keys, and fake HTTP executors. The
offline evaluator returns fixed integer counters only. It is intended for local
M1-M8 evaluation after RapidOCR has run under its existing offline guard; it
does not return token IDs, OCR text, amounts, geometry, paths, or source IDs.

No external medical-data request and no Drive/Sheets write are authorized by
this checkpoint. Promotion or production wiring is explicitly out of scope.

`prepare_structured_shadow_from_level2` requires an existing
`Level2ShadowEvaluation` and carries only its completeness state; it never
copies a Level 2 candidate ID or value. After AI response validation,
`handoff_to_existing_review_first` can link the data-free outcome to an already
pending `MedicalReviewItem`. That handoff deliberately drops the local amount,
cannot create or update review status, and keeps all authority flags false.
