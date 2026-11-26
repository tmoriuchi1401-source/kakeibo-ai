# Receipt retention protection hotfix

This independent Draft starts from main `fa7ac9378ef51e4a51258dd6053afe52ed0a2bb8`
and reuses only the retention protection from PR #208
`fef0d0a33b54f197a9f3bdd659766027563ca365`.
The owner requirements remain [A01–A15](https://github.com/tmoriuchi1401-source/kakeibo-ai/blob/fef0d0a33b54f197a9f3bdd659766027563ca365/docs/architecture-contract.md).

The processed folder and normal/age labels cannot authorize deletion. Listing
requires an independent native accounting completion check; deletion repeats
that check immediately before the existing delete call. Missing, failed or
unknown proof keeps the original. Existing processed originals use the same rule.

The existing private receipt store, source verification and exact native ledger
readback are reused. Every matching native confirmation member must be applied
with a recorded plan. Waiting, pending/unknown, incomplete and unproved members
are retained. PDFs remain ineligible until whole-source ALL-Unit completion can
be proved; one completed Unit does not authorize deleting the PDF.

The current monthly workflow does not supply the receipt proof bindings. With
this hotfix that absence denies cleanup rather than falling back to age-only
deletion. Backup, workflow/schedule, financial processing and live configuration
are unchanged. Do not run manual cleanup before an approved main application.

Scope: maintenance proof gate, existing cleanup/preview CLI composition and
offline retention regression tests. The existing PR #208 test-only httpx pin is
also reused so normal CI does not depend on the latest Gemini SDK retaining its
old transitive test import. Production dependency files are unchanged.

No cleanup, live Google mutation, original move/delete, merge, deployment,
validated-SHA change, STOP/fence release or schedule change is performed.
Main merge and validated-SHA promotion remain separate approvals. Production
remains NO-GO; CI success does not demonstrate live deletion or authorize it.
