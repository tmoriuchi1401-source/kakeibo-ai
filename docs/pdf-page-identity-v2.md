# Source identity and rendered bytes

`source_content_hash` is SHA256 of the original PDF bytes. A page's deterministic
identity is `digest(["pdf-page-v1", source_content_hash, page_number, page_count])`.
An insertion, deletion, reordered/replaced source, different ordinal or count
changes identity. Renderer, compression and runtime do not enter it.

The old `page_hash` is retained as `observation_render_hash` in native v2 page
metadata. It diagnoses the image produced at observation time. It is never
compared with a fresh transport PNG. `payload_sha256` instead binds the newly
rendered RGB PNG to the exact local OCR/privacy gate and the adapter's bytes.
The adapter rejects a changed digest before sending and retains its per-SDK-call
fingerprint and mandatory privacy gate. Ancillary metadata and multiframe/RGBA
transport images remain prohibited, even though PNG encoding is not identity.

## Separate v2 authority and existing human intent

`pdf-grouping-authority-v2` lives in a separate file in the same existing private
folder. The original v1 file, binding, confirmations and UI are untouched. The
filename is deterministic from the existing legacy binding; the runner requires
exactly one matching JSON file, checks its parent/private ACL, and pins a new
physical binding digest from that file ID. No new Secret, variable, scope, sharing
change, schedule or main application merge is needed. Missing/ambiguous state
fails closed, never falling back to v1 render identity for live analysis.

The explicit migration verifies current original bytes/hash and PDF page count,
source ID, complete page-number structure, current human page-kind proofs,
expected confirmed partition and legacy confirmation digest. It never renders
the excluded page, derives classification from OCR, or re-confirms the owner.
The existing revision, partition, automatic/human classification and all owner
confirmation timestamps are preserved. A separate migration timestamp and audit
event record the conversion.

Each native v2 group stores member page identities. New proposal/confirmation
digests and `pdf-confirmed-unit-v2` IDs bind source identity, page partition,
privacy decisions and grouping revision, excluding observation PNG/scale data.
Migration provenance links the old confirmation/proposal and timestamps. A
minimal independently valid v1 authority snapshot is retained as evidence of
the old human intent; no unrelated records/audit, OCR, images, merchant or amount
are copied. This evidence is explicitly labelled legacy and its render hashes
are not reinterpreted as native v2 identity.

Every v2 load verifies the canonical migration, current v1 bytes digest and
legacy intent, the two files' ACLs and strong v2 ETags. A new source or changed
legacy confirmation invalidates the old migration. UI values/local JSON never
restore authority. The new file is staged without authority, then published by
one `If-Match` conditional replacement with exact read-back. A 412 has no retry
or unconditional fallback. Replaying migration reuses its saved timestamp and
value; it does not create another file, revision, Unit or confirmation time.

All grouping and page-kind AI/accounting/Medical/archive flags remain false.
The explicit manual read-only invocation is separate operator intent. Human
normal does not erase sticky sensitive automatic evidence. Medical remains
manual-only and excluded from this renderer, analysis and source movement.

## Live read-only execution

The main-registered manual workflow is unchanged. Its reviewed PR head now loads
both v1 and v2正本, obtains the stable singleton Unit, verifies fresh source bytes
and page count/ordinal/identity, renders within the existing page/live/work budgets,
checks RGB/no metadata/single frame, runs complete local OCR and exact privacy,
and rechecks durable authority/source immediately before passing the authorized
digest to the receipt adapter. It processes one page at a time.

p2 must succeed on the same code, source, proposal, revision, confirmation and
Unit identity before the existing proof chain permits remaining pages. Diagnosis
uses the existing minimal encrypted artifact and one-day retention. No images,
OCR text, raw Gemini responses, Secrets or Medical content are persisted.

Production restoration is independent: verify current main's approved diff/CI
and safety tests, update only `KAKEIBO_VALIDATED_MAIN_SHA`, and read it back.
`ledger_order preview` exercises the unchanged production SHA guard with readonly
clients and no intake/writer/mover. Schedules, flags, bindings and Secrets are
not changed. PR #91 remains Draft and unmerged.
