# Bound recovery: bank53 / parent1300

Production release requires user authorization, reviewed main/validated SHA,
successful CI and a production dry-run. Passing synthetic tests is not approval.
Strict execution remains the default. A signed, explicitly approved nonexclusive
execution policy is available only for this fixed run/attempt and state pair.

## Fixed evidence and operation

Only failed run `40961220838`, run attempt 1, failed main
`6f114abac3d8c3a537febeeea9fdfc07ff9716d9` is accepted. A new private record is
pinned by SHA256 in `app.bank_attempt_recovery`; it is not a configurable run or
generation input. The record is retained in the operator's private audit folder,
not this repository. It binds separate bank/parent attempts, both immutable file
bindings, exact before/after payload hashes, all 497 identities, duplicate50,
new4, withheld6, all four financial tables, five PDF contents/metadata/parser
results, SQLite bytes/32 histories and the old checkpoint. Historical financial
and PDF request outcomes remain `UNKNOWN`; the entry never replays them.

`dry_run` (default) validates every prerequisite and performs no write. `release`
accepts only the full initial pair 53/pending and 1300/bank pending. It invokes
the existing native release method, verifies bank54 through a new read-only
transport, checks the unchanged parent, invokes the existing parent release,
then independently verifies parent1301 and the unchanged bank. Every proposed
payload is constrained by existing `ReleaseOnlyTransport` to one exact hash and
one write attempt. Bank files/checkpoint and all other parent fields are retained.

Exceptions stop immediately. A lost response is not recovered by write retry,
rollback, or a subsequent operation. On a later invocation, exact54/1301 is a
verified no-write result; exact54/1300 is inspection-only and release refuses to
continue. Any different generation/attempt/hash fails. A partial recovery needs
separate operator reconciliation and authorization; this entry has no resume or
parent-only bypass. No source runner, finance writer, PDF mutation or checkpoint
writer is part of this workflow.

## Concurrency and automatic apply

Existing native and parent CAS compare raw bytes, send an unconditional Drive
update, then compare the read-back. The interval from the last GET to PATCH is
**not atomic**. Neither an additional GET nor `ReleaseOnlyTransport` fixes that.
The two files also are not a single atomic transaction.

The default `execution_policy=strict` requires *all* of the following for the entire operation:

* Owner actor/triggering actor, workflow_dispatch on reviewed main=validated,
  attempt1, no debug; shared `kakeibo-production`, cancel=false, queue=max.
* Every OTHER checked-out workflow referencing the service-account, Gmail or
  backup credentials is `disabled_manually` in GitHub. This includes the normal
  production workflow, manual entries, scheduled backup/cleanup and legacy
  recovery entries. Merely setting schedule=false is insufficient. The entry
  reads the full bounded workflow inventory and rejects missing/active entries.
* No other running/queued/waiting/pending/requested Actions run, checked at each
  boundary; schedule=false, legacy-disabled=true, category runner=false.
* A signed, <=15-minute, exact-main/operation-nonce/mode/record-bound attestation:
  external writers frozen, known writer count0, all named local Tasks disabled,
  configuration unchanged, no new dispatch/re-enable until explicit resume.
  Cloud's existing endpoint must independently return503.

The signed freeze is an operational exclusion assumption, not remote proof that
no credential holder can violate it. A noncooperating API writer can still race
an unconditional Drive PATCH. Synthetic tests demonstrate this limit; production
is NO-GO in strict mode if the exclusive maintenance interval cannot actually be established.
No speculative ETag support or new distributed lock is claimed or introduced.

### Explicit nonexclusive execution (this run/attempt only)

`execution_policy=nonexclusive` retains the fixed evidence, all raw hashes,
run/attempt/generations, owner/main/validated/nonce/freshness checks, common
Actions lock, Cloud503 and existing legacy stops. At every boundary it still
rejects any other active/queued/waiting/pending/requested Actions run. It requires
`KAKEIBO_SCHEDULE_ENABLED=false`, `CATEGORY_BACKFILL_SHEET_RUNNER_ENABLED=false`
and `KAKEIBO_LEGACY_DISABLED=true`. It does not require disabling all 22 workflows,
enumerating external writers or proving an external freeze/local Task stop.

The signed attestation must add `execution_policy=nonexclusive`,
`nonexclusive_authorized=true`, `target_run_id=40961220838`,
`target_run_attempt=1`, `normal_apply_resume_authorized=false`, and
`residual_risk=non_atomic_CAS_external_writer_race_accepted`. Its execution policy
must match the workflow input. Omit `writers`, `external_writers_frozen` and
`no_dispatch_until_explicit_resume`; an assertion `external_writers_frozen=true`
is rejected in this mode. These fields document user approval and the accepted
residual race, not proof of exclusivity. A writer can still enter between the last
GET and unconditional PATCH, and the two updates are not atomic together.

All integrity and one-attempt write controls are identical in both policies.
Any observed conflict or mismatch stops immediately. After a partial release,
neither automatic continuation nor another release is permitted. Lost responses
must be independently inspected without resend or rollback. Normal apply and the
old first window remain unapproved and must not be started after recovery.

Ready state does not dispatch anything here. Normal apply is disabled externally
before release, remains disabled afterwards, and there is no workflow_run,
repository_dispatch, schedule, reusable-workflow or accounting invocation in
this entry. Re-enabling any writer or running the old first window requires a
separate instruction. The recovery does not restore workflow/flag settings.

## Future operator procedure (not executed)

1. Review/merge only after separate approval and successful CI. Separately align
   reviewed main/validated. Keep the original bank53/parent1300 record unchanged.
2. Select the explicitly authorized execution policy. For strict mode, establish
   the maintenance exclusion above. Record
   the disabled workflow inventory, stopped Cloud/local writers and dispatch
   freeze. For nonexclusive mode, retain the stop flags and check competing runs;
   use the risk-acceptance fields above without a freeze claim. Do not use old
   capsules or assert UNKNOWN requests were never sent.
3. Seal the fixed private record and a fresh signed attestation with the existing
   SA RSA/AES capsule format. Attestation keys are `at_epoch`, `record_sha256`,
   `main_sha`, `operation_id` (fresh 32-hex nonce), `mode`, `release_authorized`,
   `external_writers_frozen`, `no_dispatch_until_explicit_resume`,
   `configuration_unchanged`, and `writers` (`KnownWriterCount`, nonempty `Tasks`
   with `State=Disabled`). The signature is RSA-PSS/SHA256 over canonical
   `encode(attestation)`; capsule fields are `record` (base64 canonical record),
   `attestation`, `signature`, encrypted with the existing `LABEL`, RSA-OAEP
   SHA256 and AES-GCM. No plaintext IDs/financial rows enter inputs/logs.
4. Run only this main workflow in dry_run mode first, with the signed operation
   nonce as `operation_id`. GitHub run-name binds that nonce to the assigned run
   ID. The entry requires exactly one dispatch with that name and verifies the
   actual run ID/path/head/actor/event/attempt through GET. A reused nonce or a
   rerun is rejected. A release requires a new nonce and fresh signed capsule.
5. After the selected policy controls, dry-run and synthetic/CI review succeed,
   use explicit user release authorization (which may already cover this sequence).
   Confirm `RELEASE_40961220838`, then A prerequisites
   -> B bank54 -> C independent GET -> D parent1301 -> E independent GET, followed
   by an independent financial/PDF/SQLite/checkpoint observation and stop. If any
   boundary fails, keep the partial/unknown evidence and do not rerun release.

Synthetic fixtures and local fixed-evidence dry-run validation do not constitute
production approval. No current release capsule or production authorization is
generated as part of this Draft PR.

## Validation of this draft

Local targeted suite: **318 passed**, including 57 dedicated cases, existing
pending/partial recovery, native state, parent ledger, production workflows,
bank recurring/PDF source and production-flow tests. Tests block live network.
The dedicated cases cover dry-run, ordered success, each pre/post bank/parent
boundary, lost responses, wrong generation/attempt/run, fixed proof tampering,
duplicate/partial re-entry, changed financial/PDF/SQLite/checkpoint content,
exclusion gates, and the residual noncooperating-writer race. The latter is a
negative proof of the existing CAS limitation, not a race-safety pass.

A local controller dry-run against the pinned saved record also passed with
zero write attempts. It did not exercise live exclusion controls, re-download
PDFs, dispatch Actions or invoke production recovery. The existing production
and UI files were unchanged. Strict workflow disablement/external freeze was
not performed in that validation. Nonexclusive execution does not represent
those conditions as established.
