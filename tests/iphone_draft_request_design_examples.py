"""Read-only design examples, NOT a native validator or executable request.

Only existing parsers/validators/_plan are exercised. No authority, store,
consume/start/completion, dispatcher, host or financial factory is defined.
"""
from copy import deepcopy

from app.models import ReceiptResult
from app.page_receipt_model import SourceRef, stable_page
from app.receipt_reimport_production import target_snapshot
from app.receipt_validation import validate_receipt_result
from app.unified_receipt_native import confirmation_operation_identity
from app.unified_receipt_review import digest
from app.unified_receipt_write import OLD_P1_SOURCE, OLD_REFERENCES
from test_legacy_confirmation_connection_stop import native_fixture

SCHEMA = 'receipt-confirmation-draft-inputs-v2'
CORE = 'readonly-draft-review-example-v1'
FIELDS = ('date', 'merchant', 'amount', 'category', 'payment', 'memo')
OWNER = digest(['https://accounts.google.com', 'synthetic-design-owner'])


class Example:
    """Invented baseline provisioned once, then all native writes are denied."""
    def __init__(self, kind, monkeypatch, *, existing_expense=True):
        self.service, self.store, self.db, self.metadata, self.key = native_fixture(kind)
        self.kind = kind
        self.categories = [('食費', '食品'), ('日用品', '消耗品'), ('医療・保険', '病院')]
        self.category_revision = 'synthetic-category-revision-1'
        self.db.categories = lambda: deepcopy(self.categories)
        item = self.service.items[self.key]
        self.sid = item['source']['source_id']
        self.rid = 'R-' + self.sid
        if kind == 'normal' and existing_expense:
            # Existing native deterministic ID, not an item/amount match.
            self.db.rows['支出明細'].append([self.rid+'-01', '2026-01-02', 'Synthetic shop',
                'Synthetic A', 70, '食費', '食品', '現金', 'receipt', self.rid,
                'receipt:'+self.sid, 'preserve-label', 'active'])
            item['before'] = target_snapshot(self.service.tables(), self.sid)
        self.db.rows['支出明細'].append(['unrelated-id', '2020-01-01', 'Synthetic old',
            'Synthetic old', 3, '食費', '食品', '', 'manual', '', '', '', 'active'])
        # Unrelated rows are included in both full projections and kept exact.
        self.before = [deepcopy(self.db.rows), deepcopy(self.db.rows)]
        self.before[1]['支出明細'][-1][4] = '=1+2'
        self.baseline = deepcopy(self.store.value)
        self.native_tag = '"synthetic-native-initial-1"'
        self.source = dict(source_id=self.sid, sha256=item['source']['sha256'],
            native_source_version=item['source']['version'], drive_revision_id='synthetic-drive-rev-1',
            drive_numeric_version='1', source_kind='image', page_count=1)
        self.source['page_identity'] = stable_page(SourceRef(source_file_id=self.sid,
            source_content_hash=self.source['sha256'], source_kind='image', page_count=1), 1)
        self.source.update(scope='whole', receipt_unit_id=None, receipt_index=None)
        self.case_id = 'SYN-G101' if kind == 'normal' else 'SYN-M101'
        self.held = False
        self.prior_operation = None
        self.fields = dict(date='2026-01-03', merchant='Synthetic edited shop' if kind == 'normal' else 'Synthetic manual clinic',
            amount=120 if kind == 'normal' else 135, category='' if kind == 'normal' else '医療・保険｜病院',
            payment='Synthetic card', memo='Synthetic edited memo')
        self.items = []
        if kind == 'normal':
            for index, old in enumerate(self.baseline['records'][self.sid]['parsed']['items'], 1):
                self.items.append(dict(baseline_index=index, expense_id=f'{self.rid}-{index:02d}',
                    name='Synthetic corrected '+str(index), amount=80 if index == 1 else 40,
                    category='日用品｜消耗品' if index == 1 else '食費｜食品', state='entered', note='Synthetic line note'))
        self.draft_revision = 4
        self.draft_tag = '"synthetic-draft-4"'
        self.provenance = 'human-entered'
        self.draft_owner, self.draft_case = OWNER, self.case_id
        self.generation = 1
        self.financial_calls = 0
        def denied(*args, **kwargs):
            self.financial_calls += 1
            raise AssertionError('design_read_only_no_native_or_ledger_write')
        for name in ('save',):
            monkeypatch.setattr(self.store, name, denied)
        for name in ('append_raw', 'update_row_raw', 'set_raw_range', 'ensure_sheet'):
            monkeypatch.setattr(self.db, name, denied)
        for name in ('save_item', 'apply_confirmations', '_write_accounting_plan', 'render', 'capture_inputs'):
            monkeypatch.setattr(self.service, name, denied)

    def raw_state(self):
        return deepcopy((self.store.value, self.db.rows, self.metadata, self.native_tag))

    def draft(self):
        value = dict(fields=deepcopy(self.fields), items=deepcopy(self.items), provenance=self.provenance)
        return dict(domain='DRAFT_ONLY', owner=self.draft_owner, case_id=self.draft_case, generation=self.generation,
            revision=self.draft_revision, tag=self.draft_tag, input_digest=digest(value), value=value)

    def derive_example(self):
        """Pure reference example; intentionally returns NO qualified request."""
        before = self.raw_state()
        assert self.store.value == self.baseline
        item = deepcopy(self.baseline['confirmation_items'][self.key])
        assert item['status'] == 'waiting' and not item.get('require_reconfirm')
        assert item.get('decision_origin') != 'automatic'
        assert self.prior_operation is None
        assert not self.held and self.case_id not in {'G103', 'H301', 'SYN-G103', 'SYN-H301'}
        assert self.source['source_id'] != OLD_P1_SOURCE and self.key not in OLD_REFERENCES
        assert self.source['source_kind'] == 'image' and self.source['page_count'] == 1
        assert self.source['scope'] == 'whole' and self.source['receipt_unit_id'] is self.source['receipt_index'] is None
        assert (self.source['source_id'], self.source['sha256'], self.source['native_source_version']) == (
            item['source']['source_id'], item['source']['sha256'], item['source']['version'])
        assert self.source['drive_numeric_version'] == self.metadata['version']
        assert self.service.verify_source(item['source'], item['folder_id'])['version'] == item['source']['version']
        assert self.source['page_identity'] == stable_page(SourceRef(source_file_id=self.sid,
            source_content_hash=item['source']['sha256'], source_kind='image', page_count=1), 1)
        draft = self.draft()
        assert draft['owner'] == OWNER and draft['case_id'] == self.case_id and draft['domain'] == 'DRAFT_ONLY'
        assert type(draft['revision']) is int and draft['revision'] >= 0 and draft['tag'].startswith('"')
        assert type(draft['generation']) is int and draft['generation'] > 0
        fields = deepcopy(draft['value']['fields'])
        assert set(fields) == set(FIELDS)
        assert all(type(v) in (str, int) and not (isinstance(v, str) and (v.startswith('=') or len(v) > 4000)) for v in fields.values())
        assert type(fields['amount']) is int and fields['amount'] > 0
        confirmed = [fields[k] for k in FIELDS[:5]] + [
            '候補明細で確定' if self.kind == 'normal' else '医療費を確定', '', fields['memo']]
        execution = deepcopy(item); execution['inputs'] = confirmed
        lines = []
        if self.kind == 'normal':
            assert fields['category'] == ''  # Never broadcast a receipt category.
            original = self.baseline['records'][self.sid]['parsed']
            assert len(self.items) == len(original['items'])
            candidate = deepcopy(original)
            candidate.update(date=fields['date'], merchant=fields['merchant'], total=fields['amount'], payment_method=fields['payment'])
            for index, (entry, old) in enumerate(zip(self.items, original['items']), 1):
                assert set(entry) == {'baseline_index', 'expense_id', 'name', 'amount', 'category', 'state', 'note'}
                assert type(entry['baseline_index']) is int and entry['baseline_index'] == index
                assert entry['expense_id'] == f'{self.rid}-{index:02d}' and entry['state'] == 'entered'
                assert type(entry['amount']) is int and isinstance(entry['name'], str) and not entry['name'].startswith('=')
                pair = tuple(entry['category'].split('｜')); assert pair in self.categories
                candidate['items'][index-1] = dict(deepcopy(old), name=entry['name'], amount=entry['amount'],
                    major_category=pair[0], minor_category=pair[1])
                # Binding is a transport reference to EXISTING positional IDs.
                # It is not a new ledger ID and is never derived from edits.
                lines.append(dict(index=index, expense_id=entry['expense_id'], baseline_item_digest=digest(old)))
            parsed = ReceiptResult.model_validate(candidate)
        else:
            assert self.provenance == 'human-entered' and self.items == []
            assert all(str(fields[k]).strip() for k in ('date', 'merchant', 'category'))
            parsed = self.service._parsed(execution)  # Exact existing manual parser.
        assert validate_receipt_result(parsed, self.categories)[0]
        plan = self.service._plan(execution, parsed, '')  # Exact native duplicate/before/ID rules.
        assert self.raw_state() == before and self.financial_calls == 0
        inputs = dict(schema=SCHEMA,
            baseline=dict(legacy_digest=digest(self.baseline), item=deepcopy(item),
                candidate_digest=digest(self.baseline['records'].get(self.sid)), native_initial_tag=self.native_tag),
            draft=draft, source_binding=deepcopy(self.source),
            proposed=dict(confirmed=confirmed, parsed=parsed.model_dump(), lines=lines),
            categories=dict(pairs=[list(p) for p in self.categories], revision=self.category_revision),
            derivation=dict(plan_digest=digest(plan), before_digest=digest(self.before),
                source_digest=digest(self.source), line_digest=digest(lines)))
        result = dict(schema=CORE, kind=self.kind, case_id=self.case_id, owner=OWNER, purpose='ACCOUNTING_POST',
            operation=confirmation_operation_identity(item), inputs=inputs, plan=plan, before=deepcopy(self.before),
            accounting_permission=False, qualified_request=False)
        result['review_digest'] = digest(result)
        return result

    def compare_with_current_readers(self, displayed):
        """Design freshness oracle, not a production validator implementation."""
        assert displayed == self.derive_example()
        return dict(status='read_only_design_match', accounting_permission=False)


def rehash_review(value):
    value['review_digest'] = digest({k: v for k, v in value.items() if k != 'review_digest'})
    return value
