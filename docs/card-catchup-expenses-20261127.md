# Card catch-up and expense posting candidate

Development only. Main/validated remain
`dd46a2898a8d4cc57930f2eb38bb7fb23279dea1`. The limited repair and C1 are
complete. No old UNKNOWN repair work, recovery dispatch or archive migration is
part of this candidate. [A01–A15](architecture-contract.md) remain mandatory.

## Actual baseline and fixed windows

Readonly GET at 2026-11-27T12:03:39Z matched actual C1, native127, parent1418,
all financial values/formulas and the independently verified repair snapshot.
Fresh protected money/review documents also matched their saved evidence.
Existing captured Gmail inputs were reused; authentication, originals
reconciliation and the completed isolated E2E were not repeated.

All times below are JST and ends are exclusive. The second row is a projection
after successful window1, not permission to use stale C1 state for window2.

| Window | Start → end | New imports | Ordinary expense candidates | Transfers | Existing duplicates | Resend source rows | Money reviews |
|---|---|---:|---:|---:|---:|---:|---:|
| 1 | 11/02 21:00:33 → 11/16 21:00:33 | 34 / ¥105,315 | 18 / ¥64,116 | 16 / ¥41,199 | 5 | 39 | 4 / ¥5,278 |
| 2 after C2 | 11/14 21:00:33 → 11/26 21:43:48 | 34 / ¥229,255 | 22 / ¥179,335 | 12 / ¥49,920 | 10 | 44 | 2 / ¥5,624 |

Totals: 68 import rows / ¥334,570, including 40 ordinary / ¥243,451 and
28 transfers / ¥91,119. Window2's detached 44-row plan contains ten candidates
already owned after C2; it must not be applied unchanged. Alias holds and
ordinary classification reviews are zero for this captured input. The six
money IDs are distinct, absent from the current book, and remain explicit
review records (¥10,902 total), with zero money financial posting. Do not hide
them or call their accounting complete when coverage advances.

The projected forty expense categories are two 医療・保険 / 医療保険,
one 水道・光熱 / 電気, and 37 その他 / 未分類 (15 in window1, 22 in window2).
The 37 are explicit unresolved category proposals, not confirmed classifications
or silently excluded purchases. Before their financial execution, approval must
either explicitly retain that existing fallback or fix categories and rederive
the selected expense contracts. Ordinary transaction status alone does not prove
category completion or unattended-operation readiness.

Maintenance purchase/duplicate/alias/review contract hashes:

* Window1: `dc6d0cdddfd2f565c579f6fea565857f6d7884987aa698e5ae57f162dc21446e`.
* Projected window2: `17a5f6957d4870e897bd9e197f00ae1baf9c1e1c690dba0b64a5e3609f200178`.

Projected selected-expense contracts, including those category proposals:
window1 `0f0e2385d414a47d10804dff0287c72cf150adea14646cc50c11707dafdf40dc`;
window2 `0b1b7f0d68b951c2d9a81b6b12a5f0362b421f324a7907756165243fec3c0206`.

The operator must recompute these from the latest candidate implementation;
the private plan artifacts hold the complete exact inputs and targets.

## Ordinary incremental execution

Reuse `aupay-card-incremental-apply.yml`, its normal entry, existing shared
writer, recurring authority/capability/journal and `kakeibo-production` lock.
An optional historical cutoff uses `build_explicit_preview_window` against
real now, checkpoint minus two days and the unchanged 14-day maximum. No start
override, future end, truncated period or recovery-entry reuse is accepted.

A manual historical cutoff requires its reviewed `expected_plan_sha256`.
The runner independently recollects/replans and checks the full purchase,
source hash, disposition, status and review contract before any review save,
reservation or finance. Expected values do not follow newly observed data.
For window2, read actual C2/ready/ledger/aliases afresh and rederive the plan.
If the approved contract does not match, stop before Apply.

Normal completion now stores an append-only coverage certificate linking the
actual Actions identity, business coverage, sealed capability, manifest,
acknowledged/final journal and alias delta. The daily guard can thus prove a
historical window's success even when execution occurred after its cutoff.
Precredential deferral alone grants no effect: protected native proof and
actual Actions success/attempt1 are mandatory at runtime. Unproved success,
UNKNOWN/failure, altered source/manifest/journal, pending or missing proof deny.

Scheduled runs use their latest Google checkpoint and real now. Manual cutoff
and approval-hash inputs are forbidden for schedule events. No routine
per-run owner approval or new schedule is introduced.

## The two already imported ordinary transactions

The existing twelve-owner set contains two ordinary rows and ten transfers.
Both ordinary rows lack stable expense/J links and have no same-date/same-amount
active expense in the saved ledger. The old ten-owner subset therefore contains
an additional ¥145 expense candidate; fixing only ¥4,386 is insufficient.

| Separate maintenance operation | Expense rows | J cells | Existing category proposed for approval | Active expense sum delta |
|---|---:|---:|---|---:|
| Repaired ordinary purchase | 1 | 1 | 食費 / 食料品 | +¥4,386 |
| Existing-ten ordinary purchase | 1 | 1 | 食費 / 食料品 | +¥145 |

The ¥4,386 merchant has 24 historical expenses in 食費 / 食料品; the ¥145
merchant has mixed historical categories. These are explicit category proposals
for the maintenance approval, not a new global classification rule. No existing
expense/category is changed. The ¥120 transfer and all other transfers generate
zero expense rows. Stable expense IDs are the existing `expense_id(import_id)`.

Expense contract hashes:
¥4,386 `669f77d89411adc26a63fa53b0bf4ed0be256cda66aa0b819ccefdd39407d827`;
¥145 `847edda87977ebb955b293173b377020fc743e9c083bd88c763986a257ef322f`.

## Existing expense writer and legitimate owner changes

Reuse AutoExpensePipeline's decisions and row materialization. The selected
mode classifies only the named ordinary owners in memory. It does not reset
historical statuses, select transfers, clear registries or post arbitrary rows.
Existing receipts, stable IDs, original-source links, ambiguous expenses and
category-master membership are checked before writing. Already exact posted
selections keep the same contract and create zero additional expenses.

The existing SheetsDB RAW append preserves source/date values and prevents
formula interpretation. The existing RAW range writer changes J alone; full
USER_ENTERED row updates would reinterpret historical dates/timestamps.
No new financial writer is added. One expense append plus one J-cell write
per selected owner is permitted; no write retry, rollback or deletion exists.

Before finance, the existing parent auto_expense stage is pending and card
native is durably pending. Full financial values/formulas are independently
read back, accepting only the exact new expense rows and those J cells.
An append-only owner-change certificate retains the full old row, exact new
row, stable expense row and contract hash. Existing aliases/source hashes and
business fingerprint remain unchanged. Fresh normal guards also require the
actual stable expense to match the saved certificate. Old owner proofs and
UNKNOWN checkpoint certificates remain immutable.

Initial readback checks every new expense field exactly. Later alias guards
bind the expense's economic identity; legitimate category/user-note edits in
the existing UI do not reset aliases or cause another posting. Numeric Sheets
dates and omitted legacy M status cells are included in duplicate checks using
the existing ledger date/amount parsers, without rewriting those old rows.

The Google card native stores this delta without advancing its checkpoint.
The parent completes only the auto_expense stage; card coverage counts still
refer to the actual latest coverage result. A lost response, partial J update,
runner loss or mismatch leaves pending/UNKNOWN and denies further writes.

Use the existing `kakeibo-production.yml` manual `card_expenses` scope for fixed
maintenance. It selects only auto_expense, requires all other sources ready,
and skips global sorting and unrelated accounting writers. The request contains
RSA-wrapped owner IDs using the existing SA public key, existing categories and
the reviewed contract hash. No plaintext owner ID is exported through workflow
env, new Secret/key/store is required, or PAT forwarded to Google.

Ordinary core runs retain their existing hosted authority and can automatically
post only registered ordinary card owners without J links. Unregistered legacy
auto_expense rows do not acquire authority from this release. The preliminary
global scan found 503 rows with neither J nor their stable ID, including the two
above; 501 are outside the registered set. This is a reconciliation inventory,
not proof that all 501 need new expenses. Do not mass-post them or claim global
accounting completion until their existing receipt/expense correspondence and
exception handling are resolved.

## Approval order and return-to-operation conditions

1. Approve the fixed candidate head/tree and release only this diff. Check
   current base, normal CI, no competing writer; merge, verify exact tree and
   new-main CI before promoting only validated SHA. Bank settings/schedule stay.
   Before any incremental Apply dispatch, verify the hosted protected recurring
   authority period/target/bounds and HMAC through the existing readonly path.
   Its current Secret value cannot be read by GitHub metadata APIs and was not
   extracted in this phase. Do not consume a financial checkpoint to discover
   an expired authority. This is a remaining pre-execution gate, not a GO claim.
2. Execute the separately listed ¥4,386 and, only if included in approval, ¥145
   expense contracts. Verify exact expenses, J links, owner certificates and
   source aggregate changes. C1 remains unchanged.
3. Normal incremental window1: expected checkpoint C1, cutoff C2
   `2026-11-16T21:00:33+09:00`, reviewed contract, 34 import rows and four explicit
   money reviews. Verify actual C2/native/parent ready and complete evidence.
4. Freshly revalidate window2 from actual C2. Only matching reviewed scope may
   proceed to C3 `2026-11-26T21:43:48+09:00`, 34 further imports and two reviews.
   A changed/ambiguous plan stops; never reuse the detached 44-row plan.
5. Freshly derive the forty selected ordinary expense plans (18+22), stable
   IDs, existing categories and contracts, excluding every transfer. The
   captured projection is forty expenses / +¥243,451, with forty J links.
   With the two existing purchases, the proposed expense total is 42 rows /
   +¥247,982. Each exact execution must match its approved business contract.
6. Confirm native/parent ready, latest coverage, all 42 expense/source links,
   no duplicate stable IDs or aliases, preserved UNKNOWNs and fresh restore.
   Run existing derived expense/month projection only with its reviewed scope;
   source aggregate changes are not proof that a cached display was refreshed.
7. The remaining time from C3 to actual restart must be covered sequentially by
   the normal bounded planner; do not jump the checkpoint to now. Resolve or
   explicitly retain all money/legacy exceptions. Recheck actual daily guard,
   capacity, protected authority period, HMAC continuity and hosted-only path.
   An approval may list normal card/core authority and schedule activation
   explicitly, separately from maintenance scope, after these gates. No setting
   is changed now; maintenance approval alone does not replace routine authority.

Until those conditions hold, return-to-operation is not a production GO and
this is not a claim of whole-accounting completion. Existing STOP, all three
UNKNOWNs, no-replay and permanent fences remain in force.

## Receipt remains independent

The owner reported 「表示なし」 in the existing trigger detail. Trigger source
ID is still unverified; container equality is not substituted. Keep the prior
four stages: disabled configuration, fixed Script/readonly RPC, trigger-guarded
limited AI/plan, separately approved native Apply. No stage runs here. Reuse the
existing 7,179-byte Code backup and its prior evidence; do not save/discard or
claim a fresh editor read. No receipt deployment, new E2E or Gmail auth occurs.
