# p14 authenticated read-only canary

The existing synthetic HTTPS confirmation endpoint can be deployed with an
operator-mounted, strictly validated p14 configuration. It cannot select
arbitrary sources or pages through browser parameters. The existing source,
grouping v1/v2, processing journal and accounting canary journal are read-only
and must exactly match the reviewed baseline. The source PDF remains in Inbox.

The endpoint needs separately approved access to the existing Drive service
account credential through Secret Manager. No credential is in the container,
Git, Sheets, or diagnostic. Folder/file ACLs are not changed. Until that access
and iPhone card usability are confirmed, no real authentication request is
prepared and no Human General Authority is issued.

## Explicit consent and storage

The new request action is `general_receipt_and_gemini_permission`. Existing page-kind or
legacy general selections do not become this consent. The existing Google
OIDC signature, issuer, audience, timestamps, nonce, verified email and owner
issuer/sub checks remain required. Login alone never issues authority; the
same-origin, CSRF-protected explicit confirmation POST is required.

`human-general-verified-drive-canary-v1` wraps the existing HGA schema with
durable verified actor provenance and the exact request binding. Only the new
dedicated state file may receive a conditional Drive v2 PUT. Strong If-Match
is mandatory, 412 is never retried, and exact content read-back is required.
The page's automatic `sensitive_unknown` classification is unchanged. The
inner authority allows single-page AI only; accounting, Medical and archive
remain false. Existing grouping and page-kind authority are never mutated.

## Read-only analysis

The existing manual-only Actions workflow gains `page_p14`; no production
workflow, schedule, main branch or Secret is changed. Its encrypted private
binding fixes the HGA file, exact authority bytes and allowlisted actor digest.
The runner rechecks private ACLs, source, page, review, revision and HGA before
each operation. HTTP mutations are blocked. It renders only p14 into a fresh,
single-frame, metadata-free RGB PNG. Exact local privacy and SDK byte hashes
remain required; clear Medical/payroll/PII evidence is still rejected.

Two independent readings yield corroborated Receipt Units and field-level
completion drafts. A second read-only analysis reuses the frozen manifest;
unstable receipt counts or positions stay in review. Only encrypted structured
diagnostics are uploaded, for the existing one-day retention. PNG, OCR text,
raw responses and credentials are never retained.

The existing PDF review tab is updated by structured source/page identity,
not fixed row coordinates. Only p14's card is replaced. Receipt cards use the
same editable vertical inputs, strict date validation and existing category
master. Reliable fields are filled; ambiguous fields remain blank. Even a
complete candidate does not enable the accounting action in this phase.

The operator must independently verify the fresh HGA/source, protected draft,
Receipt Unit manifest, category master, user input snapshot and existing
ledger before reporting ready or needs-review. Local diagnostics and hidden
cells never confer accounting authority. No writer or mover is invoked.
