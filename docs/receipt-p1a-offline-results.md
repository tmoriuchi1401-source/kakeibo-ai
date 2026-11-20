# PR-A offline synthetic results

2026-11-20 JST. Fake services/credentials only. **実環境試験NO-GO / 本番NO-GO**.
No real Apps Script invocation/deployment, live dispatch, settings/Secrets change, resource
creation, ledger write or main merge. Local tests are not real-device evidence.

## Mandatory 23 groups

Local Python 3.14 / Node 22: all 105 parametrized cases passed.
Full local regression: 4,961 passed. Existing Node checks: 13 passed.
Targeted mandatory/workflow checks: 113 passed; compileall and diff whitespace: PASS.
CI uses the existing Python 3.12 synthetic workflow; final status is in the Draft PR.
Initial CI collection lacked httpx after the GenAI SDK switched its transitive
dependency to httpx2. Pin the already locally verified httpx 0.28.1 in test-only
requirements; production requirements and error-handling code are unchanged.

| Group | Cases | Result |
|---|---:|---|
| T01 | 5 | PASS |
| T02 | 3 | PASS |
| T03 | 5 | PASS |
| T04 | 10 | PASS |
| T05 | 8 | PASS |
| T07 | 1 | PASS |
| T08 | 12 | PASS |
| T09 | 1 | PASS |
| T10 | 1 | PASS |
| T12 | 5 | PASS |
| T13 | 4 | PASS |
| T14 | 4 | PASS |
| T16 | 6 | PASS |
| T17 | 3 | PASS |
| T18 | 3 | PASS |
| T19 | 3 | PASS |
| T20 | 1 | PASS |
| T21 | 5 | PASS |
| T22 | 10 | PASS |
| T23 | 1 | PASS |
| T24 | 6 | PASS |
| T25 | 1 | PASS |
| T26 | 7 | PASS |

Network/financial-constructor deny counters remain zero even when exceptions are caught.
Only fake management writes and permitted fake dispatch attempts occur. Unknown responses
do not cause retries/repair. Confirmed transitions match the fixed row and digest.

## Deferred 3 groups

| Group | Deferred scope | Mandatory coverage retained |
|---|---|---|
| T06 | Detailed stop concurrency stress | Lock-after-acquire enabled reread and Stop failure states: T24 |
| T11 | Generic duplicate source/case across multiple UUIDs | Foreign UUID: T08; replay: T09/T10/T21 |
| T15 | Multi-case sorting/structural edit matrix | Case/source/machine: T08/T12; frozen input only: T25 |

P1 is one fixed case with structural edits/other writers excluded during the trial.
Existing safety guards remain. Multi-case generalization is PR-B. No extra identity/authentication
or infrastructure development is needed. Deferred tests are not counted as mandatory PASS.

## Remaining execution gates

Current deploy/config/ACL/Secrets/REF/SHA/trigger and real device/Stop results are unverified.
Base main: fa7ac9378ef51e4a51258dd6053afe52ed0a2bb8; selected source:
d0c13cc879c955b256c47a6130d0c0bbfb109d27. Actual assets, settings and runtime need
independent checks and explicit approval. Follow receipt-p1a-synthetic-runbook.md.
