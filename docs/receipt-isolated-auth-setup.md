# Receipt E2E: isolated authentication setup

2026-11-26 owner instruction; PR #208 remains Draft, financial write OFF,
real AI OFF and production NO-GO. A01–A15 are unchanged. This is a test-only
authentication configuration change, not a new application/runtime design.

## Existing evidence and selection

Reuse the existing owner auth run45750220158 as evidence for the old grant.
It does not prove a future isolated client/grant; do not redispatch it.
P1A and case2 have default Cloud projects. PRB uses4095117066 but retains
STOP=outcome_unconfirmed and an existing real-source trial binding. Neither
PRB nor the production-compatible Script may be repurposed or have fences cleared.
P1A must not be associated with4095117066.

Finish only the unexamined existing Script candidates. A candidate needs to
already use4095117066, be demonstrably isolated, and preserve its existing
owner/ACL/history/STOP/UNKNOWN. Do not infer suitability from a project name.
The final inventory is currently incomplete because browser activation failed;
this is not evidence that no suitable existing Script exists.

If none qualifies, the owner explicitly permits a new **test-only** standard
project, dedicated OAuth client and necessary grant. Do not change the existing
shared project or existing production grants/Secrets to make the test work.

## Fixed preparation for the approved alternative

1. Create one standard project named `KakeiboAI receipt E2E auth` with no
   billing linkage, paid services or runtime resources. Record its actual
   project ID and number from Google's completed create/readback. Do not
   predict the ID or reuse4095117066.
2. In that new project only, enable Apps Script API, Google Sheets API and
   Google Drive API. No Cloud Run, Compute, IAM keys, service account creation,
   or broad additional APIs are needed for this OAuth separation.
3. Configure Google Auth Platform for owner-only testing; use the existing
   owner as the sole test user. Do not publish for general access. Any consent
   screen, terms, security warning or persistent-access operation requiring
   user action must be completed by the user through the normal Google UI.
4. Create one Desktop app OAuth client named `KakeiboAI receipt E2E owner` in
   that new project. Download its client JSON to a private local directory.
   Never paste the client secret, token or authorization code into chat/PR/logs.
   Do not overwrite the old client JSON or old owner credential.
5. Read back P1A's exact Script identity/owner/ACL and legacy triggers/state.
   Preserve its previous source and all history before an authorized source
   update. Associate only P1A with the recorded new project number. Changing
   from its default project cannot be reversed back to a default project.
   Do not change another Script or the original shared project's settings.
6. Reuse Google's existing installed-app library/loopback flow to request only
   these five already-authorized types of scope (no Gmail or project-management
   scope):

   - `https://www.googleapis.com/auth/spreadsheets`
   - `https://www.googleapis.com/auth/script.scriptapp`
   - `https://www.googleapis.com/auth/script.external_request`
   - `https://www.googleapis.com/auth/userinfo.email`
   - `https://www.googleapis.com/auth/drive.readonly`

   The user completes sign-in/consent. Verify owner email, new client/project
   identity and returned scopes without emitting secrets. Save the isolated
   authorized-user payload privately with owner_email/project_id metadata.
   Scope upgrades and using the old production refresh token are forbidden.
7. Deploy the existing PR #208 receiver to only the verified isolated Script
   as a fixed-version API executable, access=MYSELF. Verify source hashes,
   manifest and deployment identity. Preserve legacy triggers/STOP/UNKNOWN;
   if a preserved fence conflicts, hold that target rather than work around it.

Google references: [Cloud project requirements/effects](https://developers.google.com/apps-script/guides/cloud-platform-projects),
[Execution API](https://developers.google.com/apps-script/api/how-tos/execute),
[installed-app OAuth](https://developers.google.com/identity/protocols/oauth2/native-app).

## Settings separation, without a second execution mechanism

The existing `state-cache-inspect.yml` isolated job maps:

| Existing application environment variable | Test-only GitHub setting |
|---|---|
| GOOGLE_RECEIPT_OWNER_TOKEN_JSON | Secret GOOGLE_RECEIPT_E2E_OWNER_TOKEN_JSON |
| RECEIPT_HOSTED_REVIEW_BINDING | Variable RECEIPT_HOSTED_E2E_REVIEW_BINDING |
| RECEIPT_HOSTED_E2E_CONFIG_JSON | Secret RECEIPT_HOSTED_E2E_CONFIG_JSON |

There is no fallback to the production owner Secret or review binding. Missing
test settings fail closed. The production workflow still references the old
production names, and its behavior/settings are unchanged.

Save new test settings only after identity/ACL/digest readback, using first-time
save guards. If a test setting already exists, compare its identity and stop
on mismatch; do not overwrite unknown credentials. Existing production IDs and
SA are used by the unchanged harness for collision-denial and the existing
Google state transport. They do not authorize financial writes or production
targets. Do not issue or rotate an SA key for this change.

Reuse the existing fixture builder, validator, writer plan and state schema.
Preserve P1A's old UI inputs/history; do not assume its46-column old sheet is
the Hosted98-column sheet or reset its old request store. Verify any selected
test book/state's compatibility before setting binding. New synthetic artifacts
must be separate from real originals and production financial books.

## Verification and remaining manual steps

Only the credential/binding references changed in the isolated Actions job.
Existing tests now assert that it cannot reference production owner settings.
Application, validator, writer and state code are unchanged. Reuse their existing
passing evidence. A new client's actual authentication/RPC must still be proved
by the necessary isolated preflight, not by relabeling the old auth result.

After setup: preflight → intake → user iPhone registration → process → user
correction/re-registration → process → fresh-runner restore → unknown-create
→ unknown-restore. UNKNOWN is last and its fence remains. No financial or real
AI calls. Keep existing workflow owner/branch/SHA/attempt/shared lock guards.

Current live blocker: `failed to activate captured window`, then after fresh
window selection `no cached app state is available for Chrome`. No UI workaround
was attempted. User restores Chrome to an ordinary visible foreground window
and opens Apps Script home. Resume the remaining candidate inventory first;
project/client creation and consent are not yet performed. Bundle user consent
steps when the actual target/client and normal OAuth flow are ready.

PR #209 stays prepared, main merge/validated promotion remain separate.
Card UNKNOWN and all existing production settings/fences remain unchanged.
