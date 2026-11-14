# Temporary metadata-only preflight

Placement is not permission to execute. No execution tag is created at placement.

- Placement branch: `diag/receipt-metadata-preflight-20261114`, based on `66ba428`.
- New workflow only: `.github/workflows/receipt-metadata-preflight-once.yml`.
- Trigger: **creation** of the fixed lightweight tag `receipt-metadata-preflight-once-20261114`.
- Branch pushes, workflow dispatch, schedules, tag updates/deletion and run attempts above 1 do not run the probe.
- Main and all pre-existing workflows stay unchanged. The standard tag-push event does not require the workflow to be merged into main.

After separate approval of the exact placement commit and the one real execution,
the operator creates the tag directly at that commit:

```text
git push origin <APPROVED_PLACEMENT_COMMIT>:refs/tags/receipt-metadata-preflight-once-20261114
```

Do not execute that command as part of code placement. Do not use `--force`,
recreate the tag, rerun the job or resend after an unknown result.

The job checks out the event SHA without persisting credentials. The validation
flag must remain `false`; debug logging is rejected before private inputs.
Existing `GOOGLE_SERVICE_ACCOUNT_JSON` and `RECEIPT_VALIDATION_CONFIG_JSON` are
referenced only as runtime environment Secrets in the last step. No Secret or
Variable is copied, registered or changed.

The standalone probe extracts only the service identity and fixed private/folder
targets from the binding. A pinned digest prevents substituting those three
values; the source contains no plaintext target IDs, identity or credential.
It has no UUID input, receipt-worker import, journal/body download, Sheets client,
validator, posting or trigger path.

Maximum probe-initiated Google requests: **3** — one OAuth token POST (metadata
read-only scope), then one private metadata GET and one folder metadata GET.
All URLs/methods have a one-use allowlist; TLS verification stays enabled,
redirects/retries are disabled at both SDK and HTTP adapter layers. Failure stops
remaining requests. Tokens/private keys/HTTP bodies are never written or logged.
Output rows contain only fixed stage/code values and HTTP status (`0` means no
HTTP response). A successful probe does not prove the old worker failure cause.

Checkout, Python setup and package installation additionally use ordinary GitHub
runner/package-download traffic. They have no Google credentials; dependency
installation uses zero configured retries. This traffic is outside the three
Google-request cap. Job timeout: 3 minutes; probe request timeout: connect 10s,
read 20s. Remote side effects of execution are the tag and Actions run/log only;
Google resource writes are zero.

Offline check only:

```text
python -m pytest tests/test_receipt_metadata_preflight.py -q
```
