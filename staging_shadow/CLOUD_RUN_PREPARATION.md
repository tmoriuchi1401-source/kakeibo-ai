# Cloud Run compatibility preparation (deployment blocked)

Base commit: `055922847c4ac337e466e949aeaf28b1ecad3f85`. This document is a
configuration proposal, not an executed Cloud change or deploy authorization.

## Read-only inventory

Local `gcloud` and Docker are unavailable. No authenticated Cloud API/Console
inventory was performed. This is an access-tool limitation, not evidence of
insufficient IAM permission. Cloud state must be read back independently.

Repository/workflow inspection found no WIF provider, trust/attribute condition,
Google deploy action, `id-token:write`, registry push or Cloud deploy path. The
staging workflow uses `contents:read`, no secrets, PR head SHA checkout and a
runner-local image. Repository secret **names** were listed, never values.
Production workflows reference `GOOGLE_SERVICE_ACCOUNT_JSON`, `SPREADSHEET_ID`,
receipt/processed folder IDs, authority JSONs and `GEMINI_API_KEY`; the staging
workflow references none. Their SA identity/roles/delegation remain unknown.

User-provided earlier inventory (not revalidated current state): project
`project-4cc…-89c`, region `asia-northeast1`, service `hga-auth-synthetic`, revision
`hga-auth-synthetic-00019-xf7`, IAM required, ingress all. Repository documentation
instead records an older `00015-6gg` and image; these historical references do
not prove current traffic or permissions. Never inherit either configuration.

| Item | Status / outstanding read-only evidence |
|---|---|
| Service/revision/traffic/ingress/IAM | Current settings and effective policies unconfirmed |
| Artifact Registry/image | Repository, reader/writer grants, digest/labels and retention unconfirmed |
| WIF | No repo definition found; provider, audience, repo/ref/actor trust unconfirmed |
| Build/deploy/runtime SA | Identity, effective roles, key metadata, delegation unconfirmed |
| Cloud Secret references/volumes | Names/versions/mount bindings unconfirmed; contents must not be read |
| Production/staging dependencies | Cloud graph unconfirmed; this packaged runtime has no production transport |
| Project identity/owner/billing | Previously user supplied; no current Cloud read-back |

## Explicit binding contract

Default is `ci`: `127.0.0.1`, port 8080 unless a valid explicit `PORT` is supplied.
`UNIFIED_STAGING_MODE=cloud-run` must be explicit and requires HTTPS `HGA_ORIGIN`
without userinfo/path/query/fragment and a decimal `PORT` in 1024..65535. It binds
`0.0.0.0:$PORT`. `K_SERVICE`/`K_REVISION`/`K_CONFIGURATION` never enable Cloud mode;
their presence in CI mode fails closed. No origin or deployment URL is inferred.
Production credential/connection/authority/write/proxy settings remain forbidden.

The [Cloud Run container contract](https://docs.cloud.google.com/run/docs/container-contract)
requires wildcard binding and supplies PORT; Cloud Run terminates HTTPS outside
the container. Binding compatibility does not establish IAM/egress safety.

CI builds the same pinned image and runs both modes inside `--network=none`, no
published port, non-root/read-only/cap-drop/no-new-privileges. The ordinary
entrypoint is checked over HTTP and `/proc/net/tcp` confirms the bound address
and port. Signed synthetic native auth/list/detail/prepare rejection/logout/CAS/
TTL checks use a separate private smoke process; there is no HTTP auth bypass or
seed endpoint. Public submit/OAuth/accounting remain disabled. A numeric wildcard
address can resolve for local binding; connect/connect_ex to that address, public
and metadata addresses remain forbidden. It is not an outbound allowlist entry.

## Required Cloud boundaries before any deployment

* Preserve IAM authentication and the invoker check. No allUsers/allAuthenticatedUsers
  grant. Invoker access only for the approved test principal on this service.
  Health is generic; all case data still requires native application session and
  owner validation, irrespective of forwarded HTTPS headers or Cloud IAM.
* Prefer internal ingress when the approved test caller has a proven internal
  route. If existing all ingress is needed, IAM must still reject unauthenticated
  requests including tag/revision URLs. Ingress choice remains unapproved.
* Dedicated runtime SA with no application resource roles, keys, delegation,
  impersonation grants, Drive/Sheets sharing, Secret accessor, Firestore or AI
  permission. Effective inherited grants must be audited, not just project roles.
  Build/push and deploy identities are separate; narrowly scoped registry writer,
  service update and actAs permissions are proposals, not verified grants.
* Cloud Run has no Docker `--network=none` switch. Use an approved Direct VPC or
  connector all-traffic egress path with deny rules for IPv4/IPv6 public and
  private destinations. No NAT/Google API exceptions. Prove DNS, metadata and
  routes that bypass VPC/firewall behavior separately; the Python guard is a
  supplemental control, not a kernel-level Cloud guarantee. If a Cloud-enforced
  boundary cannot be demonstrated, deployment remains blocked.
* Minimal env: explicit staging enable/mode, exact staging origin, write false;
  PORT is supplied by Cloud Run. No secrets/mounts/credential files, production
  IDs/authority/page config, AI clients, scheduler, WIF token or ADC configuration.
* Full source SHA, OCI labels and registry digest must agree before deployment.
  Initial traffic 0, retain old revision/image, no OAuth changes; min instances 0,
  max instances 1 and concurrency 1 proposed for ephemeral memory state. Restarts
  discard sessions/keys and fail closed; no durable/cross-instance CAS is claimed.
* Private synthetic authenticated operator setup is not yet available through
  Cloud Run's HTTP boundary. No public bootstrap will be added to bypass it.
  A separately reviewed safe testing mechanism is needed for Cloud list/detail.
* Require read-back of settings, IAM-denied anonymous/revision calls, protected
  HTTP smoke, outbound/metadata negative probes, audit logs and zero case/ledger/
  accounting/authority/Drive/AI writes. Do not log token, code, cookies or headers.
  Network routing, metadata and managed access-log behavior need actual Cloud tests.
* Rollback plan after a separately approved deployment: traffic to retained old
  revision; remove only the new staging revision/service if independently approved.
  No current rollback or Cloud mutation has occurred.

IAM/ingress are distinct from egress. Sources:
[service identity](https://docs.cloud.google.com/run/docs/securing/service-identity),
[private networking](https://docs.cloud.google.com/run/docs/securing/private-networking),
[Direct VPC egress](https://docs.cloud.google.com/run/docs/configuring/vpc-direct-vpc).

CI can prove packaging, binding, synthetic auth/CAS, explicit configuration
rejection, HTTP behavior and isolated Docker egress failure. It cannot prove
Cloud IAM, real service-account access, Cloud networking/metadata, real Google
OAuth, HTTPS cookie round-trip or iPhone Safari. Both PRs remain Draft; Cloud
push/deploy/revision/traffic/IAM/SA/Secret changes and main merge are prohibited
in this task. Cloud deployment is currently **not permitted**.
