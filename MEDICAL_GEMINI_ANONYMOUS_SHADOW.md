# Minimal anonymous medical Gemini shadow boundary (v2)

This is an isolated, default-disabled shadow route. No production pipeline,
resolver, CLI, persistence, or background job calls it. A successful response
still returns local `needs_review`; it never confirms or applies a payment.

**v2 is a local-only hardening checkpoint for adversarial re-review, not approval
to send medical-derived data.** Prior synthetic connectivity results used v1.
No real v2 Gemini request or model-accuracy evaluation is part of this phase.
Neither anonymization, Free Tier eligibility, provider retention, product
improvement use, nor billing can be guaranteed by the code.

## Closed, candidate-only v2 wire schema

```text
{
  schema_version: "medical-anonymous-shadow-v2",
  unit_ref: "unit_<32 random letters>",
  candidates: [
    {candidate_id: "candidate_<32 random letters>"}
  ]
}
```

One to four candidates are allowed. No other field is accepted, including old
v1 fields. Request and response versions both change to v2; v1 has no live
compatibility route.

Coordinates, width, height, area, page size/number, source ordinal, confidence,
context enums, anchors, excluded regions, full relation graphs, and
observed/shadow_ready/unresolved/competing evidence fields are absent from the
wire. There is no field for OCR, an actual amount, a patient or facility name,
diagnosis, medication, filename, path, Drive ID, source digest or local map.

Candidate selection is local:
- Only a single well-formed amount in explicit payment context is a candidate.
- Candidate evidence must be complete and have confidence at least 0.9.
- Numeric possible-payment ambiguity is withheld, not sent as competing state.
- Excluded/unassigned regions and nonnumeric anchors contribute no wire evidence.
  This includes unrelated numeric text with no payment context; this route is
  not a complete receipt/payment resolver.
- Zero candidates or more than four, incomplete global observations, excessive
  input regions, or unsafe candidate evidence produce local withholding.

No layout graph is computed. A source observation is still one local image/page
DTO, but the code cannot authenticate the upstream provenance or detect a
dishonestly assembled multi-page observation. Local size/quality limits and
private-literal collision rejection remain fail-closed admission rules.

## Random identifiers, ordering and non-interference

The builder sorts amounts only locally, then shuffles them using
`secrets.SystemRandom` and assigns independent CSPRNG candidate identifiers.
Identifiers and order do not encode the original OCR ordinal, value rank or
reading order. Candidate IDs and unit_ref contain only letters after their
fixed prefix, reducing accidental numeric-literal collisions.

Tests fix the entropy stream ONLY in test code to demonstrate byte-for-byte
non-interference under input permutations, geometry/page/quality changes within
admission limits, and addition of irrelevant excluded/anchor/unrelated regions.
Production builds always use fresh entropy, so their literal bytes should not
be compared for this non-interference property.

`LocalAmountMap` retains the private ID-to-amount correspondence. The build seals
its canonical payload, source fingerprint, map and private-literal scan context
with a local integrity digest. A mutated payload/map/scan context is rejected
before transport or rehydration. These local hashes are never sent.

**Residual disclosure:** exact admitted candidate count (1–4), the existence of a
payment-candidate task, traffic timing and the authenticated provider project
remain observable. Random IDs remove persistent labels, not all linkability.
Repeated structures after removal of random IDs still share cardinality. There
is no differential privacy or population-level anonymity claim.

## Conservative selection capability

The v2 payload has no comparative evidence. The static instruction asks the
model to select only when there is exactly one candidate, or to abstain. With
multiple candidates it must abstain or return unresolved. The local inbound
gate independently rejects any multi-candidate select, even for an existing ID.

Single-candidate selection is structurally supported and rehydrates locally;
it is not independent proof that the amount is correct. Real model selection
accuracy, and parity with v1, are unmeasured. v1 multi-candidate selection
capability is intentionally not preserved at the expense of exposing layout.
If useful decisions require identifying structure, reconsider the Free Tier
design rather than silently restoring it.

## Outbound gate and one-use capabilities

Only `FinalOutboundGate.validate_for_transport` issues
`ValidatedAnonymousBytes`. The build-bound preparation path verifies the source
and integrity seal and runs final exact-schema/private-literal/numeric scans.
Transport revalidates canonical v2 bytes; raw dicts and OCR DTOs are rejected.

A local atomic send capability is shared across wrappers created from the same
build, including copies. A separate shared HTTP capability prevents reusing
constructed/copied HTTP requests. Claiming happens before invocation and is
never undone after timeout, HTTP failure or validation rejection.

A process-local, lock-protected replay registry also rejects reused unit_ref
and an identical source fingerprint rebuilt with fresh identifiers. It holds
at most 4096 used request references, never evicts them, and refuses further
sends when full. It stores no raw OCR, payload or amount. It is not persisted.

Limitations: process restart loses the ledger; a changed observation may have a
different fingerprint. This is not cross-process or durable receipt deduplication.
Hostile Python code in the same process can introspect private attributes;
these are accidental-reuse guards, not a sandbox. Any future invocation workflow
still needs explicit authorization and a bounded lifecycle. The policy remains
default false and is never automatically enabled or loaded from configuration.

## Fixed transport and redirect prohibition

The only endpoint remains HTTPS POST to
`generativelanguage.googleapis.com/v1beta/models/gemini-3.1-flash-lite:generateContent`.
The lazy environment provider can only read `MEDICAL_GEMINI_SHADOW_API_KEY`;
its variable is not a configurable generic-key fallback. The key enters only
the `x-goog-api-key` header, not body/query/repr.

The executor constructs a private `OpenerDirector` with explicit HTTPS, error
and rejecting redirect handlers. It does not use a global opener or install
proxy, authentication-retry, cookie, HTTP downgrade or alternate-provider
handlers. All 301/302/303/307/308 responses are rejected before Location parsing,
body draining, method conversion, credential forwarding or a second open.
All remaining non-success statuses fail closed as well.

The HTTP request capability is consumed before open. There is at most one
HTTP request attempt on this path, including failures. Mocked HTTPSConnection
tests exercise the real urllib handler chain and count `request()` calls.
DNS/TCP address probes are not additional HTTP requests. Socket timeout is 10
seconds; this is not a whole-operation wall-clock deadline.

There are no retries, fallback, tools, function calls, grounding/search, URL
context, code execution, upload/multimodal, cache, batch, priority or retrieval.

## Bounded, ordered response validation

1. HTTP status: error/redirect bodies are closed without reading.
2. Exactly one Content-Type header, semantically application/json; optional
   case-insensitive UTF-8 charset, quoted or unquoted, only. Unknown/duplicate/
   malformed parameters, non-ASCII or oversized Content-Type, other charsets and
   other media types are rejected. Compressed responses are rejected.
3. Read at most **32768 + 1 bytes** of the provider envelope. Overflow is
   `response_too_large`; never read the full oversized body into memory.
4. Strict envelope UTF-8, then whole envelope JSON with duplicate-key rejection.
5. Extract exactly one candidate with one text-only part; no function-call part.
6. Candidate must be nonempty and at most **4096 UTF-8 bytes**.
7. Existing independent candidate MIME/size/strict UTF-8 and privacy scan.
8. Whole candidate JSON with duplicate-key rejection.
9. Exact v2 response schema, request binding and known candidate ID checks.
10. Single-candidate select only; local rehydration after integrity/binding checks.

Injected fake HTTP executors are rechecked for status, MIME and envelope size
before parsing. The production executor performs MIME checking before any body
read. Metadata never becomes a prompt, follow-up message or returned result.

## Response schema and safe local results

```text
{
  schema_version: "medical-anonymous-shadow-response-v2",
  unit_ref: "the request unit_ref",
  decision: "select" | "abstain" | "unresolved",
  amount_id: "the selected candidate_id" | null,
  confidence: "high" | "medium" | "low" | null
}
```

The historic field name `amount_id` is retained on the response only; its value
is now the random request-scoped candidate_id. Select requires the sole known
candidate and a confidence enum. Abstain/unresolved require both fields null.
There are no explanations, metadata or arbitrary text fields. Provider
confidence is never a production authorization.

Data-free outcomes include `redirect_rejected`, `request_reused`,
`invalid_content_type`, `response_too_large`, `invalid_utf8`,
`malformed_response`, `privacy_rejected`, `binding_rejected`,
`validation_rejected` and the existing fixed HTTP/timeout mappings.
Request URL/body/headers, response body/Content-Type/failure metadata, private
maps, capabilities and API key values are repr-hidden. No logging is added.

## Local verification and next boundary

Dedicated privacy tests cover minimization, non-interference, ordering, residual
cardinality, v1 rejection, integrity seals and conservative selection. Dedicated
transport tests cover redirect credential isolation, one HTTP attempt, bounded
reads, parse ordering, copy/concurrency/rebuild replay and safe repr. Existing
anonymous-shadow regression tests retain strict schema/UTF-8/privacy/duplicate/
binding/rehydration/no-retry/production-isolation coverage.

All tests use synthetic observations and fake keys/executors or mocked
HTTPSConnection. Run without pytest cache/bytecode writes and with network and
real-key reads blocked. Next step is local-only adversarial re-review. No
medical-derived Free Tier send or production use is authorized by this checkpoint.
