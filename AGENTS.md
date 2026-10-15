# Codex working guidance (KakeiboAI)

## Receipt/PDF review and iPhone UI work

Before planning or changing the receipt-review, medical manual-entry, PDF page/grouping, unified review, authentication/confirmation UI, or related Cloud deployment path:

1. **Read `docs/iphone-receipt-review-mvp-spec.md` in full.** Treat it as the persistent user-approved product direction, *not* as approval for deployment or financial writes.
2. Optimize for a usable, low-maintenance **iPhone review flow**, not for building Cloud infrastructure. Google Sheets app (method A) is now adopted for the synthetic review UI based on user iPhone evaluation. Preserve its confirmed design in section 10 of the spec; do not restart method selection or switch to Apps Script / Cloud Run without prior user approval.
3. Preserve native purpose boundaries, source/page/Unit identity, medical privacy restrictions, verified owner/session/CSRF, duplicate/replay/fail-closed authority and existing writer. Do not replace them with UI-level checks.
4. Treat PR #123/#124 as **Draft**; do not merge, publish, enable production write, migrate legacy holds, or deploy Cloud resources without explicit separate approval.
5. Report what is actual production behavior versus synthetic/CI-only evidence. Do not claim iPhone Safari completion without device testing.

This guidance is scoped to receipt/PDF review and its UI/deployment. Other KakeiboAI subsystems keep their own requirements. If the referenced spec is missing, ask for it; do not substitute guesses.

## Confirmed iPhone UI contract (2026-10-15)

- **Always read `docs/iphone-receipt-review-mvp-spec.md`, including section 10 (「iPhone UI確定仕様」), before resuming any receipt/PDF review development.** It is the persistent source of the user-approved UI decisions.
- Keep the four purpose tabs: 「一覧」「一般」「医療」「PDF」. Preserve the approved medical-based widths, font/wrap settings, draft values, validations, source links and holds. Do not add one tab per case.
- Navigate within the workbook using the bottom sheet tabs. Do not restore internal full-URL links that open Safari. Drive original links are intentionally external and must remain distinct.
- Any future change to the confirmed UI needs a stated reason and impact scope, including input preservation and validation, and **explicit user approval before implementation**.
- User device acceptance covers the basic synthetic Sheets UI only. Production posting, authenticated confirmation for the two users, dynamic multi-case rendering, real-data confirmation/canary and merging PR #123/#124 remain unimplemented/unapproved. Do not describe them as complete or infer authorization from UI acceptance.
- Do not resume Cloud work, edit the validation workbook or production Sheets, or change financial/authority/writer code as part of a documentation-only task. Keep both PRs Draft and do not merge main.
