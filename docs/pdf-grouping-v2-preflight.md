# Drive v2 conditional-write preflight

On 2026-10-01, Draft PR #91's `7890b230` foundation was checked against real
Drive using the existing private folder and service account. This test did not
create real grouping authority, install a management UI, enable a workflow,
process a receipt, invoke Gemini/Medical, write accounting data or move a source.

## Live observations

The existing state file was read as metadata only. Its ID, JSON MIME type,
private parent, owner and service-account writer permission matched. No existing
state content was downloaded or updated. Drive v2 returned HTTP 200 with matching
strong File `etag` and HTTP ETag. The v3 metadata probe had no ETag header.
The [v2 File resource](https://developers.google.com/workspace/drive/api/reference/rest/v2/files)
documents the `etag` field.

Exactly one isolated `pdf-grouping-v2-preflight-20261001-9d7a2c61.json` file was
created through the existing owner's Drive connector, inheriting the existing
private grants. Its folder/file ACL passed the existing owner + SA writer check;
no sharing/auth changes were made. All subsequent conditional updates used the
same service account and existing scopes. The file has schema
`pdf-grouping-etag-preflight-v1`, a synthetic marker/counter and three closed
flags. It contains no receipt source ID, OCR, names, amount, Medical information
or images. It cannot be loaded as `pdf-grouping-authority-v1`.

| Step | Real result |
| --- | --- |
| v2 metadata read / ETag A | HTTP 200, strong quoted tag |
| Update with `If-Match: A` | HTTP 200 |
| Metadata read / ETag B | HTTP 200, B differs from A |
| Direct server update with stale A | **HTTP 412** |
| Read after 412 | ETag B and content unchanged |
| Update with `If-Match: B` | HTTP 200 |
| Final metadata / ETag C | HTTP 200, C differs from B |
| Content read-back bracketed by C reads | **Exact bytes match** |
| New adapter read-only check | Strong C and exact final bytes match |

Each update was a single non-resumable v2 request with `num_retries=0`. The stale
request reached the server; it was not merely rejected by a client-side check.
Only 412 counted as successful conflict rejection. No wildcard, unconditional
update, retry, ETag synthesis or version-based CAS was used. The diagnostic file
is retained for audit; it is not configured as live authority.

## Limited adapter

`app/conditional_drive_state_v2.py` isolates
`ConditionalDriveStateTransportV2` and its v2 client factory. It exposes only
versioned read and conditional replacement, pins file/parent/JSON identity,
rejects invalid or weak tags, validates media/tag consistency and requires exact
read-back. A 412 immediately raises `state_changed_since_read`; it cannot be
retried or acknowledged even if another writer saved identical bytes. Ambiguous
delivery can be acknowledged only by exact read-back, without a second update.

Only the disabled manual PDF grouping worker selects this adapter. Its ACL
checks and source reader still use v3. Existing v3 credentials/scopes, transport,
receipt intake, bank, PayPay, recurring state, file movement, Medical, privacy
gates, normal Gemini path and workflow configuration are unchanged.

The live result supports adopting this adapter for PDF grouping conditional
persistence. It does not authorize deployment or real authority/UI activation.

## Regression coverage

All **3287 Python tests passed**, including the unchanged existing Medical
tests. The new v2 suite has **33 passing cases**. The three Node synthetic suites
also passed (**17 tests**). Two existing Python dependency deprecation warnings
remain. Compilation and `git diff --check` passed.

The new synthetic suite checks strong/weak/missing/malformed/wildcard tags,
mandatory `If-Match`, read races, changed identity, edit permission, stale 412,
other HTTP errors, no write retries/fallback, exact read-back, v3 resource refusal,
unchanged scope selection, v2-only worker wiring, confirmation/replay and closed
Gemini/receipt/accounting/Medical/source-move boundaries.

```text
python -m pytest tests/test_conditional_drive_state_v2.py -q --basetemp ../.pytest-v2-conditional-wiring
python -m pytest -q --basetemp ../.pytest-v2-conditional-full
node --test apps-script/category-submit/Code.test.cjs apps-script/manual-entry/Code.test.cjs .github/scripts/verify-state-cache-runs.test.cjs
```
