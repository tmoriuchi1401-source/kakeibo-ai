# Human General: isolated synthetic HTTPS host

2026-10-06 deployment phase of the [OIDC core](human-general-authenticated-confirmation.md).
This service cannot load real PDFs, create real page authority, dispatch Actions,
or call accounting, Gemini, Medical, or movers. The container uses an explicit
source whitelist. The only source reader returns fixed synthetic bytes.

## Resources and configuration

| Resource | Dedicated configuration |
|---|---|
| Project | `project-4cc65fe0-cfa3-4818-89c` |
| Region | `asia-northeast1` |
| Cloud Run | `hga-auth-synthetic`; request-based CPU; min 0 / max 1; 1 CPU / 256 MiB; concurrency 4; timeout 30 s |
| Canonical origin | `https://hga-auth-synthetic-4095117066.asia-northeast1.run.app` |
| Web OAuth client | `kakeibo-hga synthetic web`, newly created, separate from Desktop |
| Redirect | canonical origin + `/oauth/callback`, exactly one redirect; no localhost or JS origins |
| Requested scopes | `openid email` only; no offline access or Google data API scopes |
| Secret Manager | `hga-synthetic-web-config`, `hga-synthetic-owner-policy`, `hga-synthetic-session-key`; Tokyo replication; mounted version 1 |
| Session/state DB | named Firestore database `hga-synthetic-auth`, native standard, Tokyo |
| Runtime SA | `hga-synthetic-runtime`; secretAccessor on those three secrets; datastore.user condition limited to that named database |
| Build SA | `hga-synthetic-build`; writer only on dedicated Artifact Registry repo, reader only on private build-source bucket, build logging writer |
| Build resources | Artifact Registry `hga-auth-images`; private uniform-access bucket `project-4cc65fe0-cfa3-4818-89c-hga-build` |

No secret values, owner sub/email, tokens or ephemeral authentication links belong
in this document or in Git. The Web client secret was transferred directly from
the creation screen to Secret Manager without a downloaded credentials file.
Administrator CLI OAuth is independent from the HGA Web OAuth flow. Existing
Desktop/Gmail/Apps Script token files are not used or modified.

Owner enrollment verifies the administrator's Google-signed ID token and its
audience, issuer, verified account and sub. It stores the canonical Google issuer
and sub in private configuration. It does not enroll the first public visitor.
Every HGA login independently verifies the Web client audience, signature,
issuer, iat, exp, nonce, email_verified, and that allowlisted sub.

## Shared state, expiry and conditional writes

`requests`, `authorities`, `pages` and `sessions` are per-synthetic-request
collections, with `expires_at` TTL policies. Logical expiry is 600 seconds and
is checked at every read. Firestore TTL deletion is asynchronous (usually within
24 hours); physical cleanup is not the security expiry boundary.

Sessions contain Fernet-encrypted server tickets. Raw state/nonce/CSRF/PKCE and
cookie values are not stored as plaintext. Session document keys are cookie
fingerprints. Requests store session fingerprints and verified actor evidence
bound to UUID and the complete source/page/review/revision/action digest.
No ID/access/refresh token is persisted. Synthetic authorities have the same
short expiry; this is not a production durable authority store.

The host ETag is a quoted SHA256 of Firestore server update-time plus exact state
bytes. This is **not** a Drive File ETag. Conditional writes compare the observed
bytes/tag and pass `LastUpdateOption(update_time)` to Firestore, with retry
disabled. A stale server precondition becomes host HTTP 412. There is no `*`,
set/upsert fallback, automatic conflict retry or unconditional update. Existing
Drive v2 conditional transport and production Drive state are unchanged.
The shared authority core validates schema and performs exact state read-back.
An ambiguous save stops as unknown and is never automatically reissued.

## HTTP and phone flow

1. Administrator creates a fresh isolated synthetic request privately. There is
   no public seed/admin route and no client-controlled source/page constructor.
2. A 10-minute signed preparation link opens `/start`. GET reads only and sets a
   stateless start-CSRF cookie; it does not create/confirm durable authority.
3. POST `/start` checks same Origin, signed link and CSRF, then starts Google
   authorization code + S256 PKCE + independent state and nonce.
4. Google returns `response_mode=form_post` to POST `/oauth/callback`. State,
   cookie and Google Origin are required. Code exchange and Google signature
   verification occur on the server. Login success leaves authority empty.
5. GET `/confirm` shows a single-column mobile screen and reads the current tag.
   Explicit POST with CSRF, Origin and ETag consumes the request before granting.
6. `/result` reads the confirmed authority and its audit/request digest linkage.
   Repeated callback/confirm/start attempts cannot issue another grant.

The OAuth session cookie is Secure/HttpOnly/host-only/SameSite=None because the
Google callback is a cross-site POST. Bootstrap cookie is SameSite=Lax. State,
nonce, PKCE, fixed callback, CSRF and Origin checks remain required. CSP allows
form redirects only to self and the fixed `accounts.google.com` origin. Headers
include no-store, strict-origin, HSTS, nosniff and DENY framing. Error pages contain
fixed Japanese messages, not exceptions, claims, tokens or technical identities.

Buttons have 54px minimum height, the screen is one column and max 420px wide.
An embedded Sheets browser must use its normal Safari-opening action; Google
OAuth must not be forced into a prohibited embedded WebView. A real iPhone test
is required; desktop emulation does not count. No real PDF card is modified to
add the synthetic link. Apps Script/onEdit/trigger/manifest modifications are zero.

## Logging and safety

Gunicorn access/error output is disabled to avoid secret-bearing URL/exception
logs. Dedicated `_Default` exclusion `hga-synthetic-request-url-private` matches
only this Cloud Run service's platform HTTP request logs. Other service logs and
audit logs are unchanged. OAuth codes travel in POST bodies, not callback URLs.
Never collect browser cookies, tokens, full request URLs or secret values into
diagnostic artifacts. Operator diagnostics print fixed status/boolean results.

Publication is staged: first IAM-private deployment, Secret mounts and native
Firestore CAS/read-back checks; only then external reachability. Public Cloud
Run invocation allows browsers to reach login, not authority issuance. Owner
OIDC + fresh request binding + explicit POST remain mandatory.

Sensitive eligibility is unchanged: automatic Medical, payroll and explicit PII
are refused even after owner verification. Old human “general” selections are
not migrated. No production p1/p4/p10/p14, accounting rows, original PDFs,
existing Actions, existing secrets or bindings are read or changed by this host.

## Verification and remaining gate

- Final full Python suite: 4,457 passed, including existing Medical tests;
  targeted HTTP/core suite: 88 passed. Two existing dependency deprecation warnings.
- Node suite: 30 passed; compileall and diff check passed.
- Live private preflight: all three Secret configs valid; signed owner sub
  matches; private `/health` and signed `/start` return 200.
- Live native Firestore: current precondition update succeeds, stale precondition
  rejects, application stale ETag is `HTTP_412`, exact read-back matches and a
  latest-tag update succeeds. This tests actual server CAS, not just mocks.
- Public HTTPS `/health`: 200 after all private preflights and configuration
  read-back succeeded. Ready revision `hga-auth-synthetic-00006-kfj`, image digest
  `sha256:98176402d019a966d707d38cf7e7cb5af56a6e097821ac827671ab36c937cf17`.
- On 2026-10-07 JST the owner completed the real iPhone Sheets→Safari→Google
  authentication→explicit confirmation→result flow and reported usable UX.
  The owner also confirmed rejection of the same original preparation link.
  Protected state read-back showed complete, one authority and one audit,
  verified Google-signed allowlisted actor and exact request/authority/audit
  digest linkage. Operator callback/confirm replays returned 409; result returned
  200; authority and request bytes and versions remained exactly unchanged.
- Desktop Chromium reproduced the header-policy difference on a local form
  probe. A separate Cloud Run flow in the Codex embedded browser rejected its
  start POST, leaving prepared/zero authority/zero audit. A normal desktop browser
  OAuth roundtrip remains a separate gate; an embedded-browser failure is not
  counted as success.

No real-page canary may start merely because provisioning/offline tests pass.
The live authentication and phone gates must both succeed first. Synthetic
success is not permission to publish this host as a production authority store.

## Same-origin form compatibility

The initial `no-referrer` policy also made ordinary browser form POSTs send
`Origin: null`. A temporary category-only live diagnostic observed this on
iPhone Chrome: Referer absent, Sec-Fetch-Site same-origin, Mode navigate and
Dest document. A desktop browser comparison reproduced null with no-referrer
and the exact expected Origin with strict-origin. After the policy fix, the
successful real iPhone Safari start POST had expected Origin, same-origin
Referer and same-origin/navigate/document Fetch Metadata.

Use `strict-origin` on the synthetic host. It sends only the origin as Referer,
never a path/query containing a signed preparation link, OAuth code or state.
Both /start and /confirm still require the configured origin's exact match;
missing/null/wildcard/lookalike origins are refused even with a matching Referer.
CSRF, cookie, state, nonce, PKCE, 600-second expiry, one-use UUID, explicit POST,
source freshness, owner allowlist, conditional save and read-back remain required.
GET/callback never create an authority. Old diagnostic links are not reused.
The temporary category-only header logger and its enabling environment setting
were removed from the final source and deployed revision after this diagnosis.
Temporary Sheet links were cleared without changing any real page card.

## Cost and upkeep

Min instances 0 means no always-on instance. For tens of confirmations/month,
the expected service/Firestore/Secret usage is within free quotas if those quotas
remain available in the shared billing account. TTL deletes are billable and
container/build-source storage plus Cloud Build may incur small charges. A
working estimate is under USD 1/month at low frequency, not a billing guarantee;
it excludes unrelated project/account consumption and repeated builds.

No scheduler, deployment workflow or regular job was added. Upkeep consists of
dependency/security rebuilds, three secret configurations, owner policy changes
when explicitly approved, and checking live authentication after changes.

Official references: [Cloud Run pricing](https://cloud.google.com/run/pricing),
[Secret Manager pricing](https://cloud.google.com/secret-manager/pricing),
[Firestore pricing](https://cloud.google.com/firestore/pricing),
[Firestore TTL](https://firebase.google.com/docs/firestore/ttl),
[Google OIDC](https://developers.google.com/identity/openid-connect/openid-connect).
