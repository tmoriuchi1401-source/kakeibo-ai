# Shared PDF owner manual canary

`app.pdf_page_manual_canary` processes one captured owner request from the
existing `PDFページ確認` / `_PDF確認受付` interface. It does not render pages,
run OCR, accept an AI key, change grouping/page-kind authority, move a source,
enable archive or connect scheduled PDF intake.

The fixed mixed-PDF canary admits only p1 Medical complete manual input and
p4/p10/p14 general complete manual input. Fields remain blank until the owner
types them. General date, positive amount and an existing category pair are
required; merchant/payment are optional. Medical uses the existing complete
manual parser, duplicate policy, durable confirmation and writer unchanged.

## Registration and admission

The manual workflow must first be registered on validated main as a separate
small change. The workflow defaults to `preflight`, shares the production mutex,
checks the exact open Draft PR #91 head and all three CI checks before exposing
existing Google credentials, and never receives Gemini credentials. The branch
entry also rechecks those boundaries. Only `review` accepts a captured UUID.
Owner fields do not appear in dispatch inputs, stdout, artifacts or raw errors.

Before any Medical page record is created, validated main must contain the exact
reviewed whole-file `ReceiptConfirmation.items` exclusion and archiver page skip.
Otherwise the request stops before acquiring the Medical write context. These
guards do not change amount parsing, HMAC identity, Medical admission, AUTO policy
or the existing manual writer.

The tested Apps Script handler still needs installation in the existing project
before live general confirmations are available. No new review sheet, trigger,
schedule, scope, Secret or binding is introduced. Registration alone does not
enable that handler, manufacture owner input or count as an iPhone check.

## Durable flow

1. Read current Drive v1/v2 grouping/page-kind proof and original source hash /
   page count, without rendering; reject stale or missing evidence.
2. Reconstruct the expected card from Drive and verify the captured identity,
   original link, explicit operation and current owner input snapshot.
3. Discover the existing private completion journal, verify its binding/ACL and
   strong v2 ETag, and require the existing projection journal before accounting.
4. General input uses `GeneralManualReview` and the existing `manual_entry`
   writer through a one-request adapter; it does not create another queue row.
   Medical input uses `PdfManualConfirmation` and the existing durable backend.
5. The HTTP accounting fence permits only one exact row from the saved pending
   intent per append, in the existing three accounting tables. It rechecks owner
   input/source authority before each append, refuses other destinations,
   `USER_ENTERED`, updates and deletes, and never resends uncertain delivery.
6. Exact accounting/backend read-back is mandatory. Medical completion stores
   only its existing backend reference in the per-Unit journal, without facility,
   amount, raw inputs, OCR, image data or a second Medical accounting plan.
7. Projection updates only the selected card's state/result and captured queue
   result; other owner fields/cards remain untouched. Failed projection can be
   retried against the same durable intent without another accounting append.
8. Replay must preserve rows, backend bytes, completion timestamps/ETag, Unit IDs
   and grouping/page-kind authority. Any inconsistency remains held.

## Current rollout boundary

This is a prepared Draft canary path, not proof of live owner completion. Actual
complete inputs, explicit confirmation, iPhone operation, live canary read-back
and replay remain required. The parent PDF remains unarchivable while any of the
four outstanding pages is unresolved. The separate normal-PDF production PR,
its canary, validated-main update and existing schedule connection follow only
after all 14 Units have verified terminal outcomes.
