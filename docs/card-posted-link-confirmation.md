# Existing posted-expense confirmation with bounded J completion

An expense append can finish before its owner links. The reviewed stop contains
18 expense rows (64,116 yen), regular N formulas and eight completed J links.
Ten J cells are blank. These expenses must never be appended or edited again.

The existing `card_expenses` operator entry, signed capsule, main/tree binding,
single attempt, `kakeibo-production` exclusion, deadline, independent readers,
DurableState and ProductionLedger confirmation APIs remain the execution path.
No workflow, writer, database, projection store or recursive archive is added.

## Fixed scope and Preview

`posted-links-v1:` selects one additional pinned record checksum. The old
`posted-v1:` pair contract and its saved proof stay valid and unchanged.
The new record includes the original approved 18-row plan, business/profile
digests, stopped native/parent identity and revision, exact old history and
checkpoint digests, full financial before/partial/complete digests, documents,
rules, ACL, source hashes, eight linked rows and ten missing-link rows.
The record contains sensitive evidence and is transported only in the existing
encrypted capsule; only its checksum is committed.

Preview uses readonly clients and the same financial validator as Apply. It
accepts exactly the reviewed partial shape or its fully linked successor.
Unknown J subsets, other IDs, duplicate candidates, changed expense/category/
reason/source/N formula, changed old rows, documents, rules or ACL fail closed.
The existing literal planner checks a projected all-linked view against the
original contract: already_posted=18, new expenses=0. This projection is only
preflight evidence; actual links must be independently verified before saving
owner evidence or releasing pending. Preview never constructs a write client.

## One J request, then the existing state-only confirmation

After a separate fresh Apply signature and all guards:

1. Recheck financial/documents/profile/ACL and native/parent observations.
2. BatchGet all ten complete import rows literally. Require their approved
   original values and blank J cells. Existing eight J cells remain unchanged.
3. Send exactly one existing Sheets `values.batchUpdate`, RAW, ten J ranges,
   `num_retries=0`. No append, N write, existing expense update or journal write.
4. Independently GET complete finance and controls through the readonly client.
   Require the exact fully linked contract, regular N formulas, documents and
   state observations. The batch response alone is not completion evidence.
5. Reuse PR218 confirmation: owner_updates for all 18, pending evidence save,
   independent readback, native ready, independent readback and actual fresh
   restore, alias/stable-ID validation, old UNKNOWN proofs, parent API release,
   independent final GET. C3 and every old history row remain unchanged.

Without bank intervention the state sequence is native139 pending ->140
pending with owner proof ->141 ready, parent1434 pending ->1435 ready. Only
auto_expense phase/attempt/error and the regular parent generation may change.
Existing bank-noop revision proofs may update execution premises; unexplained
changes and bank posting are denied. Existing concurrent-run guards stay exact.

The state-only confirmation result counts no financial requests; the hosted
operator result additionally counts the one J request when it was performed.
`financial_write_authority=false` on this legacy operator record grants no
expense append: the new prefix/checksum binds only the ten existing-owner links.
It cannot authorize arbitrary records or financial rows.

On a failed or unknown J response, partial response or mismatch: stop, leave
native/parent pending, do not retry, append, delete or roll back. A later explicit
approval with a fresh observation may recognize all ten links already complete
and skip the J write. Any unexplained partial subset is denied. The existing
saved-state recognizer skips completed proof/ready/parent saves. Failed runs
retain their actual failure status; no automatic new run is started.

## Normal selected expense processing

SelectedDB now checks every planned owner in one `get_raw_ranges()` call before
one RAW J batch request. It independently checks every owner after that request;
the existing full financial/formula/document delta check remains mandatory.
The stable expense IDs, planner, append_raw and ordinary pending guards remain.
The remaining approved 22 expenses (179,335 yen) retain their categories,
reasons and business/profile contracts: one append plus one J batch request.
This reduces per-owner reads/writes; it does not prove that HTTP errors cannot
recur. Any such error still stops without automatic write resend.

## Diagnostics and evidence

The existing value-free diagnostics mark financial preflight, reservation,
append, owner_links_preflight/write/readback, financial exact readback and
checkpoint/state commit. HTTP status is extracted without exception text.
The parent revalidates only allowlisted stage/error code and integer HTTP status;
arbitrary child JSON, source values and raw exception bodies are not forwarded.
The original HttpError's status/cause remain unknown; no 429 claim is made.

Network-denied saved-data replay verifies the real 18/J8 stop, ten-only write,
unchanged N/expenses/journal, 18 owner proofs, fresh restore, old three UNKNOWN
and exact history/C3, then the unchanged 22-row plan through the existing writer.
Synthetic tests cover signed readonly Preview/Apply, interrupted state saves,
response unknown, partial J delivery, last-owner mismatch and no reappend.

This change is a Draft release candidate. Production merge, validated promotion,
dispatch, J/state/financial write and normal unattended restart are not approved
by implementation approval. Bank schedule, A01-A15, STOP, permanent fences,
money holds, receipt/PDF settings and the out-of-scope 501 rows remain unchanged.
