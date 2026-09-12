# Medical image-AI result shadow

This opt-in module records image-AI output as a separate evidence class. It is
not an OCR observation, OCR candidate, or human-reviewed assertion, and it has
no production caller.

## Boundary

The only successful terminal state is:

```text
stored image-AI answer
  -> strict schema and ambiguity validation
  -> source / unit / page / crop-byte binding
  -> model / prompt / answer-schema provenance binding
  -> authenticated IMAGE_AI_CANDIDATE
  -> validated_for_authority_evaluation
  -> stop: requires_existing_production_authority
```

The boundary object keeps the amount together with its image-AI evidence type,
crop binding, model/prompt provenance, and authenticated result ID. All
production, write, write-plan, and state-change flags are fixed to false.

Ambiguous and unreadable results are `IMAGE_AI_ABSTENTION` records. They carry
no amount and stop at human review. Invalid schema, stale provenance, changed
crop bytes, cross-unit binding, tampering, and replay all stop with no amount
and no next authority boundary.

Replay handling is caller-supplied and read-only. Repeating the same accepted
result reports `duplicate_replay`; a different result presented after an
accepted ID reports `conflicting_replay`. Neither creates or mutates a ledger.

## Fixed evaluation replay

The local replay adapter reads the already saved manifest, final crop bytes,
answers, attempt metadata, execution summaries, and assisted-review session. It
does not copy those private artifacts into Git and performs no network or write
operation. A later human adjudication is supplied explicitly rather than
rewriting the earlier review record. For the confirmed Unit 10 correction:

```powershell
python -m scripts.replay_medical_image_ai_results_local <artifact-root> <human-session.json> --ground-truth-override 10=630
```

The JSON report contains only unit-level validation/match statuses and fixed
zero counters for external AI, HTTP, production, Drive, and Sheets writes.

## Deliberately unchanged

No production OCR selector, structured-shadow selector, allowlist, threshold,
privacy gate, negative-context rule, writer, authority implementation, or
Drive/Sheets integration imports this module. It creates no write plan and does
not call the existing authority evaluator; it stops at that evaluator's input
boundary.
