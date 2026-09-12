# Medical image-AI admission shadow

This policy decides only whether an already validated `IMAGE_AI_CANDIDATE` may
be presented to the existing authority evaluator. It does not call or replace
that evaluator and grants no production or write authority.

## Auto-admission contract

`AUTO_ADMITTED_FOR_AUTHORITY_EVALUATION` requires all of the following:

- the existing result validator accepts source, unit, page, crop bytes,
  provenance, result HMAC, and replay state;
- the result is a readable, unambiguous `IMAGE_AI_CANDIDATE` with a strict
  positive integer yen amount;
- exactly one observed payment candidate is bound into authenticated admission
  signals;
- the result-bound label evidence has payment, receipt, or billing context;
- no negative context veto is present;
- model, prompt digest, response schema, approval reference, and crop
  provenance are allowed by the explicit policy version;
- no manual adjudication conflict is known, and the conflict state is not
  unknown.

The policy has no ground-truth, human-amount, OCR-candidate, or AI-confidence
field. OCR agreement and confidence scores therefore cannot affect admission.

## Review routing

Every missing, invalid, stale, ambiguous, unreadable, duplicate, conflicting,
or unknown condition produces `REQUIRES_HUMAN_REVIEW`. Reasons are stored as a
closed machine-readable tuple with a deterministic primary reason. Invalid
binding and integrity failures are distinguished when the available safe facts
permit it.

An admitted outcome ends at
`ready_for_existing_authority_evaluation`. All outcomes fix production
authority, write authority, write-plan creation, and state changes to false.

## Fixed replay

The read-only replay consumes only the previously stored ten image-AI results,
approved crop manifest, and crop bytes:

```powershell
python -m scripts.replay_medical_image_ai_admission_local <artifact-root>
```

It does not load the human ground-truth session. Its report explicitly records
that ground truth, OCR candidate matching, and AI confidence were not used for
admission. No external AI, HTTP, production, Drive, or Sheets operation occurs.
