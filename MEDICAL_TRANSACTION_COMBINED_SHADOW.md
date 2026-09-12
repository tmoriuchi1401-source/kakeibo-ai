# Medical transaction combined shadow

`app.medical_transaction_combined_shadow` binds two independently validated
evidence origins immediately before the existing authority-evaluation boundary:

* an admitted `IMAGE_AI_CANDIDATE` amount; and
* a `LOCAL_OCR_ISSUER_SELECTOR` facility.

The origins and provenance are never merged. Amount evidence retains the
image-AI result ID, model/prompt/schema provenance, admission outcome, approved
crop digest, and manifest lineage. Facility evidence retains the full printed
OCR text, source region, facility type, selector version, complete OCR-page
digest, selection reason, and referenced providers.

Combination requires exact source, source-image materialization, unit, and page
agreement. The amount is re-evaluated through the existing shadow admission
policy and the facility result is recomputed from the bound OCR page. A supplied
facility result must exactly match that recomputation. The combined result is
HMAC-bound and duplicate or conflicting replay fails closed.

`READY_FOR_EXISTING_AUTHORITY_EVALUATION` grants no authority. Production
authority, write authority, write-plan creation, and state mutation are always
false. Missing or ambiguous evidence, role injection, provenance mismatch, or
binding mismatch returns `REQUIRES_HUMAN_REVIEW`.

The fixed ten-unit offline replay produced ten correct amount+issuer pairs and
zero incorrect authority-ready pairs. Ground truth was used only after each
combined result existed. Real receipts, OCR dumps, adjudication records, and
replay output remain local and outside Git.
