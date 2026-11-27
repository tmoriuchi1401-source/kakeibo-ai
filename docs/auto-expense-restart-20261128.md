# Fixed auto_expense reconciliation and card catch-up candidate

Development candidate only. Main/validated remain
`495e82205949e2d634d195823faacc5bdc620cb1`. No real archive save, pending
release, dispatch, accounting write or checkpoint change occurred in this phase.
[A01–A15](architecture-contract.md), bank schedule, STOP and permanent fences
remain mandatory. The three old card UNKNOWNs are not reopened or rewritten.

## Existing mechanisms and fixed boundary

The manual `kakeibo-production.yml` / `card_expenses` scope accepts an optional
`card_expense_reconciliation` capsule, mutually exclusive with the financial
request. Normal core/card schedules do not select it. It invokes the existing
`ProductionLedger.release_after_reconciliation('auto_expense', ...)` API.
No new writer, database, workflow, infrastructure, key or Secret is introduced.

Only the reviewed private record with digest
`815c1fa78645fe32ec74474fdc14427dbeb68f236ccdc8211ca0b2a60c8e97b0`
is accepted. It binds failed run46013990252/attempt1, the old parent exact bytes
and revision, its pending attempt, native127, C1, financial/ACL/protected-document
and configuration hashes. The direct runtime exception remains unconfirmed.

The existing RSA/AES/PSS capsule decoder and existing SA key are reused with a
distinct state-only label/scope. Owner approval, code tree, main SHA, effective
control snapshot, operation (`preview` or `release`), observation time and expiry
are signed. This operation has a maximum 900-second lifetime; existing financial
capsules retain their 300-second default. Guards recheck expiry/main/run and
exclusion before effects. Sign only after all preparation; never test dispatch.

The common `kakeibo-production` lock, hosted/main/validated/owner/attempt1 checks
and existing fences are required. A fixed, non-secret operator run title and
independent Actions history enforce one release dispatch per reviewed head, even
if a previous invocation stopped before any native save. Readonly previews and
ordinary financial stages do not consume that release fence. No arbitrary source, parent JSON or record is
accepted. Bank-only noop premise updates reuse the existing independent revision,
Actions, completion and code-contract verifier. Current and candidate shared bank
entry contracts are reviewed; bank accounting behavior and schedules are unchanged.
Unproved history, actual bank posting or another source change fails closed.

## Compact durable evidence before release

Before touching the parent, persist one small append-only recurring summary in
the existing protected Google card native state, through `DurableState` and
`SqliteRecurringRunState.record(..., advance_checkpoint=False)`. It stores:

* The old failed parent exact bytes and revision plus the reconciled current parent.
* Failed run identity and the new operator run identity, recorded separately.
* Financial/ACL/document/rule hashes, fixed record digest and C1.
* Explicit `financial_write_reached=False` and unconfirmed exception identity.

No native envelope is embedded into the archive; maximum archive payload is
32 KiB. Existing full UNKNOWN archives are kept exactly. This avoids recursive
archive growth and does not depend on Drive revision retention/keepForever.
The business checkpoint stays at C1; actual archive time and SQLite recorded time
are execution times, not coverage advancement.

After durable save, an independent readonly GET must recover the exact archive.
Only then may the parent API run, wrapped by the existing one-write
`ReleaseOnlyTransport` with the exact proposed after digest. At the original
snapshot this means native127→129 for archive reservation/completion and parent
1420→1421 for release; proven bank noops can legitimately advance the parent first.
Only auto_expense phase/attempt/error and the regular parent generation change.
last_success, counts, duration, all other sources, C1 and financial values remain.

Any archive/save/readback/parent response ambiguity stops without resend,
rollback, deletion or automatic completion. An existing archive or unresolved
native reservation prevents automatic re-entry. Operator preview constructs no
writer and saves nothing. The failed Actions run remains failure/attempt1.

## Reject mismatched contracts before parent pending

For a selected financial request, the existing parent runner first invokes the
actual readonly child inside the shared lock, before `ledger.begin`. A rejection
does not call begin/fail and cannot leave a new parent pending attempt.
The original child check after parent reservation and its pre-financial check
remain. The planning profile is rechecked after native reservation before finance.

The encrypted selected request now contains `imports`, `plan_sha256` and
`profile_sha256`. The profile binds the actual rule flag/money mode plus literal
category-master and rule-table hashes. A flag change with no classification effect
still rejects. The same existing planner binds IDs, categories, money and reasons.
Old two-field requests are not accepted for the new selected maintenance path.
Routine core authority remains autonomous and does not acquire per-run approval.

The existing child/parent diagnostic path propagates allowlisted fixed codes,
stage and exception class/site only. `card_expense_approved_plan_changed` and
`card_expense_profile_changed` survive the child boundary; arbitrary exception
text, credentials and financial rows are never logged. Successful readonly
selected previews expose aggregate counts and safe contract/profile digests.

## Current-checkpoint readonly window previews

The old C0 preview remains bounded and retains its old one-dispatch fence.
Two approved readonly C1/C2 windows are additionally accepted, each bound to its
expected checkpoint, checkpoint minus two days, cutoff and exact source contract:

| Checkpoint → cutoff (JST) | Imports | Fixed source contract |
|---|---:|---|
| 11/04 21:00:33 → 11/16 21:00:33 | 34 / ¥105,315 | `dc6d0cdddfd2f565c579f6fea565857f6d7884987aa698e5ae57f162dc21446e` |
| 11/16 21:00:33 → 11/26 21:43:48 | 34 / ¥229,255 | `17a5f6957d4870e897bd9e197f00ae1baf9c1e1c690dba0b64a5e3609f200178` |

Reuse Apply's side-effect-free `prepare` and `plan_contract_sha256`, the actual
hosted authority/key/configuration, existing classification, master and alias
registry. Readonly Google/Gmail services and the readonly projection wrapper
remain mandatory. Native/parent/financial/money observations must match before
and after, and rules/master/profile must remain stable. No financial, state or
review write is available. The existing Amazon native binding is read solely to
prove its parent completion; no Amazon runner is executed.

## Reused expense contracts and future batch order

Actual configuration remains rule-auto-apply=true and money=confirmed-v1.
The already regenerated IDs/amounts/categories/reasons are reused, without another
classification investigation. Three history-backed classifications and 34
explicitly approved provisional その他/未分類 purchases remain listed privately.

| Selected stage | Expense count / yen | Business contract |
|---|---:|---|
| Existing ¥4,386 + ¥145, 食費/食料品 | 2 / ¥4,531 | `bdc880d5be8eae6fd67b54252219301eefee90f0853b95dbd8df9d62e026f2df` |
| First-window ordinary | 18 / ¥64,116 | `ce49c1f165a237b3fadae9208948aa21a4e49acc8e73b740658f319810e9ed83` |
| Second-window ordinary | 22 / ¥179,335 | `3590ac5b1c57c2138e2e0c61c54e20e5ae2d884a1d5118e8be7167e0a3071373` |

After separate fixed-head/tree production approval:

1. Release only this candidate, verify the exact approved main tree and new-main
   three-job CI, then promote only validated. All other settings/Secrets stay.
2. Fresh readonly controls, authority, expiry, evidence, capacity and shared-lock
   checks; sign the state-only capsule immediately before its sole release run.
   Its own pre-effect validation is readonly. Save/readback the compact evidence,
   release the parent, verify fresh native/parent ready and preserved UNKNOWNs.
3. Existing selected expense Preview for the two purchases; compare both business
   and profile digests. Only then independently execute the two-expense contract.
4. Fixed first-window readonly Preview, then normal incremental Apply; verify
   C2=`2026-11-16T21:00:33+09:00`, ready and exact readback. Hold four money cases.
5. From actual successful C2, generate and verify the second-window fresh plan
   and readonly Preview. Only matching scope proceeds to C3=
   `2026-11-26T21:43:48+09:00`; hold two more money cases.
6. From actual imports, selected Preview for 18 and 22 ordinary expenses with the
   reused categories and fresh profile. Matching business/profile contracts alone
   may execute, each followed by exact financial/J/alias readback and ready.
7. Independently verify imports +68/¥334,570, expenses +42/¥247,982, J42 cells,
   transfer expenses0, existing expense edits0 and duplicates0. Preserve six
   explicit money holds/¥10,902 with zero posting; all old evidence stays exact.
   Verify alias successor proofs, stable IDs, fresh restore and subsequent guard.

Do not apply latest data after C3, mass-post the 501 legacy candidates, deploy
receipt, send AI, operate originals, change schedules or release permanent fences.
Ledger aggregate changes and derived display refresh remain separately verified.
Stop before approval to restart normal scheduled operation. This phase is not
proof of production completion or whole-accounting reconciliation.
