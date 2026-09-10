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
