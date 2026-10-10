# Google login reuse on the existing confirmation endpoint

This change separates login identity from operation authority and financial
posting authority. Google login does not grant AI permission or posting.

## Behavior and storage

The first request uses the unchanged Google authorization-code/PKCE flow and
server-side signature, issuer, audience, iat/exp, nonce, verified-email and
issuer/sub allowlist validation. It then rotates an unpredictable host-only
`__Host-hga-login` cookie (Secure, HttpOnly, SameSite=Lax). No Google token is
stored. The old cross-site callback cookie retains SameSite=None and its
600-second lifetime.

Subsequent requests use an encrypted Firestore login record. Each operation
still needs a fresh UUID, a 600-second request, snapshot/source/page/unit/item
binding and its own explicit confirmation POST. No GET grants authority.
Missing, expired or revoked login cookies require Google login. Corruption,
backend outage and unknown CAS outcomes fail closed, without retry.

Login defaults are an absolute eight hours and idle one hour. Configure them
with `HGA_LOGIN_ABSOLUTE_SECONDS` and `HGA_LOGIN_IDLE_SECONDS` (idle <= absolute,
60 seconds <= idle, absolute <= 24 hours). Every use enforces application-time
expiry; Firestore TTL removal may lag and is not the authorization check.

Identity sessions use `login_sessions`, hashed document IDs and a derived
encryption key separated from existing tickets. They store only verified
identity metadata, policy fingerprint, authentication/last-use times, expiry,
revocation and revision. Updates require Firestore update-time CAS, no retry
or unconditional fallback, and exact read-back. Login/logout/account switch
and allowlist/audience/revision/lifetime changes invalidate sessions.

The original `google_oidc_code_pkce_v1` / verification revision 1 remains
valid. Reused identity has `google_oidc_shared_session_v2` / revision 2 and is
bound to the new protected request and its current actor evidence. Browser
session integrity/revocation is rechecked before authority writes. Permanent
authority contains no raw or hashed browser session ID. Old decisions and
financial records are not migrated or modified.

Origin must equal the configured HTTPS origin exactly. Missing/null/wildcard
and lookalike Origins remain rejected. CSRF, state, nonce, PKCE, one-time
requests, freshness and duplicate/replay gates remain. Switching browsers
without the cookie prompts Google login; no Origin exception is added.
Parallel tabs retain separate request/ticket/CSRF bindings; mismatched tabs
fail closed rather than confirming another request. Open concurrent cards
sequentially in Safari. Logout revokes pending operations tied to that login.

The stable `/logout` entry works without an operation UUID, ticket, or unused
confirmation link. Existing screens link to it; the existing PDF review Sheet
can link to the same HTTPS path. GET only displays the existing styled screen.
The button sends a same-origin POST with a signed, expiring CSRF value bound to
the login cookie. POST revokes the server-side login with CAS/read-back and
clears login, operation-ticket, and start cookies. Successful operation
authorities are unchanged. The next fresh operation requires Google login.

## Why a pinned delta

The existing live service was built from `c29ecc44e1fb9fcc854cde1da5cce940e633e8fa`
and its Docker whitelist, while PR #91 remains unmerged. `baseline.json` pins
all 40 deployed source hashes; `existing-service.patch` and three additions
are the only container changes. `compose.py` refuses unknown paths, baseline
hash changes or result changes. It has no deployment, Secrets, writer, AI or
dispatch interface. It does not merge unrelated PR #91 files into main.

`app/receipt_plan/drive.py` has only the corresponding versioned evidence
validator compatibility change; its read-only transport is unchanged.

Run `python auth-session/compose.py --test` for isolated integration contracts.
CI has contents-read permission, no production environments or secrets and
no schedule. The pinned historical test tree is used only as offline fixtures.
Run `python auth-session/compose.py --output <new-directory>` to compose the
reviewable existing container; this does not deploy it.

## Approval and limited canary

Do not deploy or update validated SHA before owner approval of the independent
PR and green CI. Baseline Cloud Run image is
`sha256:f3a3ec1fbf61a029ad14c4d789f8fe6f8048e6e42c36f748a0fa2f21ebd3fc58`,
revision `hga-auth-synthetic-00015-6gg`. Preserve URL, OAuth client/scopes,
Secret versions, service account, memory/timeout/concurrency and IAM. No new
credential, Apps Script source, trigger or daily workflow is required.

After approval enable TTL on `expires_at` for `login_sessions` and isolated
`session_canary_routes`, `session_canary_requests`, `session_canary_authorities`.
The existing `real_request_profiles` TTL also covers synthetic route metadata.
Set `HGA_SESSION_CANARY_ENABLED=true` only during the limited canary.
Administrator-only seeding issues a new 600-second signed link per operation;
no public seed endpoint exists. Synthetic records never use p1/p4/p10/p14
identities, Drive, Sheets, accounting, Gemini, Medical, PDF movement or Actions.

On iPhone Safari verify first Google login + explicit synthetic HGA, then
synthetic item confirmation and another receipt with no Google login. Compare
backend method/request bindings and exact authority read-back; reject the
same links. Test logout -> new request -> Google login and session expiry ->
new request -> Google login. Use short operator-configured lifetimes only for
an isolated expiry canary, then restore the eight-hour/one-hour production
defaults and invalidate those short test sessions. Disable synthetic seeding
after verification. Existing pages, input, HGA, posting history and financial
values/formulas must read back unchanged before and after.

No real-page authority, financial write, Gemini call, Medical processing,
source move, cleanup, schedule change or PR #91 merge is authorized by this
session canary. Completion requires real Google/iPhone evidence; offline
tests alone do not prove the mobile flow.

References: [Google OIDC](https://developers.google.com/identity/openid-connect/openid-connect),
[Firestore TTL](https://firebase.google.com/docs/firestore/ttl),
[OWASP sessions](https://cheatsheetseries.owasp.org/cheatsheets/Session_Management_Cheat_Sheet.html).
