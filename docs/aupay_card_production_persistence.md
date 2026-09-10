# au PAY card production persistence and capability seal

This phase adds a formal one-shot production-canary capability path. There is no
CLI command and `PRODUCTION_CAPABILITY_ENABLED` remains false, so there is no
standing authority for this historical canary API. A caller must use the public issuance and
execution APIs with protected repo-external inputs. This implementation phase
did not itself execute a real canary or any production write. The later,
separately bounded daily-arrival authority is documented in
[`aupay_card_recurring_production.md`](aupay_card_recurring_production.md); it
does not expose or reuse this exact-one human-approval route.

## Protected audit key

`ProtectedAuditKeyProvider` reads a JSON document outside the repository with a
safe `key_id` and base64-encoded `key_b64`. Decoded key material must meet the
existing 32-byte minimum. Missing material returns unavailable; malformed,
weak, unsafe-ID, and repository-local key files fail closed. The provider's
representation and exceptions never include the path or value. It does not
generate a fallback, modify environment variables, or write `.env` files.

Deployment must place the file using the host's protected secret mechanism and
restrict its ACL. This repository contains neither a key nor an example value.

## Durable journal and run manifests

`SqliteAttemptJournal` uses SQLite WAL mode, `synchronous=FULL`, an immediate
write transaction, append-only database triggers, and a SHA-256 hash chain.
Every append revalidates the complete chain, legal stage/state pairing,
monotonic timestamps, and immutable run/attempt/candidate/batch binding. A
failed durable append raises before transport invocation. Corrupt or internally
inconsistent history makes the backend unavailable.

The persisted state contains identifiers, states, timestamps, and reason codes;
it contains no merchant, mail body, credential, token, or key. A
`write_attempted` event is committed before the single API request. Reopening
the database after a crash recovers an uncertain attempt by read-back only:
exact presence confirms, definite absence becomes explicitly retry-eligible,
and an unreadable result stays unknown and stopped.

`SqliteRunManifestStore` stores the immutable run UUID, aware absolute source
window, IANA timezone, absolute query, plan creation time, target binding
version, keyed plan reference, key ID, and candidate accounting. A hash protects
the stored payload from undetected accidental mutation. Reusing a run UUID with
different content and persisting a relative Gmail query are rejected.

The database path must be configured outside the repository. SQLite supports
durability and exclusive access across processes sharing that database file,
which fits a local Windows host or a self-hosted Actions runner with protected
persistent storage. It is not a cross-host consensus service. A hosted runner
without shared durable storage must not be used for a canary; adding a remotely
atomic persistence/lease backend would be a separate reviewed capability step.

## Cross-process lease

`SqliteLeaseManager` acquires and replaces a lease in `BEGIN IMMEDIATE`
transactions. A target is locked more strictly than target-plus-run: no two runs
may write the same target concurrently. It supports renewal, expiry, atomic
stale takeover, crash recovery after expiry, and refuses release by a stale or
wrong owner token. Backend errors and live-lock contention stop before write.

## Protected human approval and one-shot capability

`ProtectedCanaryApprovalProvider` reads a repo-external JSON approval envelope.
It binds a human approval reference, the exact content-bound
`canonical-item-v2` reference,
the exact keyed target reference, `batch_size: 1`, and an aware expiry. Issuance
also recomputes the full-plan, exact-one projection, candidate, and target
references, requires a schema-v3 `production_canary` run manifest, and caps TTL
at five minutes. V1 candidate references remain readable only as legacy audit
identifiers and are rejected as production approval authority.

The public exact-one projection API selects one already-eligible identity from
a validated full plan without private constructors. Missing, withheld, review,
invalid, or duplicate authority fails closed. The full plan remains immutable;
the projection has truthful one-item accounting and cannot carry a second item.
The manifest separately binds the complete fresh plan, projected plan, selected
v2 candidate content, exact target, batch size, audit key ID, run UUID, and
absolute source window.

`validate_production_approval_preflight` performs the same approval, candidate,
target, expiry/TTL, and keyed-reference checks without issuing a capability. It
also requires the caller's expected run UUID and absolute source window to
match the immutable production manifest. The API has no journal, lease,
capability-store, or transport argument and does not write the approval file or
SQLite state. A successful result reports zero capability, lease, journal,
transport, and external-write counts; capability issuance remains a separate
explicit operation.

`SqliteCapabilityStore` stores only a token hash and privacy-safe bindings. Its
atomic state machine is `issued -> claimed -> dispatching -> sealed`. Run IDs
and candidate/target pairs are unique, so another capability cannot reopen the
same approved canary. Claim and dispatch are compare-and-set operations. Every
execution exit attempts reseal; if storage itself fails, a claimed or
dispatching capability remains non-reusable and therefore fails closed.

## Real transport, capability gated

`SealedSheetsCandidateTransport` has only a one-candidate `write_once` method.
It validates the exact spreadsheet ID, approved `取込データ` worksheet, exact
12-column header, candidate authority, purchase kind, positive amount, matching
claimed capability, and the durable `write_attempted` journal event. Dispatch
then atomically consumes the capability before the fixed `取込データ!A:L` RAW
append. It has no arbitrary-range API. A successful response is only an
acknowledgement; exact post-write read-back is still required for confirmation.

Synthetic and real authority are separate public functions.
`execute_synthetic_one_shot_canary` accepts only `synthetic_only` transports;
`execute_production_one_shot_canary` accepts only the sealed Sheets transport.
The latter is the only formal real-transport route and the adapter independently
checks the capability store and journal immediately before dispatch.

## Read-only pre-canary artifact

`build_precanary_manifest` selects at most one candidate using the established
keyed deterministic ordering. It records the fixed window, target and selection
HMAC references, pre-read classification, withheld count, readiness, and
privacy-safe blockers. It does not expose merchant data or the spreadsheet ID
and always reports zero external writes. Missing key, journal, target, lease, or
candidate readiness produces a stopped manifest rather than bypassing a gate.

Before executing a separately authorized canary, operators still need a
provisioned production key, protected approval file containing the approved
candidate reference, external protected SQLite location, persisted fixed
real-data run manifest, exact target reference, and reviewed Google write-scope
credentials. Recovery is idempotent read-back, not rollback and not blind retry.
