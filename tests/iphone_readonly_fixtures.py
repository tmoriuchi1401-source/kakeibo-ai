"""Offline saved display evidence. All IDs/values are invented, no live IO.

Uses existing Case/page identities and snapshot format. Native validation and
writer behavior remain covered separately by the existing native dry-run suite.
"""
from copy import deepcopy
from dataclasses import asdict
import json
import uuid

from app.iphone_draft_journal import LocalJournal
from app.iphone_native_dry_run import bind_case, bind_versions
from app.iphone_readonly_host import SavedDraftRepository, make_isolated_readonly_application
from app.iphone_sheet_reader import HELD, issue_contract, original_formula, validate_snapshot
from app.unified_receipt_eligibility import current_snapshot
from app.unified_receipt_review import Case, LEGACY_UNMAPPED_HOLD, digest
from app.unified_receipt_shadow import ShadowConfig
from app.receipt_item_review import snapshot
from test_iphone_review_preview import from_case
from test_iphone_sheet_reader import fixture, text
from test_unified_receipt_eligibility import clean
from unified_purpose_fixtures import OWNER
from unified_shadow_fixtures import HostRig


def saved_bundle():
    raw, sources = fixture(); mappings = {}
    rows = raw['sheets'][0]['data'][0]['rowData']
    for row in rows[1:]:
        cells = row['values']; alias = cells[0]['userEnteredValue']['stringValue']
        kind = cells[1]['userEnteredValue']['stringValue']
        # Representative manual/unfinished drafts, without using any real
        # workbook, clinic, original, account or medical information.
        if kind in {'一般','医療'}:
            for col,value in {4:'2026-10-18',5:('架空手入力クリニック ' if kind == '医療' else '架空店舗 ')+alias,
                              7:'医療・保険｜病院' if kind == '医療' else '',8:'現金' if kind == '医療' else '対象外'}.items():
                cells[col]['userEnteredValue'] = {'stringValue':value}
            if alias == 'SYN-M102': cells[6]['userEnteredValue'] = {'stringValue':''}
        if kind == 'PDF':
            cells[10]['userEnteredValue'] = {'stringValue':'結合しない（下書き）'}
            cells[11]['userEnteredValue'] = {'stringValue':'p1 / p2 の区切りを原本で再確認'}
        items = None; canonical = None; version = None
        if alias == 'SYN-H301':
            case = Case(alias, 'synthetic-unmapped-history', {}, [], 'synthetic', 'medical',
                        synthetic=True, disposition=LEGACY_UNMAPPED_HOLD)
            purpose = 'RECONCILIATION_REVIEW'
        else:
            native_kind = {'一般': 'general_receipt_item_review', '医療': 'medical', 'PDF': 'grouping'}[kind]
            case = clean(alias, kind=native_kind, pages=[1, 2] if kind == 'PDF' else [1])
            case.fields += [['payment', '支払方法', '現金'], ['memo', 'メモ', alias + ' native途中メモ']]
            if alias == 'SYN-G103':
                case.authority_status = 'held'
            if kind == 'PDF':
                case.identity.update(receipt_unit_id='', receipt_index=0)
            if kind == '一般':
                # Existing native snapshot representation, independently saved
                # item IDs/categories, NEVER inferred from receipt category.
                entries = [dict(item_id=digest([alias, n]), item_index=n, kind='product',
                                name=alias+' 合成明細 '+str(n), amount=100*n,
                                category=category) for n, category in enumerate(['食費｜食料品', '日用品｜消耗品'], 1)]
                fields = dict(date='2026-10-18', amount=300, merchant=alias+' 合成店舗',
                              payment='現金', memo=alias+' 明細別メモ', action='未選択', items=entries)
                view = dict(identity=deepcopy(case.identity), token=digest([alias, 'native-token']),
                            rows=[[k, k, fields[k]] for k in ('date', 'amount', 'merchant', 'payment', 'memo', 'action')])
                items = snapshot(view, fields)
                case.fields = deepcopy(items['rows'])
            canonical = from_case(case, {'一般':'normal', '医療':'medical', 'PDF':'grouping'}[kind])
            if kind == 'PDF':
                canonical['inputs'] = {k: (v if k in {'partition', 'memo'} else '')
                                       for k,v in canonical['inputs'].items()}
            canonical['hold'] = 'held' if alias in HELD else ''
            purpose = canonical['purpose']
            source = next(s for s in sources if s['case_id'] == alias)
            source.update(source_id=case.source_id, sha256=case.identity['source_content_hash'])
            cells[2] = {'userEnteredValue': {'formulaValue': original_formula(alias, source, kind)}}
            cells[15:17] = [text(source['sha256']), text(source['source_id'])]
            observed = dict(source_id=source['source_id'], sha256=source['sha256'],
                            drive_revision_id=source['source_version'], native_source_version=case.identity['source_version'])
            version = asdict(bind_versions(lambda: deepcopy(observed), source, case.identity))
        native_digest = digest(current_snapshot(case))
        mappings[alias] = dict(owner=OWNER, case=asdict(case), native_case_id=case.case_id,
            native_digest=native_digest, canonical=canonical, purpose=purpose, version_binding=version,
            sheet_binding=None if alias in HELD else asdict(bind_case(alias, canonical, native_digest)),
            input_digest=digest(cells), item_snapshot=items, item_digest=digest(items) if items else None)
    contract = issue_contract(raw, sources)
    bundle = dict(schema='iphone-saved-display-v1', contract=contract, raw_snapshot=raw,
                  sources={s['source_id']:s for s in sources}, mappings=mappings)
    return bundle, validate_snapshot(contract, raw, bundle['sources'])


class ReadonlyRig:
    def __init__(self, monkeypatch, tmp_path, *, enabled=True):
        tmp_path.mkdir(parents=True, exist_ok=True)
        self.h = HostRig(monkeypatch, enabled=False)
        self.bundle, self.batch = saved_bundle()
        self.bundle_path = tmp_path/'display.json'
        self.bundle_path.write_text(json.dumps(self.bundle, ensure_ascii=True), encoding='utf-8')
        self.journal = LocalJournal(tmp_path/'drafts.json', self.batch.contract_digest)
        _, tag = self.journal.initialize()
        self.journal.save(self.batch, tag, str(uuid.uuid4()))  # offline provisioning ONLY
        self.repository = self.reopen()
        self.h.app = self.application(enabled=enabled)
        self.h.app.testing = True
        self.h.client = self.h.app.test_client()

    def reopen(self):
        return SavedDraftRepository(LocalJournal(self.journal.path, self.batch.contract_digest),
                                    self.bundle_path, digest(self.bundle), OWNER)

    def application(self, *, enabled=True):
        h = self.h
        return make_isolated_readonly_application(h.web.make_application, h.configured, self.repository,
            config=ShadowConfig(enabled, 'isolated_synthetic'),
            cookies={'login':h.web.LOGIN_COOKIE, 'ticket':h.web.COOKIE})

    def repin_for_negative_semantic_test(self):
        # Test-only manager tampering: bypass the digest guard so the deeper
        # source/page/Unit/purpose checks themselves have negative evidence.
        self.bundle_path.write_text(json.dumps(self.bundle, ensure_ascii=True), encoding='utf-8')
        self.repository.bundle_digest = digest(self.bundle)
