# au PAY card production writer safety contract

The general writer state machine remains synthetic-only:
`execute_synthetic_write` rejects any transport not marked `synthetic_only`.
The production module adds a distinct one-shot canary entry point; no CLI
command imports or invokes either path.

## Immutable source population

`ProductionRunManifest` binds an internally-authorized canonical plan to a UUID,
an aware plan creation timestamp, and `FixedSourceWindow`. The source window has
aware start/end timestamps, an IANA timezone, and a query containing matching
absolute `after:` and `before:` dates. Relative Gmail operators such as
`newer_than:1y` are rejected. A changed population produces a different keyed
plan binding and cannot reuse the run manifest.

Standing production authority remains disabled. A `production_canary` manifest
is valid only as one input to the separately protected, short-lived capability
path; by itself it grants no transport authority.

## Target and key binding

`TargetBinding` requires a caller-configured spreadsheet ID, worksheet, exact
header, and binding version. `ReadOnlySheetsTargetInspector` reads the header;
ID, worksheet, or schema mismatch stops before a write request. IDs are not
hard-coded and are excluded from dataclass representations.

Production audit/canary stability requires a protected persistent HMAC key of
at least 32 bytes and a non-empty key ID. The environment provider accepts a
base64 key and key ID without logging either value. Missing, malformed, or
ephemeral key material fails closed. Secrets must be supplied by the deployment
secret store and never committed or written to `.env`.

## Journal and exclusive lease

The attempt journal is mandatory and must declare persistent availability. Each
attempt records run ID, canonical identity, attempt ID, deterministic batch
reference, state, timestamp, and reason code. It stores no merchant, email body,
credential, or token. The enforced transition order is:

1. `pre_read`
2. `write_attempted` — durably recorded before calling the API
3. `write_result`
4. `post_read`
5. `final`

An exact pre-existing identity may transition directly from `pre_read` to
`final`. Invalid or time-regressing transitions are rejected.

An exclusive target/run lease is acquired before revalidation and renewed before
each candidate. A live lease cannot be replaced. An expired lease is stale and
may be atomically replaced, but the old owner cannot renew or release the new
lease. Failure to acquire or renew stops execution; there is no lock-bypass
fallback.

## Batches, writes, and recovery

Batches have a hard ceiling of 100 candidates. A one-item canary uses the same
keyed deterministic selection as the dry-run executor. The controller does not
assume batch atomicity and stops the remaining batch as `not_started` after an
unknown, conflicting, failed, or explicitly retry-eligible outcome.

Every candidate follows pre-read, durable journal, one write request, result
classification, bounded read-only verification, and final journal state. A
successful API response never confirms a write. Only one exact read-back row is
`write_confirmed`.

Write requests are never automatically retried. Timeout, connection loss, HTTP
ambiguity, or an unexpected transport exception is `outcome_unknown`, followed
only by bounded reads. Exact presence confirms the write; conclusive absence
after an ambiguous/not-sent request is `retry_eligible`; unreadable state remains
`outcome_unknown`. Acknowledged-but-invisible writes remain unknown rather than
being resent.

`ReadBackPolicy` allows 1–10 deterministic read attempts with a fixed delay
schedule capped at 30 seconds per interval. Exhausting the window never causes a
second write request.

## One-shot production capability

The contract now has repo-external protected-key loading, an append-only and
hash-chained SQLite journal, immutable fixed-run persistence, a cross-process
SQLite lease, restart recovery, and an exact-target Sheets transport adapter.
They remain unreachable from the CLI. Production issuance now requires an exact
repo-external human approval envelope and creates a five-minute-or-shorter
SQLite-backed one-shot capability. Details and backend deployment limitations are in
[`aupay_card_production_persistence.md`](aupay_card_production_persistence.md).

A separately approved execution phase is still required to provision real
secrets and durable storage, review write-scope credentials, persist a real
fixed run, and supply the protected approval record before a one-item canary.
