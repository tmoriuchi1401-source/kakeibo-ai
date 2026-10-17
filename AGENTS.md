# Codex working guidance (KakeiboAI)

## Receipt/PDF review and iPhone UI work

Before planning or changing the receipt-review, medical manual-entry, PDF page/grouping, unified review, authentication/confirmation UI, or related Cloud deployment path:

1. **Read `docs/iphone-receipt-review-mvp-spec.md` in full.** Treat it as the persistent user-approved product direction, *not* as approval for deployment or financial writes.
2. Optimize for a usable, low-maintenance **iPhone review flow**, not for building Cloud infrastructure. Google Sheets app (method A) is now adopted for the synthetic review UI based on user iPhone evaluation. Preserve its confirmed design in section 10 of the spec; do not restart method selection or switch to Apps Script / Cloud Run without prior user approval.
3. Preserve native purpose boundaries, source/page/Unit identity, medical privacy restrictions, verified owner/session/CSRF, duplicate/replay/fail-closed authority and existing writer. Do not replace them with UI-level checks.
4. Treat PR #123/#124 as **Draft**; do not merge, publish, enable production write, migrate legacy holds, or deploy Cloud resources without explicit separate approval.
5. Report what is actual production behavior versus synthetic/CI-only evidence. Do not claim iPhone Safari completion without device testing.

This guidance is scoped to receipt/PDF review and its UI/deployment. Other KakeiboAI subsystems keep their own requirements. If the referenced spec is missing, ask for it; do not substitute guesses.

## Confirmed iPhone UI contract (2026-10-17)

- **Always read `docs/iphone-receipt-review-mvp-spec.md`, including section 10 (「iPhone UI確定仕様」), before resuming any receipt/PDF review development.** It is the persistent source of the user-approved UI decisions.
- Keep one 「確認」 tab integrating general, medical, PDF and held cases: one row per case, fields in columns, horizontal scrolling allowed, case ID column and header frozen. The old four-tab layout is historical testing only; its two-column widths are not the adopted layout.
- Preserve original links, draft values, intermediate notes, validations and holds. Device acceptance includes search, original round trips, held-case display, input retention and reduced zoom retained after app restart. Do not restore internal full-URL navigation that opens Safari.
- Any future change to the confirmed UI needs a stated reason and impact scope, including input preservation and validation, and **explicit user approval before implementation**.
- User device acceptance covers the synthetic one-tab UI only. Production connection, authenticated/authorized confirmation and sorting safety remain incomplete. Sheet editing or changing status must never by itself execute posting. Never use row numbers as durable identity; independently validate case ID, source ID/hash/version, kind/purpose and input snapshot downstream.
- Keep H301 LEGACY_UNMAPPED_HOLD display-only and G103 held with existing inputs. Sorting/insertion/deletion/regeneration/concurrency must not transfer inputs to another case. Cell protections are not a safety boundary; owners can bypass them.
- Do not resume Cloud work or edit production or approved validation workbooks. New Drive resources require advance approval. PR #123/#124 stay Draft and unmerged. The Phase 1 Markdown-only PR may merge after clean diff/CI under the 2026-10-17 user instruction; Phase 2–3 processing changes stay on an independent branch and require separate approval for main merge or activation. No real posting, source move, AI sending or real authority update.
