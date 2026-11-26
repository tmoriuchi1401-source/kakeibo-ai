"""Native receipt writer adapter using existing production and ledger contracts.

The production factory always selects prepare-only. No environment flag or
synthetic result can turn this adapter into financial permission. The apply
method also requires the existing private production attempt and source intent.
"""
from copy import deepcopy

from .drive_run_state import StateError
from .page_receipt_model import digest
from .receipt_pipeline import ReceiptPipeline
from .receipt_validation import validate_receipt_result
from .receipt_plan.planning import PlanningDB, check_duplicates
from .receipt_plan.posting import ScopedMaterializerDB, outcome, read_ledger


class LedgerReader:
    def __init__(self, db): self.db = db

    def sheet_rows(self, region, *, render='UNFORMATTED_VALUE'):
        return self.db._execute_sheet_read(lambda: self.db.svc.spreadsheets().values().get(
            spreadsheetId=self.db.sid, range=region, valueRenderOption=render,
            dateTimeRenderOption='SERIAL_NUMBER')).get('values', [])


class ProductionReceiptAuthority:
    """Existing main/SHA boundary + private parent's current pending attempt.

Run numbers are deliberately absent. A public Actions run cannot authenticate
this operation. Drive ACL/bindings are verified by the existing context opener.
"""
    def __init__(self, env, ledger):
        self.env, self.ledger = dict(env), ledger
        self.attempt = ledger.value['sources']['receipts']['attempt']

    def check(self):
        from .production_flow import verify_execution_boundary
        from .production_ledger import validate_ledger
        verify_execution_boundary(self.env, self.env.get('GITHUB_SHA', ''))
        if self.env.get('RUNNER_ENVIRONMENT') != 'github-hosted' or self.env.get('RUNNER_OS') != 'Linux':
            raise StateError('receipt_hosted_runner_required')
        if (self.env.get('WORKFLOW_REF') != 'tmoriuchi1401-source/kakeibo-ai/.github/workflows/kakeibo-production.yml@refs/heads/main'
                or self.env.get('WORKFLOW_SHA') != self.env.get('GITHUB_SHA')
                or self.env.get('KAKEIBO_LEGACY_DISABLED') != 'true' or self.env.get('RUNNER_DEBUG') == '1'):
            raise StateError('receipt_native_workflow_required')
        value = validate_ledger(self.ledger.transport.read(), self.ledger.binding)
        # A source-only selector is not a proof that abandoned shared card
        # candidates are disjoint. Preserve UNKNOWN until that proof exists.
        if any(item['phase'] != 'ready' or item['attempt'] or item['error']
                for name, item in value['sources'].items() if name != 'receipts'):
            raise StateError('source_reconciliation_required')
        current = value['sources']['receipts']
        if (not self.attempt or self.env.get('RECEIPT_PARENT_ATTEMPT') != self.attempt
                or current['phase'] != 'pending' or current['attempt'] != self.attempt
                or current['error'] or self.ledger.transport.read() != self.ledger.payload):
            raise StateError('receipt_native_parent_attempt_required')


class NativeReceiptWriter:
    def __init__(self, db, authority, *, clock, readers=None):
        self.db, self.authority, self.clock = db, authority, clock
        self.readers = readers or LedgerReader(db)

    def prepare(self, parsed, source, link):
        self.authority.check()
        categories = self.db.categories()
        if parsed.transaction_kind != 'purchase' or not validate_receipt_result(parsed, categories)[0]:
            raise StateError('receipt_native_validation_failed')
        check_duplicates(self.db, source['source_id'], parsed)
        before = read_ledger(self.readers)
        stamp = self.clock()
        planning = PlanningDB(categories)
        ReceiptPipeline(planning, None, clock=lambda: stamp)._materialize_result(parsed, source['source_id'], link, [])
        plan = dict(rows=planning.plan, categories=categories, stamp=stamp, source=deepcopy(source),
                    parsed=parsed.model_dump(), link=link, before_ledger_digest=digest(before))
        plan['digest'] = digest(plan)
        return plan

    def reconcile(self, plan):
        self._binding(plan)
        return outcome(self.readers, plan, plan)

    def _binding(self, plan):
        if plan.get('digest') != digest({k: v for k, v in plan.items() if k != 'digest'}):
            raise StateError('receipt_native_plan_changed')

    def apply(self, plan, *, verify_intent):
        """No retries, updates, gap filling or permission derived from a flag.

        The caller must have persisted this exact plan before reaching here.
        On any append error, stop the materializer and reconcile the entire
        ledger, including old values/formulas, instead of sending another row.
        """
        self._binding(plan)
        self.authority.check()
        verify_intent(plan)
        if self.reconcile(plan)[0] != 'not_written':
            raise StateError('receipt_native_reconciliation_required')
        from .models import ReceiptResult
        parsed = ReceiptResult.model_validate(plan['parsed'])
        if (self.db.categories() != [tuple(c) for c in plan['categories']]
                or not validate_receipt_result(parsed, self.db.categories())[0]):
            raise StateError('receipt_native_categories_changed')
        check_duplicates(self.db, plan['source']['source_id'], parsed)
        scoped = ScopedMaterializerDB(self.db, self.readers, plan, plan)
        # Recheck native permissions, durable intent and source immediately
        # before EVERY append, retaining the existing exact-prefix fence.
        original_append = scoped.append
        def append(title, rows):
            self.authority.check()
            verify_intent(plan)
            original_append(title, rows)
        scoped.append = append
        try:
            ReceiptPipeline(scoped, None, clock=lambda: plan['stamp'])._materialize_result(
                parsed, plan['source']['source_id'], plan['link'], [])
        except Exception:
            if self.reconcile(plan)[0] != 'written':
                raise StateError('receipt_native_write_unknown') from None
        state, fingerprint = self.reconcile(plan)
        if state != 'written':
            raise StateError('receipt_native_exact_readback_required')
        return dict(ledger_digest=fingerprint, plan_digest=plan['digest'], exact_readback=True)
