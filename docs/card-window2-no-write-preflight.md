# Second-window no-write audit and shared readonly preflight

Development candidate, not production execution approval. Main/validated stay
`a92abe6aa4a44d1fb1ceadc52ce2c8caa82b905b`. Native134, parent1428 ready and C2
`2026-11-16T21:00:33+09:00` are not saved or changed by this candidate.
[A01–A15](architecture-contract.md), the bank schedule, the three old UNKNOWNs,
STOP and permanent fences remain in force.

## Fixed audit, not a successful run

Failed incremental Apply **46103398237**, attempt1, job **140850673239**, ran
at the main SHA above. Its dependency/early-guard steps succeeded and its Apply
step failed. The direct exception was not recovered; the existing public code
was `incremental_apply_stopped_no_retry`.

The bank schedule run **46103439725** at the same SHA completed with zero write
counts. Available exact Drive revisions match parent1426, bank pending1427,
bank ready1428. Only the bank source and parent generation changed. Native134
bytes/metadata, financial values/formulas, J/N, money/reviews, projection journal,
protected documents and ACL were independently unchanged. The existing Apply
requires confirmed parent/native pending saves before review or financial
dispatch. There is no rollback/ready reset on this failure path. Together,
these historical revisions and independent snapshots establish writer
nonarrival; a matching present-day snapshot alone would not establish it.

Audit anchors (SHA-256; original private evidence is retained separately):

| Evidence | SHA-256 |
|---|---|
| Final limited-audit artifact | `534b81bc4fc9b84f6ceca6040a5bfc252b69d02b7c816dfdbd30c71ae7cbee71` |
| Native134 exact bytes | `9d8b6aafde553f45d1e659315c45774c255d726f3203c43d219a16507c30b621` |
| Parent1428 exact bytes | `b87f01c0c102c101a47a217cbba17462bb310276d27e9952c8effbe97c5493e2` |
| Native revision audit | `c4b1d438d0869738522f432f886df36000ca277ffd1bfb72264d7d836a1d9c2f` |
| Parent revision audit | `70baf0e7d83be183473c47751a8fb2f484c148f2b60086b94f3bf7e5937dae9a` |
| Non-bank source elements | `283473eb6cac7a0661b3a38ca020860981e88e4ac074ecac7656664692d9c15f` |
| Whole financial/control tables, both renderings | `07ad4831d3b5067b782a94cc91bd5061f69defbd2c35fd09da5036b5c6efbecf` |
| Five protected money/review/journal documents | `f57a5c50ecac90bfa0258348f88b3eb16d3936c1b5d1349e30d32853fb7f3c30` |
| Preserved first-window coverage summary | `8ab018e21bf319cad55409f5b06e2f33fbd96502fbcfff201a04182506e8bead` |

Only this fixed failed identity is added to the existing no-write verifier.
The old 41729101706 exemption and all UNKNOWN/archive proofs remain required.
The failed run's conclusion remains failure; its dispatch/attempt/capability
cannot be reused. No caller can supply a new exempt run or replace a digest.
The audit is reviewed code evidence; it does not create a new Google archive.

At C2, preflight additionally freezes the native and non-bank sources and
independently reads the complete financial/control tables and five protected
documents. Bank readiness and zero write counts are mandatory. Parent generation
is not pinned to an execution clock, so normal zero-write bank bookkeeping does
not alone invalidate the historical no-write proof. This does not certify
arbitrary new bank transitions or excuse an unexplained financial change.

After C2 advances, preserve the exact first-window anchor and prove an independent
completed successor covering from C2 with the existing sealed capability,
manifest, acknowledged/final journal and alias certificate. Its Actions creation
time must be after the failed run. Do not compare the historical business cutoff
with the later repair/catch-up execution time or freeze pre-C3 financial values.

## Preview and Apply share validation

The existing Apply history loop is extracted into `history_controls()`.
Apply retains its existing actor, workflow, main/validated, attempt1, current-run
identity and all active-status checks, then calls that loop. The existing
catch-up Preview keeps its own Preview identity and readonly scope and calls
the same loop with **no fabricated Apply identity and no deferral**. Both use
`verify_no_write_state()` and the existing `plan_contract_sha256()`.

The Preview now checks actual execution history, protected UNKNOWN/coverage
proofs, the fixed no-write audit and the approved window contract before it can
report success. Failure stops before planning/state reservation/financial write.
Its existing independent before/after snapshots remain mandatory.

Preview success is not a lock or a future execution permit. Authority expiry,
live main/state, another queued/running workflow and other time-dependent
conditions must still pass fresh inside Apply under `kakeibo-production`.
Queued-bank `competing_run` is a reproduced cause candidate, not a recovered
direct exception. Do not relax this guard or stop the bank schedule.

## Later execution, subject to separate approval

After an approved release/normal CI/validated promotion, wait for the bank run
to finish and choose a noncompeting interval. Fresh state/authority/lock checks
and a successful hosted readonly Preview must precede any new independent Apply.
Never redispatch the failed run or use Apply to test preflight.

Keep the approved contracts: window2 **34 imports / ¥229,255**, then **40 expenses /
¥243,451** (18 / ¥64,116 and 22 / ¥179,335). C3 remains
`2026-11-26T21:43:48+09:00`. The already posted two expenses and completed first
window are not repeated. Four money cases / ¥5,278 remain held; the other two /
¥5,624 remain unposted. No new writer, workflow, DB, infrastructure, state save,
archive, category investigation or recovery engine is introduced.
