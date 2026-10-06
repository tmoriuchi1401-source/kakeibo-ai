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
include no-store, no-referrer, HSTS, nosniff and DENY framing. Error pages contain
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

- Final full Python suite: 4,442 passed, including existing Medical tests;
  targeted HTTP/core suite: 73 passed. Two existing dependency deprecation warnings.
- Node suite: 30 passed; compileall and diff check passed.
- Live private preflight: all three Secret configs valid; signed owner sub
  matches; private `/health` and signed `/start` return 200.
- Live native Firestore: current precondition update succeeds, stale precondition
  rejects, application stale ETag is `HTTP_412`, exact read-back matches and a
  latest-tag update succeeds. This tests actual server CAS, not just mocks.
- Public HTTPS `/health`: 200 after all private preflights and configuration
  read-back succeeded. Ready revision `hga-auth-synthetic-00003-kmr`, image digest
  `sha256:58e5b9b2a07ccf5e7a8815584a9758ed20c4ba45573ab5582a729c4ba64bc77b`.
- Live Google Web OAuth, explicit confirmation, post-confirm replay and real
  iPhone Sheets→Safari roundtrip are still pending until separately recorded.

No real-page canary may start merely because provisioning/offline tests pass.
The live authentication and phone gates must both succeed first. Synthetic
success is not permission to publish this host as a production authority store.

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
