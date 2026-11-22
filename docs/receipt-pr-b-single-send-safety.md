# PR-B single-send safety correction (offline only)

Base: PR #202, `afeeb89a04a54c2b8c146be049006270e2fe1ed8`.
No real receipt retry, deployment, financial write or UNKNOWN recovery is included.
The failed real attempt's source/hash fences must remain untouched.

## Causes and boundaries

The prior SDK `attempts=1` did not limit `GeminiAI`'s separate three-reading
loop. The audited google-genai 3.6.1 Interactions wrapper also defaults to
automatic continuation: each continuation is another HTTP request. Its generated
retry adapter translates attempts=1 into max_retries=0, but that is per hop.
PR-B now disables continuation, limits the reading loop to one, and uses a
dedicated httpx client (redirects off, environment proxies off) whose
HTTPTransport has retries=0. A guard permits only one transport entry, only to
the expected HTTPS v1 Interactions POST endpoint. A second entry is rejected
before the delegate is called, regardless of SDK retry behavior.

The audited httpcore 1.0.9 connection retry loop exits immediately with
retries=0. HTTP/2 is disabled. The HTTP/1.1 pool's ConnectionNotAvailable loop
can only reassign a connection before request headers are sent; read, write and
protocol failures after sending propagate without replay. Fake network-backend
tests also exercise the actual httpx/httpcore stack without opening sockets.

Only the opt-in PR-B path uses this boundary and audited dependency versions in
`requirements-prb-safety.txt`. Other callers retain the previous three readings,
client construction and retry defaults. Unsupported PR-B SDK stacks fail closed.

An offline normal-response test also reproduced `TypeError: unhashable type:
'list'`: the JSON runner configuration supplied list pairs to an existing
validator that uses set(categories). PR-B alone converts these pairs to tuples.
This is a reproducible defect, not proof of the earlier real attempt's exception.
The real failure remains unidentified because its exception/status was not saved.

## Private diagnostics

Future intake requires `--diagnostic ABSOLUTE_NEW_PRIVATE_JSONL` before the
subcommand, in addition to the existing config and persistent attempt DB flags.
The file must not already exist. No directory, credential or receipt is created
automatically. Use the same permanent attempt DB across process restarts.

Records contain only allowlisted stage/result/error categories, bounded HTTP
status, allowlisted exception class (otherwise OtherException), HTTP attempt
count/presence and intake attempt presence. No exception text, stack, response
body, request/response headers, source IDs/hashes, image, merchant, item, key,
token or owner is serialized. SDK logging and warning output are suppressed on
the PR-B runner path. Records are flushed and fsynced before the transport or
intake boundary. Journal failure raises and stops; it never permits another send.
The source/hash fence is committed before analysis and retained on every failure.

Counts refer to transport-entry attempts, not proof that a server received an
image. A timeout or lost reply is UNKNOWN even when the transport count is one.
An unobserved/bypassed transport is UNKNOWN with a null count and cannot reach
intake. A last STARTED record after a crash or journal failure is unresolved:
do not reinterpret it as zero delivery or as permission to retry. Intake presence
is a durable pre-call intent, not proof that its server-side write committed.

## Offline evidence

`tests/test_receipt_review_single_send.py` drives the real installed SDK through
a fake HTTP transport with fabricated bytes and temporary SQLite databases.
It covers success, 401/429/503, timeout, lost response, invalid JSON/schema,
invalid receipt totals, continuation, redirect, a deliberately attempted second
transport call, diagnostic IO failure, UNKNOWN instrumentation, category shape,
SDK retry translation, dependency admission and secret canaries. Existing Gemini
and PR-B tests verify production behavior remains unchanged.

These tests do not establish live key validity, quota, model availability,
real iPhone success, or the historical Gemini request count. Main merge and any
new real-data trial remain separate decisions. No existing attempt is replayed.
