# p14 authenticated plan-only runner

This manual workflow accepts one authenticated p14 request UUID. It reads the
existing private Drive context, HGA, item confirmation journal, source PDF,
frozen page/receipt manifest, current owner inputs, category master and ledgers.
The context reference is the only reference added to Git; individual page
binding, actor allowlist and signed input snapshots remain private Drive data.
No previous authentication URL is used and no posting permission is inferred.

## Scope and dependency extraction

Baseline main: `7f2e0035735a7470edf109f9bc36f2a783a51e76`.
Selected PR #91 source: `1ac98bdbe6941a879e1963e2bc0ff1a72bd720e7`.
PR #91 remains Draft/Open and is not checked out by the privileged runner.

| Plan-only module | Selected existing responsibility |
| --- | --- |
| identity / manifest | frozen source, page, bbox and Receipt Unit identities |
| authority / drive | validate HGA decision and verified actor provenance; GET only |
| completion / items / proof | validate exact owner snapshot and narrow item confirmation |
| wire / fields | semantic Sheet markers and original reference; no UI writes |
| readers | private ACL, source SHA256, HGA, categories and current Sheet inputs |
| planning / validated | existing PlanningDB and duplicate check; in-memory plan |
| runner | hosted-main guard, compact queue receipt and exact result read-back |

The two Receipt model classes and receipt validator are compatibility-only
copies scoped to this planner. They support the authenticated p14 quantity=None
and optional merchant without changing main intake validation or model policy.
No Web OAuth server, Medical writer, PDF observer/grouping engine, renderer,
Gemini invocation, lifecycle/history provisioning or cleanup is copied.

The existing `ReceiptPipeline` materialization tail is factored into
`_materialize_result`; normal intake still calls it after the same existing
privacy, validation and transaction-kind gates. An optional clock defaults to
the same existing clock. The planner supplies only memory-only PlanningDB and
a fixed timestamp from the authenticated confirmation. No new accounting writer.

## Execution boundary

`p14-plan-only.yml` has workflow_dispatch only, mode choice `plan_only` only,
contents:read and the existing kakeibo-production concurrency group.
The Python entrypoint additionally requires main, exact workflow_ref, repository,
current checkout SHA = GITHUB_SHA = validated-main SHA, production enabled,
legacy disabled, non-debug logging, exact spreadsheet and a valid request UUID.
Checkout never selects a PR or user-provided ref. Only the existing service
account and spreadsheet Secrets are exposed. No new Secrets or schedule.

Drive and accounting reads use read-only OAuth scopes. The separate queue port
uses only a Sheets scope and can issue only one exact A:F row PUT in existing
hidden `_PDF確認受付` (gid 261001092). It cannot append/update any ledger or
update source/authority files. No production DB is passed to ReceiptPipeline.
There is no apply/post/write mode, automatic retry or terminal promotion.

## Authenticated evidence and freshness

The protected journal's `validated_not_written` entry must bind the UUID,
owner actor hash, source/page/review/revision, frozen Unit/item identities,
candidate digest, exact current snapshot and explicit 確認済み + 記帳する inputs.
The actor evidence originated from the existing explicit Google OIDC POST,
not an onEdit email or a spreadsheet cell. HGA verified issuer/sub, method,
request binding and independent current privacy are rechecked from private state.
The 600-second login/session expiry does not erase durable confirmation evidence;
reusing it for plan-only does not extend it into accounting write authority.

Each fresh plan re-reads baseline file bytes, source hash, private ACL and HGA.
There are two independent full plans before queue intake, one after intake and
one after result read-back. All rows, identities, categories, timestamp, source,
HGA/journal bytes/ETags, snapshot and value/formula ledger digests must agree.
A possible duplicate or exact identity collision fails closed before intake.

The p14 canary budget is receipt 1, expense rows 10 (9 products + discount -13),
import row 1, total 3,801. The existing target binding requires the -13 discount
category to match the confirmed third product. Printed inclusive tax is not
added twice. No fabricated balancing items or validation tolerance.

## Existing hidden queue and replay

The UUID is reused as the plan intake ID. Only metadata is copied into the
existing queue: mode, context reference, Unit/item IDs, identity digest,
snapshot digest and confirmation digest. Full values stay in private Drive.
The compact envelope is below 2 KB in the synthetic size check.

State transitions: no row -> accepted -> plan_only_complete.
`plan_only_complete` means plan validation only, never imported/terminal.
Result contains counts, total, plan/snapshot/ledger digests and zero-write flags.
The original input cells, HGA, confirmation journal and audit event are not edited.

A repeated dispatch with the same UUID must find exactly one matching row and
produce the identical result without a second intake row or queue update.
UUID reuse with different content, duplicate UUID rows, another pending request
for the same Unit, or an unknown queue delivery is rejected. If a PUT times out,
read-back alone determines whether the intended row exists. No blind resend.
A retained accepted row may be reconciled by a later explicit plan-only dispatch;
there is still no accounting write capability.

## Promotion and live verification

1. Review this separate PR and its CI; leave PR #91 Draft/Open.
2. Obtain explicit owner approval before merging this PR into main.
3. Run existing required checks on merged main. Only after success update the
   existing KAKEIBO_VALIDATED_MAIN_SHA using the established approval procedure.
   Do not disable or alter scheduled workflows.
4. Dispatch this main workflow with the authenticated UUID and plan_only.
5. Confirm actual Actions URL/commit, queue binding/read-back, identical replay
   plan and zero accounting change. A local preview does not satisfy this step.
6. Keep p14 visible and nonterminal. Actual posting requires a separate explicit
   authorization and appropriate posting authority; a plan row cannot be promoted
   into a write request by changing its mode.

No raw source bytes, item content, actor identity, tokens, keys or full request
URLs are written to Actions logs or artifacts. Failures print only a safe fixed
category. No cleanup is enabled; queue metadata follows existing retention policy.
