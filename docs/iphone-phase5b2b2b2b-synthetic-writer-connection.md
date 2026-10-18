# Phase 5B-2B-2B: existing receipt writer in an isolated synthetic ledger

## Scope and result

This branch starts at PR #135 head `0a61648e318feb250cf0ffdaeb40a0c472260307`. It connects the existing `ReceiptPipeline._materialize_result` for the two whole-source automatic receipt outcomes (`normal_auto` and `buyback_income`) through the existing shared accounting gate and the 5B-2B-2A durable start/completion evidence. It does not connect `process_bytes`: that method can call Gemini and writes as one combined path, so analysis is supplied only as wholly synthetic `ReceiptResult` input after the existing receipt/privacy/income validators pass.

The implementation is deliberately partial. Legacy `ReceiptConfirmation`, medical manual entry, PDF automatic/manual/grouped, canary, explicit Unit and future one-tab entrances continue to be exercised by the existing shared-gate and native-writer regression tests, but this branch does not wire those native writers into the new evidence adapter. Their stored authorities, request journals and ledger plans have different scopes and schemas. Reusing the whole-source receipt gate as their authority would bypass those contracts. Medical stays manual-only; STRUCTURE/METADATA/RECONCILIATION remain outside financial posting.

This does not make the product or production path write-enabled. The only accepted writer backend is the explicitly marked in-memory synthetic ledger adapter. No `SheetsDB`, service, credential, Drive/Firestore/AI client, or external network transport is constructed or injected. The existing production constructors, runner, workflow, authority schema, validator and writer implementation are unchanged. The prior shared gate reports `native_authority=False`, and the synthetic run result is not a real native grant or production completion.

## Planner parity and writer boundary

`existing_plan` runs `ReceiptPipeline._materialize_result` against a write-capturing `PlanningDB` subclass. The target writer then rebuilds this plan immediately before dispatch and compares the complete operation order and rows to the evidence-bound Golden digest. For the tested synthetic source, both expense and buyback plans match exactly, including stable receipt/expense/income/import IDs, timestamps, amounts, category, income provenance/hash, table order and final import marker. No existing planner or validator was modified to make the Golden pass.

The synthetic ledger implements only the methods required by the existing materializer: reads for the receipt IDs, expense index, income validation, and append/append_raw. Every append must equal the next plan operation byte-for-byte at the row level; the current ledger must equal the saved before-snapshot plus the exact prior plan prefix; duplicate IDs, unexpected sheets, schema changes and formula-like USER_ENTERED strings fail closed. Each synthetic append uses the existing versioned IO conditional replace, then reads the values/formula projection back. The evidence adapter owns the sole capability to dispatch this writer after the one-time gate callback and exact writer-start marker are persisted/read back.

The same existing ReceiptPipeline method is used for the real synthetic append sequence. Normal expense writes receipt → all expense rows → import marker. Buyback writes receipt → validated `収入明細` row → import marker. Independent read-back uses the 2A ledger contract to check every target row, formula, ID, existing row and source/binding; a marker alone cannot complete the operation. Exact unrelated rows and formulas are asserted unchanged.

## Failure and recovery behavior

The tests crash after each normal/buyback append boundary, after marker persistence, and after a lost response following the import marker. Prefix rows classify as PARTIAL. Full rows without the native completion proof remain UNKNOWN. Reconstructing adapter/writer objects against the same synthetic IO and replaying the same request performs report-only reconciliation. No replay dispatches the writer; a new request for the owned source is rejected. The 16-way test uses 16 distinct journals, adapters, writers and gates over the same financial CAS IO, native evidence IO and ledger IO; exactly one writer dispatch succeeds.

The process-restart test starts a separate child process and transfers serialized synthetic fake-store snapshots to it. This checks adapter/schema reconstruction and read-only classification across process memory, not durable transport, filesystem or production-backend behavior. There is no automatic repair, retry, claim release, ledger gap-fill, or authority issuance. A real repair would require separate human review of the source, all input, before/observed ledger rows and pending/start evidence.

## Verification and remaining gates

The new dedicated Ubuntu workflow runs these writer tests, the complete #135 evidence tests, and existing authentication, privacy, purpose, duplicate/replay, native authority and PDF writer regression suites. It has read-only repository permission, the product write flag is false, and no secrets or deployment environment are configured.

Unverified: production planner parity, live Sheets USER_ENTERED/formula behavior, real versioned transport durability/ETags/IAM/encryption/retention/backup/audit/cost, actual native authority/provider bindings, durable process restart, iPhone/OAuth, and real data. The existing auto pipeline has no persisted native authority grant schema independent of the shared 5B synthetic gate; any production interpretation requires explicit design/approval. PDF and other native writers remain unconnected to this new whole-source adapter.

Next approval point: review the limited auto/buyback result and decide whether to design separate synthetic bridges for each native authority family (legacy confirmation, PDF Unit and grouped/canary paths). Any schema or writer-boundary change requires a concrete diff and prior approval. Production access, write enablement, spreadsheet/Drive resources, Cloud and main merge remain separate approvals.
