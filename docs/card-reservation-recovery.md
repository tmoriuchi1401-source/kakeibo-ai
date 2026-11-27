# Unused card reservation recovery

This extends the existing reconciliation orchestrator and batch writer. It does
not grant execution approval, add a writer/database/infrastructure, enable a
schedule, or authorize expense creation. Production application remains a
separate owner decision on the exact release SHA/tree and private record.

## Proof and state order

1. Verify the signed fixed record, current owner/main/validated release,
   shared `kakeibo-production` exclusion, exact independent state revisions,
   ledger values/formulas, ACL, protected documents and fixed source evidence.
2. Validate every existing UNKNOWN archive and immutable history before effects.
   The business window, source/candidate/status/amount plan, financial snapshot,
   protected documents, ACL and target must equal the failed approved record.
   Recovery changes execution authority and reservation ownership only.
   Require the failed reservation's exact record/manifest/capability, expiry,
   empty attempt/journal/leases, and a no-dispatch barrier. An issued capability
   and empty persisted journal alone never prove no financial request occurred.
3. Use the existing capability store to seal only the exact expired unused row.
   Preserve its ID, token hash, binding and timestamps. Record before/after rows,
   hashes, no-dispatch proof and owner-record digest in an append-only receipt.
4. Preserve the current native/parent bytes as a new UNKNOWN archive. Keep all
   prior UNKNOWN/reservation rows unchanged. Reserve a new run and capability;
   persist archive, revocation receipt and reservation atomically in the normal
   native envelope, then independently read it back. Local cache revocation alone
   is not production revocation. Reserve the parent pending-to-pending and read
   it back; no release/ready shortcut is used.
5. Persist `REPAIR_DISPATCH_INTENT` in the same existing recurring store and
   independently read it back before entering the existing batch writer. Its
   presence means dispatch might have happened and forbids unused recovery.
6. Execute the exact approved import-only projection once. Require acknowledgement,
   sealed capability, confirmed journal, exact complete financial readback,
   aliases and both UNKNOWN archives. Persist the outcome pending before
   advancing business coverage; independently read native and parent completion.

The original consumed workflow remains consumed. The new manual workflow allows
one dispatch per separately approved release, rejects attempt2 and another
dispatch at that SHA, and requires all earlier failed releases to appear in its
signed ancestry. A new UUID or changed release alone creates no authority: the
fixed record digest, actual pending ownership and protected approval must match.

## Existing identities and fences

The batch-reference UNIQUE constraint remains. A recovery projection binds its
HMAC authorization context to the approved recovery-record digest. This creates
a distinct authorization for an independently approved recovery while preserving
canonical transaction IDs, the full plan, target binding and all old capability
rows. It cannot reuse an old protected batch approval. Default projections keep
their existing references. There is no schema migration.

Legacy reservations require unchanged parent bytes **and revision**. Reservations
created by this protocol can also be proved unused after parent reservation if
their mandatory durable intent is absent and execution identity matches the
archived failed attempt. Repeated failures at those boundaries use a new fixed
record/run/capability and retain the complete archive chain. Failed pre-reservation
dispatches are not silently exempted from the ancestry fence.

Any intent, claimed/dispatching capability, journal event, lease, unexpected
parent change or missing proof denies unused recovery. After financial intent,
partial/unknown results and final-state acknowledgement loss require readonly
reconciliation; this entry never retries, rolls back or deletes rows. Existing
state/capsule/history size bounds still apply.

## Completion and time

`preserved_unknown_proofs` follows immutable successor links to a new completed
run, verifies its archive-chain digest and exact revocation transitions, and
preserves all other old rows. It accepts only the closed set of supported failed
workflow identities. It does not change an old attempt to success or ignore
pending, unsealed capabilities, unresolved journals or leases.

Business checkpoint time describes mail coverage. It is separate from manifest
creation, capability revocation/issuance and repair execution time. A failed
repair may occur after the business coverage timestamp; it must precede its new
repair execution. Revocation must precede new capability issuance. The parent
counts must match the protected checkpoint result. A fresh runner verifies the
actual successful completing Actions run and every archived failed attempt
before the subsequent processing guard accepts the state.

## Validation and release boundary

Isolated tests start with the same generation/phase shape as the held reservation
and use invented sources, a synthetic approval/key, existing SQLite stores and
the real batch writer against an isolated Sheets transport. They cover the full
revoke/reserve/append/readback/checkpoint/parent-ready/fresh-restore/guard path,
repeated unused reservations, response loss, runner loss and tampering. Review,
category and formula changes hold completion. This is not live posting proof.

The private readonly preflight uses only Drive/Sheets readonly scopes and the
existing Gmail readonly grant. It neither signs approval nor issues/revokes a
capability. Hosted HMAC-secret continuity, a fresh short-lived signed capsule,
release CI/validated equality, exclusion and final live preconditions are checked
again only after explicit production approval. No receipt deployment, AI,
original operation, catch-up Apply, schedule change or fence release is included.
