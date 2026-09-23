# Canonical ledger date order

The validated `kakeibo-production` writer orders 支出明細 by 日付 and 収入明細
by 入金日 descending after a successful apply, including the isolated daily,
receipt, and money scopes. It runs only after writers release their physical row
hints. Preview and the read-only-ledger `projection` scope never sort rows.

`scope=ledger_order` previews or applies this operation alone under the same
production concurrency lock, validated main check, and APPLY confirmation.
It does not invoke intake, OCR, classification, or accounting creation.

Serial dates and literal ISO/slash dates share the same chronological key.
Ties preserve their original order. Headers stay at row 1. Every existing column
moves together, using a temporary rank column created, sorted, and removed in
one atomic Sheets batch. There is no accounting-value rewrite. Invalid dates or
duplicate/missing IDs stop before any sort; existing sorted tabs need no writes.

Before moving expense rows, the projection journal records `rebuild: true` and
`append: true`. A refresh then rebuilds the physical index and cached history
links, preserving category identities and coverage. The flag remains through a
failed/unknown sort or partial rebuild, so a later projection refresh recovers
before fixed-ID corrections use the index. A physical reorder intentionally
requires one full rebuild; ordinary unchanged-order refreshes remain incremental.
Do not replay accounting operations to recover a sort/projection failure.
