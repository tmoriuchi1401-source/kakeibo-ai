# Bounded PDF observation

Observation separates visible-page privacy from transaction grouping and later
payload creation. The original PDF cannot become an external-AI payload.
Multi-page observations retain no PNGs and grant no accounting, Medical or
archive authority. Existing single-page intake retains its one bounded normal
PNG for the existing independent privacy recheck.

## Geometry and budgets

Each page uses `min(3, sqrt(pass_pixel_budget / (width * height)))`, quantized
downward to a millionth. The calculation accounts for `ceil(width * scale)`
and `ceil(height * scale)`; actual bitmap dimensions are checked again before
copying/encoding. Scales below 0.125, nonfinite/nonpositive dimensions, dimensions
over 100,000 points, or aspect ratios above 32 fail closed. A standard pass that
cannot fit at minimum scale can use the second-pass budget only if that fits.

| Budget | Bound |
| --- | --- |
| Standard pass | 4,000,000 pixels/page |
| Optional second pass | 12,000,000 pixels/page; existing page cap unchanged |
| Live raster + scratch reservation | 48,000,000 pixel-equivalents; one page at a time |
| Source size / page count | Existing 50 MiB / 50 pages |
| Render calls | 100/document; at most two/page |
| Cumulative render work | 400,000,000 pixels/document |
| Conservative OCR work | 2,400,000,000 pixel-work/document |
| Elapsed work guard | 900 seconds, checked between page passes |

OCR work is reserved before rendering: `6 * pixels + 2 * min(4 * pixels,
12,000,000)` conservatively covers the existing text/token gate readings and
its bounded classification rereads. Live reservation is
`max(4 * pixels, 2 * pixels + 12,000,000)`, including transient RGB copies and
the existing OCR scratch. This is a logical raster bound, not a promise of exact
process RSS or a preemptive wall-clock timeout for existing OCR subprocesses.
PDF parser state and the bounded source bytes also consume memory.

The old 100,000,000 document-pixel cap remains a future composite payload cap.
It no longer rejects an observation just because sequential page work would
exceed that figure. The cumulative work budget independently prevents unlimited
processing. A failure leaves an explicit restricted observation for that page;
it never silently drops a failed page from the document.

## Sequential two-pass privacy

Render one complete page, run the unchanged local privacy gate, retain minimal
metadata, close the bitmap/Pillow objects and the per-page native PDF document
(including decoder caches), then proceed to the next page. A
second render is permitted only for incomplete observations, unresolved privacy,
or missing continuation anchors. There is no third pass. Render/OCR/resource
failures and unresolved completeness become `sensitive_unknown` with categorical
reason codes; parser/OCR exception messages and document text are not logged.

Any Medical, payroll or sensitive signal from an earlier reading remains a
restriction. Medical plus payroll becomes `sensitive_unknown`; a later normal
reading cannot erase it. A failed OCR channel also remains restricted even if
subsequent reads succeed. Embedded text only strengthens restrictions and never
substitutes for visible-page OCR. Existing Medical extraction, identity,
authority, persistence, and external-AI policy are unchanged.

## Metadata, grouping and replay

Page metadata includes source identity, page/hash, scale, extraction state,
classification/reason, completeness and render count. Grouping hints retain only
hashed issuer/date/receipt anchors, printed page numbers, total-presence and
continuation booleans, and only for completely observed normal pages. No OCR
text, raw name/date/receipt number, amount or images enter durable state.

Proposals are generated only after every page has an observation, and consume
these hints without reopening images or running OCR. Unknown pages remain
unknown in proposals and confirmed units. Legacy minimal state remains readable;
extended metadata is validated strictly. Changed render/hash/completeness/hints
invalidate the previous snapshot and confirmation, requiring a fresh review.

A future explicitly confirmed normal unit can request only its member pages
from the retained local source bytes. Regeneration uses each observed scale and
requires exact PNG hash agreement before the existing payload privacy check.
Observation and payload construction have separate lifetimes. No live payload
construction, AI send, accounting write, Medical handoff or source move is
enabled by this change. Review workflows and archive flags remain unchanged.

## Canary scope

The selected 14-page scan is observed read-only, with no source mutation. Only
its unconfirmed metadata/proposal may be saved to the existing dedicated Drive
state and displayed in the owned review tab. Confirmation is a later human step:
this rendering phase stops after displaying the proposal. HTTP mutation fences
allow only strong-ETag conditional state replacement and owned review-tab
updates, with exact read-back. Existing state files and accounting tabs cannot
be written by that runner.
