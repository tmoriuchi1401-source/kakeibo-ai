# PR-A: independent synthetic Sheets trial

Status: implementation and offline tests only. **実環境試験NO-GO / 本番NO-GO**.
No deployment, real Apps Script invocation, settings/Secrets change, Drive resource
creation, real dispatch, financial write or main merge is included.

## Scope and reuse

Base: main fa7ac9378ef51e4a51258dd6053afe52ed0a2bb8.
Selected source: review/receipt-sheets-authority-20261116 at
d0c13cc879c955b256c47a6130d0c0bbfb109d27 (46 commits / 71 changed files).
Only the Sheets-authoritative dependency closure is ported. Old Drive journal,
private adapter and receipt worker are excluded. The adopted one-tab UI is unchanged.

- Receiver: reread local enabled under the existing lock; exactly one additional
  GET checks RECEIPT_VALIDATION_ENABLED before the first latch/row write. Only
  named HTTP 200 value exactly true passes. OFF/unknown/malformed/failed responses
  mean no row, latch, UUID consumption, dispatch or retry.
- UI: OFF can leave I at register:UUID. No automatic uncheck, restoration, queue
  or resend. A register display is not an accepted request.
- Fixed binding: an offline-generated profile pins independent Script/case/source/
  UUID/digests. Existing config/schema and validators are reused. No new checksum
  Variable, strict editor proof, OIDC, signatures or per-transaction authentication.
- Workflow: only a synthetic Sheets job is added to the registered category workflow.
  REF/actual SHA/flag/attempt guards and pinned checkout precede worker credentials.
  Main category job body is unchanged; synthetic concurrency is separate with no
  production cancellation. All other workflows and schedules remain unchanged.
- Worker: only frozen snapshots; 受付済み -> 検証中 -> 検証済み・未記帳 (or reasoned
  修正が必要/保留). Read-back-confirmed logs contain UUID/digest/closed state only.
  Updates are management H:J only. No financial writer is connected.
- Configure stages only RECEIPT_P1_GITHUB_TOKEN, without reading/deleting
  CATEGORY_GITHUB_TOKEN. Existing Start/Stop bodies are retained. Stop deletes
  only the trial PAT copy; it preserves request rows and uncertainty latches.

PR-B owns real-source automatic intake, variable-item UI, multi-case capacity and
persistent duplicate history. PR-C owns writer connection and canary posting gates.
No new web server or Cloud infrastructure is introduced.

## Local verification

Run python -m pytest -q tests/test_receipt_p1a.py for the mandatory 23 groups.
The generated Apps Script bundle runs in a Node VM with fake Google/GitHub services,
not on Google. Fake Python HTTP/authentication links the actual worker and authority.
Network/financial-constructor deny counters must remain zero even if caught by a
broad UNKNOWN handler. Baseline hashes protect other workflows/main category body.
See receipt-p1a-offline-results.md for case counts and deferred tests.

The fixture contains only fabricated local identifiers, a new test UUID and an
aaaaaaaa... worker SHA, not deployable assets/configuration or real image data.
Neither the consumed earlier UUID nor any old row/run is used as a successful case.

## Future resources and bundle (separate approval)

1. New case/UUID/original identity/hash and fixed binding; preserve old I2/request/
   run/digests/stopped Script. Never make the consumed old UUID appear unused.
2. Independent empty Sheet/Script. Reuse verified unused synthetic-only resources,
   folder/SA/PAT where safe; create only approved missing resources. SA must have
   no financial-resource access. No existing binding/production Secret is overwritten.
3. Workflow active, final branch commit S, REF/SHA and synthetic Secret mapping
   must agree. Metadata alone cannot prove a Secret's binding; require documented
   insertion from the approved config/principal. Never retrieve Secret values.
4. Confirm owner/ACL/parent/source/hash and backend separation independently.
   These controls do not identify which human approved a spreadsheet edit.
5. Freeze branch/Variables/Secrets/ACL/registration/structural edits and other writers
   during the short household trial window. No claim of distributed CAS.

After code commit S and explicit artifact-preparation approval, from a clean checkout:
python scripts/build_receipt_sheets_bundle.py PRIVATE_EMPTY_OUTPUT --commit S --binding PRIVATE_DISABLED_BINDING_JSON --script-id INDEPENDENT_SCRIPT_ID

Keep private binding/output outside Git and synchronization, with restricted ACL.
The builder creates six local files, ZIP/hash manifest only. Missing/old profile
targets, duplicate functions and nonempty output fail closed. It never uploads,
authorizes, changes triggers or runs functions. Generate profile after S to avoid
self-referential commit hashes. Actual bundle/configuration is not prepared here.

## Bounded live sequence (not authorized by this implementation PR)

1. Both gates OFF. Verify deployed code/manifest, owner/Script/Sheet, header-only
   table, I=ready:newUUID, trigger count/source, PAT/binding, disabled audit and both
   latches absent. Verify original/ACL/parent, related history and unfinished runs.
2. Read workflow active, branch actual SHA=S, REF/SHA Variables and Secret insertion
   records. Any mismatch/unknown -> stop. These checks belong before ON, not in
   every edit callback.
3. Obtain explicit bounded ON, Start, audits, Stop and GitHub OFF approval first.
   Set GitHub acceptance true and independently GET; owner runs Start once and
   Enabled audit once. All identity/state/binding/PAT/latch conditions must agree.
4. Owner alone uses iPhone Sheets: open new synthetic row, inspect original, correct
   B=762 to 900 (A=600, total=1500), confirm categories/original; register once.
   No API cell edit to simulate ON_EDIT, double tap, re-edit, dispatch or rerun.
5. Independently match one row/UUID/snapshot/digest and one run/attempt=1/ref/SHA,
   confirmed transition logs and final G display. Verify result binding,
   posting_authority=false, financial_write_count=0, duplicates=0, source/ACL
   unchanged and inputs/notes retained. Fast polling alone need not capture
   検証中 if confirmed read-back transition evidence agrees with the final row.
6. After terminal/run completion, approved Stop once and disabled audit once;
   approved GitHub false change plus independent GET. Preserve I, all ten columns,
   digest, run and timestamps. Record each independent outcome.

## Stop, OFF and UNKNOWN

Stop requires STATE=DISABLED, STATE_MATCH/BINDING/OWNER/PROJECT/MODE/IDENTITY/READ
consistent with the profile. PAT_PRESENT=FAIL is expected after copy deletion.
AUDIT=FAIL may therefore be expected; never restore PAT for aggregate PASS.
Latches may remain STOPPED; record them without automatic clearing.

Stop failure/unknown is not retried. The preapproved GitHub OFF remains independent;
false does not cancel a running worker or prove local Stop. Preserve actual states
and request specific recovery approval. No automatic repair, Configure, rollback,
row deletion, run cancellation, retry or resend.

A definite OFF rejection is unconsumed only when latch absent, PUT attempts=0,
dispatch=0 and related row/history absent are independently confirmed. Returning
I from register to ready is a separate approved owner operation, never automatic.
It is forbidden for a consumed or uncertain request. Negative OFF tests are offline;
the real normal trial gets exactly one registration.

Any skipped run, identity/binding mismatch, lost save/dispatch/read-back response
or UNKNOWN stops progression and forbids same-UUID resend. Later independent reads
may establish the committed result without repeating a mutation. A later trial
uses a new case/original/UUID/binding with approval; no resume feature is added.

P1 GO remains unmeasured until approved new one-case iPhone completion, zero resend/
duplicate/financial write, both gates OFF and safe Stop are observed. Main merge,
deployment, settings, source movement and financial write each need separate approval.
**実環境試験NO-GO / 本番NO-GO**.
