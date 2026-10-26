# Read-only Authority diagnostics

The existing production preview adds an `authority` object for selected bank and
au PAY card sources. It reuses the protected policy providers and their existing
source, classification/status, schema and bounds validation. It also compares the
configured spreadsheet and bank inbox bindings and checks the current validity
interval. It does not issue a capability or change a policy, flag or checkpoint.

Public stdout and the Actions summary contain boolean checks only:
`policy_valid`, `source_matches`, `spreadsheet_matches`, `bounds_valid`,
`period_active`, and, for bank, `folder_matches`. `policy_valid` requires all
checks to pass. A source ingestion failure does not suppress this separate policy
diagnostic. Missing or invalid policy input produces a fixed unavailable result;
exception text and input values are never included.

Validated policy fields, exact bounds and timestamps, source/account scope,
target IDs and the original input SHA-256 are saved only in a local
`kakeibo-authority-<random>.private.json` report under `RUNNER_TEMP`, falling back
to the operating system temp directory. The directory must be absolute and
outside the repository. The existing exclusive-create, mode-0600 file helper is
used. Only the typed policy fields are saved: unknown JSON fields, audit keys,
OAuth tokens and service-account credentials are not copied. Plaintext reports
are never added to a state cache, uploaded, printed, or written to Drive.

## Private recovery from a hosted runner

The selected recovery method is an opt-in, owner-encrypted temporary artifact.
The owner creates a fresh RSA-3072 key locally. Only the base64 DER public key is
passed as the optional `authority_recipient` workflow input. Neither the encrypted
private key nor its passphrase is sent to GitHub; the existing service-account key
is not used for report encryption or decryption. There is no new server, database,
repository Secret/Variable, or permanent authentication service.

The original diagnostic is unchanged. After it saves its private report locally,
an opt-in hook encrypts the same bytes using a fresh AES-256-GCM key and nonce.
RSA-OAEP-SHA256 wraps the AES key for the owner's public key. The run ID, attempt,
full head SHA, format version and recipient fingerprint are authenticated as
associated data. Invalid keys or encryption failures stop the run without a
plaintext upload fallback.

Only the exact `RUNNER_TEMP/kakeibo-authority-delivery/authority-report.enc.json`
file is uploaded, with a one-day retention period. That directory contains no
plaintext reports or keys. The upload runs only for an owner-initiated manual
`mode=preview`, `scope=drive_bank` run with a nonempty recipient. Both actor and
triggering actor must be the repository owner. A recipient cannot enable apply or
select another source. Existing main/validated-SHA guards remain mandatory.

The following are operator steps for a later separately authorized live preview;
they are not performed by implementing this PR:

1. Create a private local directory outside the repository. Generate a fresh key:

   ```text
   python -m app.authority_report_delivery keygen --private-key ABS_PRIVATE_DIR/owner.pem --public-key ABS_PRIVATE_DIR/owner-public.txt
   ```

   The CLI prompts twice for a local passphrase of at least 16 bytes. It saves a
   password-encrypted PKCS8 private key and the public key to new local files.
   It does not print their contents or overwrite existing files. Keep the
   passphrase off command lines, workflow inputs, environment variables and logs.

2. After the separate main/SHA verification and alignment, use the contents of
   `owner-public.txt` as `authority_recipient` for bank preview only. Keep bank
   apply false and the shared schedule stopped.

3. Through the authenticated GitHub account, verify the run belongs to this
   repository, the existing production workflow, `main`, the approved full SHA,
   the expected attempt, and the owner. Download only its encrypted artifact:

   ```text
   gh run download RUN_ID --repo tmoriuchi1401-source/kakeibo-ai --name authority-report-RUN_ID-ATTEMPT --dir ABS_PRIVATE_DIR/download
   python -m app.authority_report_delivery recover --envelope ABS_PRIVATE_DIR/download/authority-report.enc.json --private-key ABS_PRIVATE_DIR/owner.pem --output ABS_PRIVATE_DIR/authority-report.private.json --expected-run-id RUN_ID --expected-attempt ATTEMPT --expected-sha FULL_APPROVED_SHA
   ```

   Substitute absolute paths. The passphrase is entered locally using a hidden
   prompt. Recovery checks the recipient and exact run/attempt/SHA and decrypts
   before creating a new external local report. Wrong keys/passwords, corruption,
   metadata mismatch or existing output files produce only a fixed failure code.
   The CLI never prints Authority details. Encryption is not proof of sender
   identity: obtain the artifact from the verified run through authenticated
   GitHub, never from an arbitrary link or substituted envelope.

4. Read the recovered report locally. Once its contents and approval worksheet
   are retained in the owner's private location, dispose of the one-time key and
   temporary downloaded envelope as appropriate. GitHub's encrypted artifact
   expires after one day. Test fixture keys must never be used for live reports.

The hook runs only for `mode=preview`; apply behavior is unchanged. Existing
main/ref/repository/validated-SHA checks, the common production lock and source
selection are unchanged. This does not authorize dispatching apply, enabling a
schedule, changing Secrets/Variables or using Payroll as household income.

The implementation is not approval for its own production activation. A new
commit cannot run through the existing validated-main boundary until the separate
SHA verification and alignment process is authorized. A blocked or skipped run
must not be reported as a completed live policy inspection.

Regression coverage is in the existing `tests/test_production_flow.py`: private
data stays out of public output, unknown credential fields are not copied,
inputs are unchanged, target/source/bounds/expiry failures are detected, private
reports cannot be written inside the repository, and the main hook is preview
only. Existing workflow, bank-authority and card-recurring tests remain in use.

Recovery/privacy-only tests in `tests/test_authority_report_delivery.py` cover an
exact offline round trip, wrong recipient/password/run/attempt/SHA, authenticated
ciphertext/key/nonce tampering, unchanged existing output, invalid recipient and
size bounds, owner/source/mode restrictions, the unchanged public boolean report,
and the workflow's ciphertext-only path and retention.

## Approval worksheet after recovery

The worksheet must cover only a missing bank-income scope. Preserve valid bank
expense/card and other authorities; do not request their reapproval. Use recovered
bank policy values, not fixture values or documentation examples:

| Field | Evidence and proposed change |
|---|---|
| Before/after binding | Recovered policy hash and typed fields; re-read before any future update and after it. Limit changes to the approved income scope and flag. |
| Input and accounts | Exact existing bank inbox and supported sources from the recovered policy; reconcile account/source pairs with the previously verified private ledger inventory. No wildcard or expansion of the shared expense source set. |
| Destination | Exact existing spreadsheet, import A:L and income A:J worksheets. Keep IDs and aliases in the private worksheet. |
| Executor | Existing service account, approved main Actions workflow, validated SHA and shared production lock. |
| Allowed transactions | Only existing confirmed bank-income rules, including salary/bonus, using actual deposit date/amount. Exclude transfers, loans, unconfirmed deposits and refunds classified under the existing refund policy. Payroll income writes remain prohibited. |
| Period | Preserve the recovered initial boundary, validity interval, overlap and source-file window; distinguish these from the transaction's deposit date. Do not silently extend a validity period. |
| Bounds | Preserve actual max_files and shared max_rows. Record explicitly that the existing schema has no separate numeric money ceiling; never invent one in the approval table. |
| Duplicate/uncertain outcomes | Stable source ID/hash and income ID, existing-content conflict checks, exact read-back of import and income columns, existing pending/journal handling. Duplicate/conflict/UNKNOWN stays held; no automatic resend. |
| Activation and exclusions | One combined owner approval for missing scope and income flag only; no per-transaction reapproval, schedule enable, Cloud restart, card authority change or Payroll income. |

If live policy fields have not been recovered, keep the corresponding cells
unconfirmed. Do not present an incomplete worksheet as an approved Authority.
