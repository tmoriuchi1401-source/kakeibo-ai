# Printed tax, vouchers and owner-preserving revisions

Expense item amounts remain integer yen and must sum exactly to the receipt
total. This change does not widen automatic posting, change the schedule,
modify an existing ledger or grant posting authority.

* **External tax:** add one real tax row only when printed net subtotal,
  printed tax and printed gross subtotal agree. A missing item-sum difference
  alone is never tax evidence. The tax row uses the sole validated product
  category; mixed categories/rates without taxable-base allocation stay held.
* **Included tax:** do not add it again. Already-present external tax is
  idempotent; conflicting tax rows stay held.
* **Discounts:** preserve printed signs, amounts and item ownership. Existing
  adjustment/category validation still applies. Human category corrections
  are preserved even when they require further confirmation.
* **Points/vouchers:** earned points do not change expenses. A voucher label
  does not establish discount versus tender. Unresolved voucher use is a hard
  review gate, even when Gemini produces an apparently balanced item list.
* **Tender:** cash, prepaid cards and a purchased gift card are funding, not
  product discounts. This expense writer cannot express a voucher-funded
  gross expense as a smaller cash total; it must hold such a case rather than
  manufacture a negative product. A future funding breakdown must preserve
  both gross expense and actual cash paid before enabling that posting.

For an owner-confirmed discount, a pre-discount tax bridge may use **both
printed net and gross subtotals** only when one explicit tax rate explains
their difference and the separately printed final tax base plus final tax
equals the payable amount. The final tax is not added again. This bridge is
labelled `printed_subtotal_tax_bridge`, with bounded amounts/evidence digest.
No OCR or Gemini response is retained as permanent history.

## New analysis and existing cards

Two stable independent readings of one receipt can receive the same locally
corroborated external tax correction before completion drafts. Multi-receipt
pages cannot acquire an unlocated tax item: their existing item-box gate is
maintained. Low category confidence continues to require owner completion.

An existing card can be revised only before posting authority, confirmation
journal, prepared posting or write intent exists. Source/page/frozen Unit
identity are unchanged. New candidate/item digests invalidate old snapshots.
Every owner date, total, merchant, payment, memo and existing item name,
amount/category is carried forward. `確認済み` and `記帳する` are reset to
`未選択` because the item snapshot changed; totals are never re-requested.

The private registry is CAS-updated with a bounded repair digest and
`settlement_projection_pending`. The existing semantic Sheet formatter
projects the revised card. Exact snapshot read-back is required before
clearing that pending state. A timeout is inspected read-only; no blind
repeat of row insertion or registry writes. Scheduled projection/manual
preparation cannot advance a pending repair. This uses the existing Sheet,
OIDC/session-v2, private state and writer; no new UI, trigger or credentials.

The extra tax row's source is `source_document`, its category attribution is
`same_product_category`, in candidate repair metadata and item evidence note.
The existing UI's generic automatic-field enum is not independent Gemini
evidence for that row. Owner confirmation is still required for a repaired
card. Raw images remain temporary and the original is not copied/moved.
