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
renderings, including normalized ASCII/full-width digits, comma or dot grouping,
whitespace-separated digits, and JSON Unicode escapes. This provides a regression
tripwire beyond schema validation. The policy
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

## Synthetic response boundary

This phase still has no provider client, SDK use, API key, HTTP operation, or
production caller. `FinalInboundGate` accepts synthetic UTF-8 JSON bytes only;
it is intentionally not a Gemini parser. Its exact allowlist schema is:

```text
{
  schema_version: "medical-anonymous-shadow-response-v1",
  unit_ref: "the outbound random unit_ref",
  decision: "select" | "abstain" | "unresolved",
  amount_id: "amount_A" | null,
  confidence: "high" | "medium" | "low" | null
}
```

Only `select` may contain an amount ID and confidence; both values must be null
for `abstain` and `unresolved`. The amount ID must be one of that request's
existing IDs. There are no explanation, markdown, reasoning, OCR, amount,
metadata, filename, path, Drive ID, page, relation extension, arbitrary text,
or nested fields. Duplicate JSON keys are rejected during parsing, and response
size is bounded.

The local response binding is a SHA-256 digest of the build's private source
fingerprint, outbound `unit_ref`, and canonical anonymous request. It is never
placed on the outbound payload or response. The response must also echo the
unique random `unit_ref`, so a response built for another request is rejected.
The gate scans raw response bytes for locally retained OCR literals and concrete
amount renderings before parsing, including JSON-escaped text and normalized
full-width/grouped/whitespace numeric surfaces.

Only a returned `AcceptedAnonymousResponse` with the matching private binding
can enter `rehydrate_anonymous_response`. That is the sole inbound call site for
`LocalAmountMap.resolve`; its result remains local and `needs_review`, and does
not call or replace the production resolver. `receive_synthetic_shadow_response`
turns malformed/empty response, unknown IDs, schema/parser errors, and synthetic
timeout/quota/API/parser failures into data-free `needs_review` results. No
exception, report, or repr contains source OCR or actual amounts.

## Synthetic transport boundary and kill switch

There is still **no network implementation**. `FakeAnonymousShadowTransport` is
only a test double: it has no HTTP, SDK, URL, header, key, retry, telemetry, or
model call. The future transport protocol accepts only a
`ValidatedAnonymousBytes` wrapper. `FinalOutboundGate.validate_for_transport`
creates that wrapper after canonical serialization and the final private-literal
and numeric scans. Normal callers have no supported API to hand an observation,
raw dict, `LocalAmountMap`, image/PDF, filename, path, Drive ID, page number,
metadata, actual amount, or production state to transport.

Python is not a capability-secure runtime: hostile code executing in the same
process can inspect private attributes or module globals. The wrapper therefore
prevents accidental architectural bypasses and is enforced with exact runtime
type checks, but is not a sandbox against arbitrary in-process code. A real
deployment must additionally keep untrusted plugins/code out of this process.

`MedicalAnonymousShadowTransportPolicy` is independent from generic Gemini and
production settings. It is default false; `from_setting` enables only the exact
local value `"true"`, while unset, booleans, and malformed values are disabled.
`run_fake_shadow_transport` checks this kill switch before source binding, byte
preparation, or transport invocation. A real caller must retain that ordering
before API-key lookup as well. Disabled results are data-free `needs_review` and
the transport callback is not invoked.

The policy freezes the candidate model to exact stable
`gemini-3.1-flash-lite`; aliases such as `latest` are not allowed. It represents
the required no-tools, no-grounding, no-caching, no-batch, no-priority, no-retry,
and no-fallback rules locally. This is not proof of Free Tier at runtime: billing,
quota, and account tier must still be independently verified before any real
request, as documented in the preflight review.

## Transport response format gate

Before `FinalInboundGate` sees a response, `TransportResponseGate` accepts a
minimal synthetic `TransportResponse` only when its status is `ok`, its MIME is
exactly `application/json` or `application/json; charset=utf-8`, its body is
nonempty bytes at most 4 KiB, and strict UTF-8 decoding succeeds. Missing MIME,
other MIME/charset values, oversized bodies, and replacement decoding are
rejected. The gate scans raw decoded bytes for private OCR literals and concrete
amount surfaces before parsing; then it requires one complete JSON document using
duplicate-key rejection. Markdown fences, prose prefixes/suffixes, and trailing
garbage fail before semantic parsing.

Only opaque `ValidatedTransportResponse` bytes are passed to the strict semantic
inbound gate. Fixed data-free result codes cover `disabled`, `timeout`, `quota`,
`authentication`, `unavailable`, `transport_error`, `invalid_content_type`,
`response_too_large`, `invalid_utf8`, `malformed_response`, `privacy_rejected`,
`binding_rejected`, and `validation_rejected`. Raw exceptions, request/response
bodies, headers, keys, OCR, amounts, paths, and filenames are never put in a
result or repr. Each request has exactly one fake transport invocation; no retry,
model/provider fallback, paid route, or production apply path exists.
