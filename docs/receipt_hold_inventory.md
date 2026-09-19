# Receipt inbox counts and confirmation questions

`found` counts supported files in the current receipt inbox. It does not count
historical reanalysis questions about already processed files. `needs_review`
retains the inbox-oriented meaning. `review_pending` counts actionable questions
in the confirmation store, with `normal_review_pending`, `medical_review_pending`
and `intake_review_pending` as its breakdown. Superseded rows with successors and
completed questions remain in history but are hidden in the sheet.

For example, 17 inbox originals can overlap only two of seven physical review
rows: two current medical questions, one obsolete medical version, and four
historical normal questions outside the inbox. Recording the other 15 inbox holds
produces 22 physical rows for 21 distinct original files. Closing two redundant
normal questions and hiding the obsolete medical row leaves 19 actionable rows.
The difference between inbox count and review count is explained by identity,
not by subtracting unrelated totals.

## Questions that can close without accounting writes

- A fixed, verified original already has complete matching receipt, import and
  detail rows. Merchant labels and whitespace-only product differences may keep
  their existing spelling. Product meaning, date, amount, category, payment
  method, line identities and relationships must otherwise agree.
- An owner already used the existing manual-expense workflow. The exact import
  marker, deterministic manual expense ID and single active linked row must
  match the original's date and amount. Preserve that decision instead of adding
  candidate details. Missing, duplicate, inconsistent or excluded rows do not close.
- Existing confirmation input, changed presentation, error, or reconfirmation
  requirement prevents automatic closure. Original version/content binding and
  current UI are checked before changing only the review status.

## Intake holds

The existing local privacy gate is unchanged. A blocked file now gets a durable
`intake` question keyed by the same source ID/version/content-hash scheme as
other review items. It contains a source link and data-free gate metadata, never
OCR text, a new AI candidate, or authority to post. Answering its type in the owner
memo does not authorize AI transmission or accounting. An intake hold also blocks
later automatic medical posting for the same source.

Rendering uses current accounting for the existing-value column, names the
actual differences, and preserves H:O. New review rows are written in one explicit
empty range and read back, without table-inferred append or retry after an
unknown write. Completed and obsolete rows are hidden, not removed. The decision
dropdown is limited to actions supported by each kind of question.

## Release boundary

Code and synthetic tests are independent from approval to change production.
Adding intake records, closing questions, changing managed sheet cells and hiding
rows are production writes. Present the exact source set and counts before applying
them. Do not rerun the general production workflow merely to refresh this UI if
that would introduce new AI requests or accounting writes outside the approved scope.

Rollback to code that recognizes only normal/medical questions cannot read a
store containing intake questions. Once a production store is migrated, retain
the new reader support when rolling back unrelated code.
