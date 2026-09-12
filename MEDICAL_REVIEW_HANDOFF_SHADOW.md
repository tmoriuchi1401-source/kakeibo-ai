# Medical reviewed assertion handoff shadow

## Decision

Overall: **A (shadow boundary)**. A reviewed assertion can be authenticated and
bound to the existing review item, source unit, materialization, and provenance
without carrying an amount, candidate, write plan, or authority. A successful
handoff stops at `validated_for_authority_evaluation` and explicitly requires
existing production authority. It does not make a Medical receipt writable.

Boundary rescue remains C, coherent rescue remains C/review-only, and geometry
remains B/shadow. No rescue selector or promotion is part of this phase.

## Existing authority and future connection point

Medical business authority currently belongs to the local production chain:

`evaluate_receipt_privacy -> collect_payment_evidence -> resolve_payment_evidence`

Only that chain can confirm a Medical payment candidate under current acceptance
rules. The review assertion is not an input candidate and cannot influence its
amount or confirmation result.

Receipt writes are performed inside `ReceiptPipeline.process_bytes` after its
own validations. That pipeline currently blocks every Medical classification
before AI and before all Sheets calls. There is no current Medical write-plan
constructor or Medical writer path. The smallest future connection is therefore
a read-only authority-integration preview that uses a validated assertion only
as permission to re-evaluate the original bound receipt unit with the existing
local privacy/evidence authority. Any later Medical write plan requires a
separate, explicit production design and must consume the authority result—not
the assertion's decision enum—as its business value.

## Assertion schema

The frozen `ReviewedAssertion` contains only:

* fixed handoff schema version;
* stable HMAC review-item and receipt-unit references;
* fixed `reviewed` status and decision enum;
* review/parser/policy provenance versions;
* trusted materialization binding digest;
* positive review revision;
* deterministic assertion ID and HMAC integrity tag.

The HMAC key and source identity are transient inputs and are never represented.
The materialization digest is supplied by the trusted local materializer; the
reviewer cannot supply an amount or arbitrary candidate. Extra fields and free
text are rejected with a fixed data-free error.

## Binding and provenance validation

Evaluation recomputes and constant-time compares the receipt-unit reference,
review-item reference, deterministic assertion ID, and integrity tag. It also
requires the assertion and item to be reviewed, exact provenance equality,
current parser/policy compatibility, and exact expected materialization digest.

Results are fixed privacy-safe states:

* `validated_for_authority_evaluation` with next boundary
  `requires_existing_production_authority`;
* `rejected_binding` for identity, source/unit, materialization, integrity, or
  conflicting replay failures;
* `stale_provenance` for parser/policy mismatch;
* `invalid_review_state` for an unreviewed item;
* `duplicate_replay` for an already accepted identical assertion.

Every result has `write_authorized=false`, `write_plan_created=false`, and
`state_changed=false`. Only a validated result names the next authority boundary;
no result contains an amount, candidate, or write plan.

## Replay and privacy

Assertion construction and evaluation are pure and deterministic. Re-evaluating
the same assertion with the same inputs returns the same result. When the caller
supplies the already accepted assertion ID, the result is `duplicate_replay` and
does not approach authority. A different assertion for an already accepted item
is rejected. This module owns no mutable replay ledger and invokes no writer,
executor, apply function, Sheets, Drive, or external AI.
