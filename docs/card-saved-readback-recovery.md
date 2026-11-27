# Bounded GET readback for a fixed card save

This is a development candidate, not production execution approval. PR213 is
already deployed at `e306ed90a81600eec1aa2ba11326de44954c427e`.

## Observed stop and limits of the diagnosis

[Run45965280576](https://github.com/tmoriuchi1401-source/kakeibo-ai/actions/runs/45965280576)
stopped at `native_independent_readback`, with `state_changed_during_read`,
`guard_rejected`, no HTTP status, and financial writer not reached. Native123
was saved; parent1416 stayed byte/revision-exact. No dispatch intent, journal
event, lease or financial delta exists, and C0 remains unchanged.

Current readonly GETs match retained native122/native123 revision bodies and
the saved native123 bytes, SHA256, size and MD5. Native version is now 251,
with exactly one retained content transition from version249/native122.
Current metadata is stable. The failing request's before/after metadata values
were not logged, so its differing fields and underlying reason cannot now be
established. Propagation delay is a hypothesis, not a finding. No real write was
repeated to investigate it. Bank proof, Gmail authentication and original-source
reconciliation were reused without repeating the completed investigations.

## Narrow readback policy

`ReadOnlyState.observe()` remains one observation with strict before/after
metadata equality. On mismatch it now attaches only allowlisted differing field
names. Values, hashes, source details and raw exception messages never enter the
public diagnostics. Recovery attempts do not modify ordinary readonly policy.

Only the fixed repair's immediate post-save checks use `RepairReadbacks`:

1. Before one existing save, capture a stable pinned baseline using readonly
   GETs. Require the fixed ID, sole parent folder, JSON MIME, non-trash state,
   exact expected prior bytes/SHA256 and metadata digest, size and MD5. General
   pre-save guards do not retry. The session's metadata pin is not refreshed by
   an unrelated read or by observing a new value.
2. The existing native/parent store writes once. A rejected or ambiguous write
   raises immediately; it never enters the GET retry loop. Append and workflow
   dispatch likewise remain one-shot. No writer or save transport is replaced.
3. Freeze the expected saved bytes, SHA256, MD5 and size before the first
   independent read. Up to three GET observation groups, with delays .25/.5s,
   may resolve metadata that still contains known old/own-save values. Every
   group must return those exact saved bytes. Expected content is never updated.
4. Accept only identical metadata before/after with full size/MD5/body agreement,
   a new head revision consecutive to the pinned old head, exact revision size/
   MD5/time, and the existing save path's observed version increment of two.
   Version observations must not regress. Extra revisions, metadata churn,
   stable different content, target changes or unknown values are rejected.
   Missing history is not guessed harmless. Current-write revision metadata is
   independently obtained, rather than inferred from present-day content alone.
5. The shared hosted lock stays held. The existing guard is checked for each
   observation/retry and before acceptance; the original absolute deadline is
   checked around each GET. It is never extended. No HTTP/write retry is added.
   After successful readback, only this acknowledged own save moves the session
   pin. Ordinary subsequent reads must match that pin, catching later rollback.

The bounded policy covers native reservation, parent reservation, durable intent,
outcome, native checkpoint and parent completion. Unknown responses still stop.
Diagnostics retain stage/effects/error/class/status plus safe metadata field
names and bounded readback attempt count; no metadata values are printed.

## Native123 and three preserved UNKNOWNs

The previous effective execution record was reconstructed solely from the exact
archive's bank proof and its original immutable business record. Its digest is:
`c82b7ff32c1dbb58546f9b1c9c960ee52a7b94829ada06618c6868566921f150`.
This exactly equals the stored reservation and archive evidence digests.

The existing unused-reservation proof succeeds on native123: the issued
capability expired, was never claimed, has no attempt/terminal or journal/lease,
the mandatory intent is absent, and parent1416 bytes/revision are unchanged.
This validates a future revocation's conditions, not permission to perform one
now. Keep UNKNOWN41825241169, UNKNOWN45899587125 and UNKNOWN45965280576 and all
old reservations, revoked/issued rows and exact archives. Never retry the failed
run, use attempt2, reuse its capability or mark it successful after later GETs.

A new independently approved run uses the same existing workflow and writer.
The existing capsule decompression limit is 1MiB, while the new full record is
over 2MB because it retains exact predecessor evidence. The limit is unchanged.
Instead, the same signed/encrypted capsule carries a small fixed reference:

* Capsule request digest (existing `RECORD_SHA256`):
  `31bb31edc5d9cf90e6448b130f84bfc2119c6725ff8e35869b604b73d5754232`.
* Deterministically reconstructed new base record digest:
  `470ebe41d42e57eef06be0448e2f5511f1741042b349f1f6afd3cd6a8302ce3c`.
* Original business record digest, unchanged in every predecessor:
  `1558548ced53ca1689e5f076424b79c4fa88c95877bf35492eafb93a411ea7c5`.

The request binds exact target/state digests, failed run identity, predecessor
digest and reconstructed record digest. Hosted readonly GETs restore the full
record from the pinned native's existing archives. Both digests must match;
arbitrary inline records, altered bindings/amounts or selected older capabilities
are rejected. The existing HMAC capability context binds the resulting full
execution record, not merely a UUID or the small request. Save the small request
in the new archive so future fresh runners can reconstruct this path as well.

Actual-data offline sizing includes the three-UNKNOWN archive chain: the new
exact archive is 23,400,809 bytes; native envelope upper bound including reserves
is 47,376,428 bytes, below 67,108,864. The parent bound is 7,479 bytes, below
65,536. Capacity failure stops preparation without pruning or migrating history.

PR213's bank proof mechanism accepts the additional fixed base-record anchor,
retaining its original anchor for archived proofs. A later proven zero-write
bank no-op can still update only signed premises, with no new workflow or release.
The business contract and all original/predecessor records stay immutable.

## Execution plan after separate release and repair approval

Keep the bank schedule and all fences. Verify fixed candidate head/tree, normal
CI, main/validated, complete settings inventory, latest signed request/premises,
ACL, existing financial/protected snapshots, source integrity, HMAC continuity,
capacity, expiry and absence of competing writers. Use the existing shared lock.
Sign immediately before the one real dispatch; no trial/priming dispatch is used.

Expected no-additional-bank-transition path: native123 → new reservation124;
parent1416 → new pending1417; mandatory intent125; one existing writer append;
outcome126; exact acknowledged/sealed/journal/alias/archive proof → native127/C1;
parent1418 ready; independent final GET and fresh restore/guard verification.
Every save/readback boundary must remain fail-closed without additional writes.

The fixed append remains ordinary4,386 yen + transfer120 yen, two import rows /
4,506 yen only. Expense additions, existing-row/category changes and transfer
expense recognition are zero. C1 stays `2026-11-04T21:00:33+09:00`. Retain the
4,386-yen unposted-expense follow-up; this is not complete accounting processing.
Stop before catch-up Apply. Ambiguity/partial/unexplained change leaves the new
run UNKNOWN with no resend, rollback, row deletion or automatic new repair.

No new writer, database, infrastructure, workflow, schedule or original-source
operation is introduced. The existing 64MiB native / 64KiB parent / capsule
limits, three UNKNOWNs, STOP, permanent fences and A01–A15 remain in force.
