# Anonymous Gemini Free Tier shadow boundary

This is a local, shadow-only preparation layer.  It has no Gemini SDK import,
API key handling, request code, retry, fallback, billing setting, or production
caller.  `GeminiFreeTierShadowPolicy` accepts only the literal `free` tier and
is disabled by default.  If Free Tier terms, quota, rate limits, credentials, or
provider behavior become unacceptable, leave it disabled: there is no paid tier
or automatic provider fallback in this design.  A future client must convert any
provider/quota failure to local `needs_review`, with no retry to a paid service.

## Local input and page isolation

`build_anonymous_shadow_payload` accepts one complete `OcrObservation`, which is
already one immutable image / one rendered PDF page.  It never accepts a list of
observations or a PDF/document envelope.  Source unit/page identity, image digest,
filename, path, Drive ID, model provenance, OCR text, OCR tokens, original
polygons, confidence scores, and the actual amounts remain local.  The wire-only
`unit_ref` is fresh random opaque data per build and is not derived from source
metadata.  Amount correspondence is held in `LocalAmountMap`; response handling
must use that local map and must never add it to the payload.

`prepare_anonymous_shadow` is the fail-closed local entrypoint. It turns every
unsafe/ambiguous semanticization failure into a data-free `needs_review` result;
only `shadow_ready` contains a private build. A private SHA-256 fingerprint binds
that build to the exact local observation before `prepare` serializes bytes. The
fingerprint is neither an input field nor wire data, so a different source cannot
substitute its literals for final-byte scanning.

## Allowlist wire schema

The only allowed serialized object is:

```text
{
  schema_version: "medical-anonymous-shadow-v1",
  unit_ref: "unit_<random>",
  mode: "shadow_only",
  state: "shadow_ready",
  structure_state: "unresolved",
  competing_structure: boolean,
  regions: [{id, kind, context, amount_id, geometry, confidence, status}],
  relations: [{left, right, kind}]
}
```

`kind` is `numeric_evidence` or `context_anchor`; `context` is `payment` or
`excluded`; `amount_id` is an opaque `amount_A`-style ID only on numeric evidence;
`geometry` has exactly normalized `x/y/width/height`; confidence is only
`high/medium/low`; relations are only row/column/overlap between included opaque
region IDs. `structure_state` is always `unresolved`, and `competing_structure`
is an allowlisted boolean. There is no free text, raw OCR field, arbitrary
metadata map, provider prompt, page number, receipt identifier, extension field,
or unknown semantic label.

## Fail-closed outbound gate

`FinalOutboundGate` independently validates exact keys, native types, enum values,
opaque IDs in their builder-defined sequence, finite normalized geometry, bounded
unique relations, and relation references immediately before canonical JSON bytes
are made. It rejects extra/missing fields, subclasses and unexpected types. It
also scans those final bytes against locally supplied raw OCR literals and amount
renderings, providing a regression tripwire beyond schema validation. The policy
also verifies the generated opaque unit reference before preflight bytes are
exposed to any future sender; no sender is implemented in this phase.

Local semanticization recognizes only explicit existing payment/excluded context.
Possible-payment context, unassigned numeric text, multiple numeric runs,
malformed amounts, invalid/incomplete OCR, invalid geometry, or unresolved layout
produce no payload and must remain `needs_review`.  This intentionally favors
withholding an ambiguous observation over exposing source text to a Free Tier
service that may retain inputs for service improvement.

Production receipt OCR, resolver, Gemini adapter, requirements, and persistence
are unchanged.  The maintenance review point is this policy class: a Free Tier
specification change stops the route by keeping `enabled=False`; it cannot change
to paid behavior through configuration.
