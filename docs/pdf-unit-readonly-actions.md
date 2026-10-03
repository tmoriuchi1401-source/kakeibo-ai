# Actions-only confirmed PDF Unit evaluation

The dedicated entrypoint is `python -m app.pdf_unit_readonly_analysis`; its
manual-only workflow is `.github/workflows/pdf-unit-readonly.yml`. It never calls
`production_flow`, ReceiptPipeline, a ledger or Medical writer, processed mover,
or authority-save interface. Existing production workflows are unchanged.

## Registration before execution

GitHub requires a `workflow_dispatch` workflow to exist on the default branch
before it can be dispatched. After registration, a dispatch may select a branch
using `--ref`. See [the event reference](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#workflow_dispatch)
and [manual execution](https://docs.github.com/en/actions/how-tos/manage-workflow-runs/manually-run-a-workflow).

This workflow is absent from main at implementation time. Do not dispatch a
production workflow, rename another registered workflow, add push/PR triggers,
or merge PR #91 as a workaround.

The smallest registration PR against main contains exactly one added file:

```
.github/workflows/pdf-unit-readonly.yml
```

Copy the reviewed version of this file from PR #91. No application code,
Medical code, state data, existing workflow, schedule, Secret, variable, or
Spreadsheet changes belong in that registration PR. Its only execution trigger
is an explicit manual dispatch. Registration and merge require separate approval;
this change on PR #91 does not perform either operation.

The registered workflow validates the open Draft PR #91 in the same repository,
its expected head/base branches, the exact operator-approved 40-character SHA,
and all three required successful CI checks. It then checks out that immutable
PR head with credential persistence disabled. The entrypoint repeats the PR/CI
checks and verifies checkout SHA, hosted Linux runner, workflow identity, manual
event, explicit `READ_ONLY`, and all three disabled feature flags. Only the final
analysis step receives existing analysis Secrets. Dependencies and OCR models
are installed before that step.

A separate main registration commit changes main's SHA. Existing production
validated-main controls must be reviewed by their owner through the existing
deployment process; this workflow does not change that variable or enable
production execution. Keep PR #91 Draft and unmerged.

## Existing configuration, never key export

Use existing `GEMINI_API_KEY`, `GOOGLE_SERVICE_ACCOUNT_JSON`, `SPREADSHEET_ID`,
and `RECEIPT_DRIVE_FOLDER_ID` Secrets and the existing encrypted
`PDF_GROUPING_BINDING` variable. No new API key, Secret, scope, ACL or binding is
created. The normal model matches the production workflow's existing override.
The service-account JSON is parsed in runner memory; no credential file is made.

Secret-name metadata establishes presence only. Runner preflight separately
checks nonempty values and authenticated binding/ACL/authority/source freshness.
Logs expose configuration booleans and result counts only; third-party output
and raw exception bodies are suppressed. Debug execution is rejected before the
analysis Secrets are provided. No echo, environment dump or plaintext artifact
is used.

## Staged manual execution after approved registration

Use the exact current PR #91 head whose CI has passed. These are future commands;
they do not bypass the registration requirement:

```sh
gh workflow run pdf-unit-readonly.yml --repo tmoriuchi1401-source/kakeibo-ai \
  --ref main -f approved_sha=<reviewed-PR91-head-SHA> -f confirm=READ_ONLY -f mode=preflight

gh workflow run pdf-unit-readonly.yml --repo tmoriuchi1401-source/kakeibo-ai \
  --ref main -f approved_sha=<same-reviewed-SHA> -f confirm=READ_ONLY -f mode=p2

gh workflow run pdf-unit-readonly.yml --repo tmoriuchi1401-source/kakeibo-ai \
  --ref main -f approved_sha=<same-reviewed-SHA> -f confirm=READ_ONLY \
  -f mode=remaining -f p2_run_id=<successful-p2-run-ID>
```

Wait for preflight success before p2. Wait for p2 success before `remaining`.
A privacy-blocked or failed p2 does not qualify. A parsed p2 requiring review
qualifies as analysis success, but still grants no posting permission.
Remaining verifies the completed successful manual run's repository/workflow,
single encrypted artifact and authenticated diagnostic, code SHA, source hash,
proposal digest, grouping revision, confirmation digest and p2 Unit ID. It also
requires p2's complete normal privacy gate and zero prohibited side effects.
An expired artifact or changed code/authority requires a new p2 run.

The source/binding/confirmation are pinned for this canary. Each Unit loads the
Drive正本, checks human normal and sticky automatic classification, checks fresh
whole-source bytes, renders only its selected page to a new RGB PNG and verifies
the member hash. It runs local OCR and the exact payload gate, rechecks authority
and source before analysis, and reruns the mandatory existing gate before each
SDK request. PDF bytes, neighbouring pages and embedded content are never sent.
Restricted automatic classifications remain blocked even after human normal
confirmation. A platform/render hash difference stops; it does not regenerate
authority. The excluded Medical page is neither rendered nor processed.

The ordinary receipt analyzer and category validation are reused with one API
attempt and at most three bounded correction readings. Units are processed
sequentially. Results are `would_import`, `would_need_review`, `privacy_blocked`,
`analysis_failed` or `authority_held`; none grants accounting authority.

## Private diagnostic on a public repository

No receipt fields appear in public logs. The diagnostic field whitelist includes
Unit/page/source hashes, model, status, date, merchant, total, item count,
validation issues and conservative validation checks. It excludes raw responses,
item descriptions, OCR, images, Medical input, credentials and Secrets.

Before any file write, the diagnostic is encrypted using a fresh AES-256-GCM
content key, wrapped with RSA-OAEP-SHA256 to the existing service-account public
key. This is an ephemeral encryption key, not a new API/authentication key. Only
`diagnostic.enc.json` is written/uploaded, with one-day retention. Decryption
requires the owner's existing service-account credential through a private
authorized process; no credential is requested from GitHub or exported by this
workflow. The remaining run decrypts p2 proof in runner memory. Artifact redirects
are restricted to signed GitHub storage and never receive the repository token.

Drive/Sheets use readonly OAuth scopes and a GET-only HTTP fence. Authority save
methods are also disabled. The runner compares authority content and ETag before
and after execution. No Google write, source move, Medical invocation or
authority mutation is performed, and no new schedule is added.
