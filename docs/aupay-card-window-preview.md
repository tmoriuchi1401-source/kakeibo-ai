# Explicit au PAY card window Preview

The ordinary card runner still requires checkpoint-minus-overlap through real
current time and still rejects a window exceeding its existing authority.
This change adds only an explicit historical window to its existing dry-run:

```
python -m app.cli card-gmail-recurring --dry-run \
  --preview-window-start 2026-10-13T08:55:56+09:00 \
  --preview-window-end 2026-10-27T08:55:56+09:00
```

Both bounds are required. The start must equal the actual successful checkpoint
minus the unchanged overlap. The end must advance coverage, must not be in the
future, and must fit the unchanged maximum width. The authority validity period
is checked against real now, independently of these historical bounds. No clock
override, checkpoint substitution, initial-boundary fallback, arbitrary query or
Apply/canary combination is supported.

The source, spreadsheet, worksheet/header, statuses, audit key, parsers and
duplicate classification remain those of the existing card runner. Explicit
Preview also rejects inconsistent Gmail IDs/raw responses and overfull pages;
it never turns a truncated collection into success. The issuer-configured
message cap is unchanged (currently 500). Existing ordinary candidates and new
money records share the authority's batch cap (currently 100); duplicate money
records receive the existing readback. Unresolved money intents stop before
Gmail. Overflow stops the entire Preview rather than slicing candidates.

The dry-run branch returns before manifest/capability/lease/financial transport
creation. Money handling uses the existing read-only preview, not apply or
projection preparation. Existing dry-run summaries are still recorded in the
local SQLite with `advance_checkpoint=False`. A production Preview must restore
a disposable copy through `run_durable_source(..., apply=False)`; it must never
save that copy back to persistent Drive state. Summary history in that local
copy is distinct from financial writes or checkpoint advancement.

## Deployment boundary for the requested first window

The confirmed checkpoint is 2026-10-15 08:55:56 JST, overlap 172800 seconds and
maximum width 1209600 seconds. The requested first window is consequently
2026-10-13 08:55:56 through 2026-10-27 08:55:56 JST. These are reference values,
not authority JSON or permission to execute this Draft.

No existing manual-only card Preview workflow can run this branch. The legacy
card workflow has a schedule, requires main and legacy enabled, uses old cache
state, and remains disabled. The unified workflow is validated-main-only and
does not select a standalone explicit-window card Preview. Its source invoker
does capture child output privately, but this Draft does not add a new scope or
change that workflow. The authority-inspection workflow is not repurposed.

This Draft does not merge, update validated SHA, enable workflows, change flags,
dispatch, or call Gmail. The approved first-window Preview must stop before
deployment: it needs a separately reviewed production entry that selects only
this dry-run, preserves actual durable bindings and private result capture,
checks persistent/financial before-and-after snapshots, and returns no details
to public logs. No candidate counts, amounts or successful production Preview
are established by the offline tests. Apply and the second window remain
outside this change; an explicit window is rejected in Apply mode.
