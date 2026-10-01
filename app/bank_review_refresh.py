"""Refresh the fourth native review block without consuming user answers."""
from copy import deepcopy

from .bank_archive_evidence import completed_bank_postings, approved_nonposting_holds
from .bank_income import classify_deposit, income_id, income_row_matches, validate_income_rows
from .bank_meaning_resolver import BankMeaningResolver
from .bank_meaning_rules import RULE_SHEET
from .bank_review_decisions import DECISION_SHEET
from .bank_review_store import BankReviewStore
from .bank_review_ui import BANK_LABELS, BANK_UI_HEADERS, PURPOSES, build_rows, checked, validate_rows
from .bank_review_groups import digest
from .bank_review_groups import validate_snapshot
from .bank_pdf_pipeline import NormalizedBankTransaction
from .bank_rule_usage import plan_applications
from datetime import datetime, timezone


def build_view(source, resolver, prior_rows=(), *, retired_originals=None):
    """Meaning approval and ledger settlement are independent state machines."""
    validate_rows(prior_rows)
    rows = build_rows(source.groups, prior_rows)
    incomes = validate_income_rows(source.incomes)
    retired_originals = retired_originals or {}
    class Ledger:
        def get(self, rng):
            return source.incomes if rng == "収入明細!A2:J" else source.expenses
    completed = completed_bank_postings(Ledger(), tuple(source.transactions.values()), source.imports)
    nonposting_holds = approved_nonposting_holds(Ledger(), tuple(source.transactions.values()), source.imports, resolver)
    income_states = {}
    counts = {"bank_confirmation_groups": 0, "bank_settlement_groups": 0,
              "bank_stale_groups": 0, "bank_income_missing": 0, "bank_income_conflicts": 0,
              "bank_groups_resolved": 0, "bank_ui_groups": 0, "bank_ui_rows": 0}
    for tx in source.transactions.values():
        if tx.signed_amount <= 0:
            continue
        decision = classify_deposit(tx, **source.legacy_rules, meaning_resolver=resolver)
        if decision.outcome != "confirmed_income":
            continue
        saved = incomes.get(income_id(tx.source_row_identity))
        if saved is None:
            state = "missing"
            counts["bank_income_missing"] += 1
        elif (completed.get(tx.source_row_identity) == "income"
              and income_row_matches(saved, decision.row(), approved_meaning=True)):
            state = "settled"
        else:
            state = "conflict"
            counts["bank_income_conflicts"] += 1
        income_states[tx.source_row_identity] = state
    output = []
    current_keys = set()
    labels = {meaning: label for label, meaning in PURPOSES.items() if meaning}
    for index, group in enumerate(source.groups):
        pair = deepcopy(rows[index * 2:index * 2 + 2])
        current_keys.add(group["key"])
        meanings = [resolver(source.transactions[member["identity"]]) for member in group["members"]]
        held = any(meaning["state"] == "held" for meaning in meanings)
        held |= any(meaning["state"] == "matched" and meaning["classification"] == "needs_review" for meaning in meanings)
        all_matched = all(meaning["state"] == "matched" for meaning in meanings)
        if all_matched:
            unique = {tuple(meaning[key] for key in ("classification", "income_category", "source_bank", "source_alias"))
                      for meaning in meanings}
            held |= len(unique) != 1
        if held or not all_matched:
            counts["bank_confirmation_groups"] += 1
            if held:
                pair[0][1] += "\n既存の用途判断と競合・確認が必要"
            output.extend(pair)
            continue
        meaning = meanings[0]
        pair[0][1] = str(pair[0][1]).split("\n処理結果：", 1)[0]
        purpose = meaning["classification"]
        conflict, pending = False, False
        for member in group["members"]:
            identity = member["identity"]
            saved_purpose = completed.get(identity)
            if purpose == "income":
                state = income_states.get(identity, "conflict")
                conflict |= state == "conflict"
                pending |= state != "settled"
            elif purpose == "expense":
                conflict |= saved_purpose == "income"
                pending |= saved_purpose != "expense"
            else:
                reason = nonposting_holds.get(identity)
                conflict |= bool(reason and reason != "unresolved_import_status")
                pending |= reason == "unresolved_import_status"
        # A later unsent edit remains an edit, even after an earlier answer was
        # saved. Refresh may never silently substitute the saved answer for it.
        controls = [labels[purpose], meaning["income_category"],
                    "登録する" if any(m.get("rule_ids") for m in meanings) else "今回のみ"]
        sender_controls = [BANK_LABELS.get(meaning["source_bank"], meaning["source_bank"]), meaning["source_alias"]]
        edited = ((pair[0][2:5] != ["未選択", "", "未選択"] and pair[0][2:5] != controls)
                  or (any(pair[1][2:4]) and pair[1][2:4] != sender_controls)
                  or (checked(pair[1][4]) and (purpose != "transfer" or pair[1][2:4] != sender_controls))
                  or checked(pair[0][5]))
        if edited:
            counts["bank_confirmation_groups"] += 1
            pair[0][1] += "\n用途保存済み・未送信の入力を保持"
        elif not pending and not conflict:
            counts["bank_groups_resolved"] += 1
            continue
        else:
            pair[0][2:6] = [*controls, False]
            pair[1][2:4] = sender_controls
            pair[1][4] = False
        pair[0][1] = pair[0][1].replace("用途未確定", "用途確定・記帳待ち" if not conflict else "用途確定・記帳内容の確認が必要")
        if conflict:
            counts["bank_confirmation_groups"] += int(not edited)
        elif pending:
            counts["bank_settlement_groups"] += 1
        output.extend(pair)
    # Keep unsent input whose original disappeared or whose group boundary
    # changed, but revoke its checkbox: it cannot authorize a different scope.
    for index in range(0, len(prior_rows), 2):
        pair = deepcopy(prior_rows[index:index + 2])
        if pair[0][6] in current_keys or (pair[0][2] == "未選択" and not checked(pair[0][5])):
            continue
        proof = validate_snapshot(pair[0][11], pair[0][6], pair[0][10])
        originals = [NormalizedBankTransaction(member["source"], member["account_alias"], member["date"],
            member["description"], member["signed_amount"], member["page"], member["row"],
            member["identity"], member["source_hash"], member["transaction_kind"])
            for member in proof["members"]]
        old_meanings = [resolver(tx) for tx in originals]
        saved = completed_bank_postings(Ledger(), originals, source.imports)
        nonposting = approved_nonposting_holds(Ledger(), originals, source.imports, resolver)
        if not checked(pair[0][5]) and all(meaning["state"] == "matched"
                and PURPOSES.get(pair[0][2]) == meaning["classification"]
                and pair[0][3] == meaning["income_category"]
                and pair[0][4] == ("登録する" if meaning.get("rule_ids") else "今回のみ")
                and pair[1][2:4] == [BANK_LABELS.get(meaning["source_bank"], meaning["source_bank"]), meaning["source_alias"]]
                and (not checked(pair[1][4]) or meaning["classification"] == "transfer")
                and ((meaning["classification"] in {"income", "expense"}
                      and saved.get(tx.source_row_identity) == meaning["classification"])
                     or (meaning["classification"] in {"transfer", "reimbursement", "other_nonwrite"}
                         and tx.source_row_identity not in nonposting
                         and retired_originals.get(member["file_id"], {}).get("pdf_sha256") == member["pdf_sha256"]))
                and (meaning["classification"] != "income" or income_row_matches(
                    incomes.get(income_id(tx.source_row_identity)),
                    classify_deposit(tx, **source.legacy_rules, meaning_resolver=resolver).row(), approved_meaning=True))
                for tx, meaning, member in zip(originals, old_meanings, proof["members"])):
            counts["bank_groups_resolved"] += 1
            continue
        pair[0][5] = False
        pair[0][1] = str(pair[0][1]).split("\n処理結果：")[0] + "\n処理結果：対象が変わりました。新しい対象を確認してください"
        output.extend(pair)
        counts["bank_stale_groups"] += 1
        counts["bank_confirmation_groups"] += 1
    validate_rows(output)
    counts["bank_ui_rows"], counts["bank_ui_groups"] = len(output), len(output) // 2
    header = BANK_UI_HEADERS.copy()
    if output or counts["bank_income_missing"] or counts["bank_income_conflicts"]:
        header[1] += (f"\n用途確認 {counts['bank_confirmation_groups']}グループ"
            f"\n記帳待ち {counts['bank_settlement_groups']}グループ"
            f"\n収入未記帳 {counts['bank_income_missing']}件 / 不一致 {counts['bank_income_conflicts']}件")
    return counts, header, output


class BankReviewRefresh:
    def __init__(self, db, source_reader, *, store=None, retirement_reader=None, clock=None):
        self.db, self.source_reader = db, source_reader
        self.store = store or BankReviewStore(db)
        self.retirement_reader = retirement_reader
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def refresh(self, *, apply=False):
        prior_header, prior = self.db.bank_review_ui_table()
        source = self.source_reader()
        master = self.store.read()
        resolver = BankMeaningResolver(master.records(RULE_SHEET), master.records(DECISION_SHEET),
            legacy_rules=source.legacy_rules, store=self.store, master=master)
        retired = self.retirement_reader(prior, source) if self.retirement_reader else {}
        counts, header, rows = build_view(source, resolver, prior, retired_originals=retired)
        usage_edits, usage_counts = plan_applications(source, resolver, master, applied_at=self.clock().isoformat())
        counts.update(usage_counts)
        if not apply:
            return {**counts, "bank_ui_updates": 0, "bank_usage_metadata_writes": 0}
        live = self.source_reader()
        if live.fingerprint != source.fingerprint or digest(live.legacy_rules_serialized) != digest(source.legacy_rules_serialized):
            raise ValueError("bank_review_source_changed_before_refresh")
        resolver.require_unchanged()
        if self.retirement_reader and self.retirement_reader(prior, live) != retired:
            raise ValueError("bank_review_retirement_changed_before_refresh")
        # This follows all source/master checks. Usage and count changes are
        # one metadata batch with exact read-back, and never financial writes.
        usage_writes = self.store.commit(master, usage_edits)
        normalized = lambda values: [["TRUE" if v is True else "FALSE" if v is False else str(v)
                                      for v in row] for row in values]
        if prior_header == header and normalized(prior) == normalized(rows):
            return {**counts, "bank_ui_updates": 0, "bank_usage_metadata_writes": usage_writes}
        self.db.replace_bank_review_ui_rows(rows, header=header)
        # Verify native literals, including controls and hidden proof. An
        # uncertain write is not automatically repeated.
        actual_header, actual = self.db.bank_review_ui_table()
        if actual_header != header or normalized(actual) != normalized(rows):
            raise ValueError("bank_review_ui_readback_failed")
        return {**counts, "bank_ui_updates": 1, "bank_usage_metadata_writes": usage_writes}


def run_bank_review_refresh(db, env, *, apply=False):
    if env.get("BANK_REVIEW_ENABLED", "false") != "true":
        return {"bank_review_disabled": 1}
    if apply:
        import subprocess
        from pathlib import Path
        from .production_flow import verify_execution_boundary
        head = subprocess.check_output(["git", "rev-parse", "HEAD"],
            cwd=Path(__file__).resolve().parents[1], text=True).strip()
        verify_execution_boundary(env, head)
    from .bank_review_source import from_environment
    source_reader = from_environment(db, env)
    retirement_reader = None
    if env.get("BANK_PDF_PROCESSED_DRIVE_FOLDER_ID"):
        from .bank_review_retirement import BankReviewRetirementReader
        retirement_reader = BankReviewRetirementReader(source_reader.drive, source_reader.folder,
            env["BANK_PDF_PROCESSED_DRIVE_FOLDER_ID"], download=source_reader.download)
    result = BankReviewRefresh(db, source_reader, retirement_reader=retirement_reader).refresh(apply=apply)
    if apply:
        from .bank_review_home import update_home
        result.update(update_home(db))
    return result
