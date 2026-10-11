# Unified receipt review: blocked read-only stage

This change adds an **offline Stage 1 prototype**, not a production cutover.
It reads bounded CellData snapshots and native receipt confirmation state.
It neither modifies the workbook nor connects an operation to a validator.
No Sheets, Drive, AI, authority minting, ledger writer, or archive client is
instantiated. Existing confirmation and PDF entry points remain unchanged.

The live audit found an old medical review ID absent from the current native
confirmation state, without an established Receipt Unit/index mapping. The
same page also has an unresolved existing-ledger reconciliation. Production
writes and the real-original canary must remain stopped until those bindings
are established from original evidence. Never infer a binding from a file ID,
page number, date, merchant, or amount, and never transfer another review's
owner confirmation or authority.

## Function mapping

| Existing entry and purpose | Inputs / operation | Proposed unified detail / existing validator |
|---|---|---|
| Receipt intake/type | Owner type answer and original inspection | Original/type section; retain local privacy gate and separate AI authorization |
| Receipt normal | Existing values, candidate lines, category, owner choice | General receipt comparison; `ReceiptConfirmation` validation/planning/readback |
| Receipt medical | Date, facility, actual paid amount, category, optional payment/memo | Fully manual medical form; existing medical validator; no OCR/AI candidate adoption |
| Receipt duplicate | Existing expense ID and owner comparison | Reconciliation precedes new posting; link without an additional financial posting |
| PDF structure | Page/receipt ranges, split/join decision | Page/Unit structure section; existing grouping authority, never posting permission |
| PDF page privacy | General/medical/out-of-scope answer and observation | Per-page privacy section; observation/version-specific gate retained |
| PDF general details | Stable line ID, amount/name/category, structure choice, action | General detail section; existing item snapshot validator and authenticated submission |
| PDF authenticated plan queue | Exact input snapshot, request/owner proof, plan receipt | Processing detail; a plan-only completion is not a financial grant |
| PDF explicit posting | Independent financial grant, exact plan, ledger fingerprint | Existing per-Unit posting validator and materializer; no new writer |
| PDF archive | Every page and Unit terminal, exact ledger readback, original parents | Source-level completion section; existing archive gate only |

The Stage 1 preview contains reference sections for these purposes. It has
**zero operation targets**. It deliberately does not auto-merge old references.
It does not yet implement the requested production list/detail interface.

## Display states

| State | Required meaning |
|---|---|
| 入力が必要 | Required fields are absent/incomplete; medical fields are fully manual |
| 原本確認が必要 | Type, privacy, structure, or existing-ledger comparison requires original inspection |
| 本人確認が必要 | This exact input snapshot requires this owner's authenticated confirmation |
| 処理待ち | Show received purpose, last processing time and remaining stage; never infer source completion from type completion |
| 保留・再確認 | Unmapped/changed identity, original/input revision, authority disagreement, partial/unknown accounting outcome |
| 完了 | Appropriate terminal authority plus readback and source-level completion evidence; no status-cell shortcut |

The conservative preview displays no completion until the full evidence is
available. Optional native financial receipts are validated with the existing
schema validator and snapshot reader. Their identity/input digests must match
the displayed card. Even an imported native financial receipt remains pending
source/readback checks in this prototype. Date/number render normalization is
used **only to find display differences**; raw inputs and writer digests never
change.

## Identity and presentation

Each reference carries its original schema, legacy ID/token, field markers,
source hash/version, page identity (including the current
`stable_page_identity` spelling), Unit ID, receipt index, and review purpose.
Case IDs include schema, legacy reference and immutable identity. File-only
joins are forbidden. Conflicting page identity spellings fail closed. Items
are bound by their stable item markers. Row insertion cannot change an ID.

The HTML prototype has an unfinished reference list with all eleven requested
columns, anchors into framed details, repeated case/page/receipt/state bands,
an explicit end marker, and separate history/test references. It escapes cell
content and only links to constrained original URLs. It has no forms/scripts.
This is an offline design artifact, not a deployed website or a Sheets sidebar.

Snapshots contain private receipt data. Store them and generated HTML/JSON
outside the repository; never upload them to PRs/CI. A snapshot containing
medical candidates must not project them as adoptable candidates.

Example (replace private paths with local bounded snapshot files):

```powershell
python -m app.unified_receipt_preview --receipt-snapshot <private-receipt.json> --pdf-snapshot <private-pdf.json> --confirmation <private-confirmation.json> --output <private-preview-directory>
```

Optional `--posting-receipt <private-candidate.json> <private-native-receipt.json>`
may be repeated. It does not grant permission, write accounting, or move files.

## Remaining rollout gates

1. Resolve orphan legacy identity/reconciliation without replacing inputs,
   minting substitute authority, or posting the medical receipt again.
2. Extend read-only projection to native-only reviews, page observations,
   terminal Units, successor relationships and complete original/readback
   evidence; reconcile all counts, states and links.
3. Connect unified detail to existing validators with exact immutable request
   bindings. Preserve source/input snapshot preconditions, CAS/claim, pending
   readback recovery and unique operation routing across both legacy entries.
4. Run synthetic operations for every requested receipt/structure/privacy,
   duplicate, stale-input, re-entry and concurrent-request case. Current new
   tests cover projection only; existing writer tests are not evidence of a
   unified writable route.
5. Run one real-original canary only after safety blockers are cleared;
   report writes, duplicate zero, AI sends, source moves and exact readback.
6. Obtain user approval before main merge or production entry switch.
7. Make legacy PDF review a read-only history only after the new route is
   validated. Audit old reads/submit/authentication/queue references and every
   unsent input before separately proposing deletion.

Desktop and a 390px browser viewport were checked for the prototype. iPhone
device testing has **not** been performed. This work is not release complete.
