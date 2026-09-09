# au PAY card production persistence and capability seal

This phase connects the writer contract to production-shaped persistence and a
real Sheets adapter without granting write capability. There is no CLI command
for this module, `PRODUCTION_CAPABILITY_ENABLED` is false, and capability
issuance always fails. No canary or production write can occur in this phase.

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

## Real transport, still unreachable

`SealedSheetsCandidateTransport` has only a one-candidate `write_once` method.
It validates the exact spreadsheet ID, approved `取込データ` worksheet, exact
12-column header, candidate authority, purchase kind, and positive amount. Its
request is fixed to `取込データ!A:L`, uses `RAW` input, and cannot accept an
arbitrary range. A successful response is only an acknowledgement; the writer
contract still requires exact post-write read-back before confirmation.

The transport cannot currently be invoked: the global production flag is
false, no `ProductionWriteCapability` can be constructed or issued, and the CLI
does not import this module. `evaluate_capability_seal` also enumerates the
future gates: explicit capability flag, protected persistent key, healthy
journal, acquired lease, verified target, valid fixed window, valid executor
authority, and explicit apply authority. Even if all inputs are marked ready,
this phase adds the unconditional `production_capability_disabled` blocker.

## Read-only pre-canary artifact

`build_precanary_manifest` selects at most one candidate using the established
keyed deterministic ordering. It records the fixed window, target and selection
HMAC references, pre-read classification, withheld count, readiness, and
privacy-safe blockers. It does not expose merchant data or the spreadsheet ID
and always reports zero external writes. Missing key, journal, target, lease, or
candidate readiness produces a stopped manifest rather than bypassing a gate.

Before a separately approved canary phase, operators still need a provisioned
production key, an external protected SQLite location (or reviewed remotely
atomic backend), a persisted fixed real-data run manifest, reviewed Google
write-scope credentials, and an explicit human-authorized seal-release design.
Recovery is idempotent read-back, not rollback and not blind retry.
