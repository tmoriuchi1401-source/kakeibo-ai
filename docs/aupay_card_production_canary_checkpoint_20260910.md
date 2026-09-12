# au PAY card production canary checkpoint — 2026-09-10

- The original Gmail canary append succeeded for identity
  `aupaycard-mail:29957bdb0c3366b83592e053:002`.
- Read-back found a materialization-contract mismatch in columns B/H/I/K:
  blank import timestamp, `メール通知`, `unclassified_card`, and a 24-character
  value in the source-hash column.
- The old K value `8483bc33e12993d7590630ee` was the first 24 hexadecimal
  characters of a business fingerprint, not the established au PAY card source
  hash. The established K contract is the 64-hex SHA-256 of canonical source
  data: date, merchant, amount, payment type, member, memo, occurrence, and
  import ID.
- The repair changed only B/H/I/K. B now contains the original append dispatch
  time in JST format; H is `通常払い`; I is `auto_expense`; and K is the expected
  64-hex semantic source hash. A fresh read-back found exactly one identity row
  and matched the corrected row in full.
- Repair audit run `823342e6-2854-413c-a169-bdfad5ba0ece` recorded the reason and
  before/after digests in an append-only journal. Its one-shot capability is
  sealed and its execution lease was released.
- Writer-contract checkpoint commit:
  `025ed7e2e83a17c18132a66bc12e536a1110c0d9`.

No credential, HMAC secret, message body, or reusable authority is recorded in
this checkpoint.

## Staged production rollout checkpoint

- Human approval authorized a fresh staged rollout of 5 candidates, then 25,
  then all remaining production-eligible candidates in batches of at most 50
  rows and one Sheets append request per batch. Returns, ambiguous/review
  candidates, and unmatched Amazon candidates remained write-prohibited.
- Every fresh Gmail collection read 696 of 696 messages and parsed 2,644
  transactions with no parser-review item. The initial plan contained 1,322
  canonical transactions, including 1 existing identity, 5 returns, and 10
  cross-source ambiguous items. Amazon classification additionally withheld 1
  review item and 65 unmatched items.
- Phase 5 completed with verdict A: 5 exact rows, one request, sealed
  capability, released lease, and the complete five-stage journal. Run:
  `3abfde7d-cc3b-4050-bf57-80717f9e9923`.
- Phase 25 completed with verdict A after a new Gmail collection and full plan:
  25 exact rows, one request, sealed capability, released lease, and the
  complete five-stage journal. Run:
  `b89eb84d-0ac6-4754-bde7-e66b89babfbb`.
- The third fresh plan found 1,210 remaining production-eligible candidates.
  Twelve 50-row batches completed with verdict A (600 rows and 12 requests).
  Before the thirteenth remaining batch could acquire a lease, create a journal
  attempt, or invoke the transport, its target-header read received the Sheets
  per-user read-quota HTTP 429. Execution stopped immediately and no later batch
  was attempted. The stopped pre-write run is
  `3d3b88c5-0270-443a-86f5-c8b17c7b3d27`.
- Rollout total: 630 new rows in 14 successful one-shot requests. A fresh
  read-only audit found all 630 identities exactly once, with zero invalid B,
  H, I, or K cells. Statuses were 276 `auto_expense`, 38 `matched_amazon`, 4
  `matched_receipt`, and 312 `transfer_aupay_charge`.
- Authority audit: 15 immutable manifests and 15 protected approval artifacts;
  14 capabilities sealed; 70 append-only journal events across 14 completed
  runs; zero active leases. The stopped run's unused capability naturally
  expired while still recorded as `issued`; it was never claimed and cannot be
  dispatched after expiry.
- The common target binding was
  `writer-target-v1:282b0d11d2998d9e9cf6357bcb396f92`. The implementation and
  schemas require exactly 32 hexadecimal characters after the prefix. The
  earlier 33-character rendering was a report transcription typo; repository
  history contains no 33-character target reference.
- Google-rendered visual verification reached the final populated row, 1053,
  and showed the appended values in the existing `取込データ` layout without
  visible structural damage.

The rollout stopped with verdict B because 610 approved, eligible candidates
were not attempted after the read-quota failure. The 630 confirmed rows must
not be replayed.

## Quota recovery and rollout completion

- A later recovery run confirmed the Sheets read quota with one minimal header
  request and freshly verified all 630 earlier rollout identities exactly once.
  None of those identities was selected or written again.
- A new absolute-window Gmail collection read 696 of 696 messages and parsed
  2,644 transactions with no list/read failure, parser review, or review line
  item. Fresh reconciliation reported 631 existing identities (the repaired
  canary plus the prior 630), and exactly 610 remaining production-eligible
  candidates.
- The 610 candidates were applied through 13 new one-shot batches: twelve
  50-row batches and one 10-row batch. Each batch used a new UUID, immutable
  manifest, protected approval artifact, and capability; made exactly one
  append request; confirmed every complete A:L row exactly; sealed its
  capability; completed all five journal stages; and released its lease.
- Batches were paced by 20 seconds to stay below the Sheets per-user read quota
  without removing target, pre-write identity, or post-write identity checks.
  No quota failure, timeout, partial result, blind retry, or unknown outcome
  occurred during the resumed writes.
- A final fresh collection and reconciliation again read 696 of 696 messages
  and found 1,241 existing production identities, zero uninserted
  production-eligible candidates, 5 withheld returns, 10 cross-source
  ambiguous items, 1 Amazon review item, and 65 Amazon unmatched items.
- Final rollout audit: 1,240 new production rows in 27 one-request batches
  across the original and resumed rollouts; zero duplicate identities; zero
  invalid B/H/I/K cells; zero active leases; zero reusable capabilities; zero
  unresolved or partial writes; and 135 append-only journal events. The single
  historical failed-run capability remains recorded as `issued` but is expired,
  unclaimed, and unusable.
- The `取込データ` sheet contained 1,662 data rows after completion, with row
  1663 as its final populated row. Google-rendered visual verification reached
  that row and showed the appended data in the existing layout.

Final verdict: A.
