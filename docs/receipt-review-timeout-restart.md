# Phase 2 timeout correction (offline preparation only)

The running baseline was `d7d2a32f48caececf7ec89ceaf401fe12ac27066` with
google-genai 3.6.1, httpx 0.28.1 and httpcore 1.0.9. Its explicit HTTP client
inherited HTTPX's connect/read/write/pool timeout of 5 seconds. SDK
`http_options.timeout` and the per-call timeout were unset. A network-blocked
probe of the installed SDK confirmed all four effective Interactions timeouts
were 5 seconds. SDK retries translated to zero; HTTP transport retries were zero.

The saved real run records `ReadTimeout`, wrapped as `APITimeoutError`, one
transport entry and no intake. Reception start to STOP was 99.289 seconds;
reception start through both OFF acknowledgements was 102.681 seconds. The old
diagnostics contain no event timestamps: the exact AI wait and time spent in
local privacy/OCR cannot be recovered. A read inactivity timeout is confirmed;
the 5-second setting explains that limit, but server/network latency and the
exact historical elapsed read time are unknown. The outer 300-second window
and its then 240-second shutdown threshold did not cause this exception.

The correction sets HTTPX's four phases to 60 seconds, SDK timeout to 60000 ms,
and the single Interactions call to 60 seconds. It preserves no continuation,
one attempt/zero retries, the source/hash fence before sending, UNKNOWN stop,
medical exclusion and financial write OFF. Diagnostics add only monotonic
elapsed milliseconds from runner start and from transport entry. Response
timing includes the complete body; no image, prompt, key or response text is
logged. SDK-wrapped timeout/connection errors remain TRANSPORT_UNKNOWN.

HTTPX phase timeouts are inactivity limits, not a hard total wall-clock limit.
For the existing finite-window launcher, pass its absolute deadline into
`ReviewDiagnostics(..., deadline_epoch=...)`. Sending requires at least
60 + 75 seconds remaining. Use the existing outer watchdog and bound its owned
intake subprocess to window deadline minus 90 seconds; on expiry kill that
owned subprocess, preserve STOP/fences, and perform the same single OFF path.
The existing OFF requests have at most 65 seconds of configured wait, leaving
25 seconds within the 300-second ceiling. Do not rely on SDK timeout alone to
bound an entire operating window.

## Minimum application/restart steps

1. Use this PR's fixed, CI-passing Python commit on the existing PC and venv.
   Only `app/gemini_ai.py` and `app/receipt_review_diagnostics.py` change at runtime.
   No Apps Script change, SDK upgrade, new environment or server is required.
2. Apply the finite-launcher deadline/subprocess adjustments above. Preserve all
   old operation evidence, the existing SQLite DB, PR #204 fences and Phase 1
   result. Do not reset a diagnostic, attempt record or UUID.
3. Retain the UNKNOWN original's source **and hash** fences. Select an unattempted
   different ordinary original only after owner/version/hash, medical/privacy
   and existing-import checks. A new Drive ID cannot bypass a hash fence.
4. Reuse the saved owner refresh credential and existing owner-held GitHub PAT;
   do not issue a PAT. The previous trial Script copy was deleted by Stop, so
   restoring that same copy is a required owner input at restart.
5. This change authorizes no live send, reception ON, STOP clearing, financial
   write or scheduled task. Those operations were not performed for this fix.
