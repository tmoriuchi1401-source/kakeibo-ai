# CI-only isolated staging container

This package is preparation, not a Cloud deploy path. No WIF, Cloud credentials,
registry push, publicly published port, or real Google OAuth is used.

The independent Dockerfile uses an immutable official Python Linux/amd64 image
and 25 exact wheel versions/hashes. Wheel download and base pull are build-time
public dependency reads; pip installation in the Docker build is offline.
Repeated builds use the same source, wheel hashes and OCI-created input; the
second build may use Docker cache. This is not a claim of byte-identical images
across different creation timestamps or builders.

`prepare_context.py` creates a new workspace-local context, verifies the pinned
native host via the existing strict composer, and copies a small allowlist.
Production Dockerfile/runtime/manifest and the unified UI are unchanged.
The staging-only backend and shared-login copies change **two imports only**
to the pure-memory CAS port. Every non-import AST node must remain identical;
RSA/owner/state/session/CSRF/TTL/CAS/read-back/logout logic is retained.
Firestore/Drive/Secret SDKs, credentials, native real-page runtime, accounting
writers, authority stores, AI clients and schedulers are not packaged.

The native `make_application -> create_app(configured_runtime)` receives an
explicit memory factory in this separate process. Login absolute/idle and
operation TTL are 600 seconds. Startup rejects production connection settings.
All HTTP submit, OAuth callback/start, confirm, intake and archive routes are
denied. Authenticated prepare fails closed because no executable native port or
writer is provided; GET/prepare cannot issue authority or update case state.
The UI's synthetic display rows have no operation/evidence/authority targets.

There is no public synthetic login/seeding endpoint. The smoke CLI privately
exercises the existing RSA Google verifier against an ephemeral synthetic
certificate and code-exchange memory transport, binds the existing login/ticket,
then uses those verified cookies for real loopback HTTP. It does not forge an
actor or disable the web authentication gate. It never logs credentials.
This proves synthetic local contracts, not real Google identity or browser OAuth.

CI uses contents-read only, a pinned PR head SHA, Docker `--network=none`, no
published ports/mounts, non-root UID, read-only root, dropped capabilities, and
no-new-privileges. Application socket/DNS guards supplement the kernel boundary.
The smoke checks unauthenticated rejection, list/detail/logout, zero GET/prepare
memory writes, blocked submit/OAuth, CAS, state, logout/expiry, absent Cloud SDKs,
negative egress probes, and secret response/log nonreflection. Only explicit
synthetic auth bootstrap/logout may write memory auth records; authority and
case/financial records have no storage capability.

Run from a Git checkout on an existing Linux Docker host:

```
python -m staging_shadow.ci_build
```

The local image is not pushed. CI removes its own test container. Context/wheels
remain in the disposable runner workspace and are not uploaded as artifacts.
Cloud IAM/WIF/SA/delegation/registry/secret-name inventory remains a separate,
uncompleted gate. This image listens on loopback for CI and is not a ready-to-
deploy Cloud Run service. Any Cloud-compatible binding, IAM/egress arrangement,
synthetic authenticated operator bootstrap and source revision require separate
review, testing, inventory and deployment approval.
