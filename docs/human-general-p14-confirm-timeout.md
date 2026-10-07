# p14 explicit-confirm transport: bounded synchronous processing

The failed request is immutable evidence, not a recovery job. It remained
`claimed`, with zero grants/audit, identical dedicated Drive state bytes/ETag
and a disabled Sheet link. Do not adopt it, replay it or backfill authority.

Platform request logging is excluded to protect signed URLs. Consequently the
historical response status/duration and last individual downstream call cannot
be reconstructed. Read-only Cloud Run diagnosis of the same image and confirm
call graph reached the pre-write fence after 175 GETs / 16 source downloads.
The confirmation portion took about 81 seconds, exceeding the deployed 30-second
request deadline while still doing post-claim freshness work. The diagnostic
used an in-memory synthetic actor/request and denied PUT before HTTP; it was
not an authenticated confirmation or a live authority issuance. Runtime Drive
source/state reads, owner/SA ACL and canEdit all passed. No ACL/scope/key changes
were necessary.

## Minimal correction

Each HTTP request owns a bounded observation snapshot. Only source PDF and the
four pinned baseline files are reused within that request. Authority state is
always re-read; an observation snapshot never supplies authority. Teardown drops
all cached bytes and the deadline. The original PDF SHA256 is still verified.

Immediately before conditional PUT, clear the snapshot and re-read the ACL,
all baseline bytes/hashes and original PDF bytes/SHA256. Thus source or state
changes during confirmation fail closed, even when an earlier observation was
cached. A successful confirm needs two complete source reads, instead of sixteen.
If-Match must still match the current strong v2 ETag. Native 412 is never retried.
Exact envelope and inner authority read-back remain mandatory.

Drive work has a 45-second request budget, per-call timeout at most 25 seconds,
and refuses PUT when fewer than five seconds remain. The auth service deadline
may be set to 60 seconds only after isolated runtime measurements verify the
optimized path. This is not a timeout-only fix. No background worker, task retry,
new schedule or daily Actions processing change is introduced.

After a confirmation exception, a GET-only reconciliation classifies the
dedicated state as `not_written`, `written` (matching grant/audit/actor request),
or `unknown`. No class initiates another PUT or marks the failed request complete.
A claimed request remains consumed; only a new, independently prepared UUID
may be considered after durable outcome reconciliation.

## Logging and regression

Stage logging permits only an abbreviated UUID hash, stage, elapsed duration,
HTTP result, exception class and outcome enum. Never log exception messages,
request URLs, cookies, state/nonce/PKCE, OAuth codes/tokens, identities or receipt
content. `HGA_STAGE_DIAGNOSTICS=1` temporarily enables detailed stages; remove
the setting after the canary. Default logging retains only claim, completion,
failure/reconciliation and response status.

Synthetic tests cover HTTP-local disposal and exactly two source reads, source
and baseline mutation before PUT, source/authority read failures, native 412,
write-success/response-failure reconciliation, duplicate confirmation, expired
requests, request budgets and secret-safe stage logging. OIDC, owner allowlist,
unknown-only eligibility, exact Origin/CSRF, nonce/state/PKCE and one-time binding
are unchanged. No renderer, Gemini, accounting, Medical or mover is connected.

The live gate still requires a new p14-only request, owner Safari authentication,
explicit POST, the owner seeing the success sentence, one grant/one audit,
request `complete`, exact read-back and a rejected replay with unchanged bytes,
ETag and generation. Failure stops after read-only reconciliation and link
cleanup. HGA success does not start Gemini or accounting.

Google documents that a request deadline closes the connection with HTTP 504,
while the container may continue processing: [Cloud Run request timeout](https://docs.cloud.google.com/run/docs/configuring/request-timeout).
