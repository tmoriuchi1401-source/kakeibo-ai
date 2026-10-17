"""Saved one-tab drafts on the existing host, isolated synthetic GETs only.

No live readers, native services, POST handlers, auth provider or write ports.
The private bundle is provisioned offline and pinned by the composition owner.
It is evidence for display, never permission or a freshness claim about Drive.
"""
from copy import deepcopy
from dataclasses import asdict
import json
from pathlib import Path

from flask import Blueprint, Response, render_template, request

from .drive_run_state import StateError
from .iphone_draft_journal import LocalJournal, MAX_BYTES
from .iphone_persistent_drafts import PersistentJournal
from .iphone_source_observation import SourceObservation, verify_saved
from .iphone_native_dry_run import (KINDS, VersionBinding, bind_case,
                                    check_version_binding)
from .iphone_review_drafts import check_row
from .iphone_review_preview import check_native_case
from .iphone_sheet_reader import CASE_IDS, HELD, raw_value, validate_snapshot
from .unified_receipt_eligibility import current_snapshot
from .unified_receipt_purposes import PurposeBoundary
from .unified_receipt_review import Case, LEGACY_UNMAPPED_HOLD, digest
from .unified_receipt_shadow import ExistingHostSessionGate, ShadowConfig

PREFIX = '/isolated-synthetic/iphone-drafts'
BLUEPRINT = 'iphone_saved_readonly'
LABELS = {3: 'シート内の下書き状態（権限ではありません）', 4: '日付', 5: '店舗・施設',
          6: '金額', 7: 'カテゴリ（下書き）', 8: '医療の支払方法', 10: 'PDF結合判断',
          11: 'PDF分割判断', 12: '途中メモ'}


def deny(reason):
    raise StateError('iphone_readonly_' + reason)


class SavedDraftRepository:
    """Bounded saved reads, existing journal validation, pure saved projection.

    bundle_digest is trusted configuration, not a browser/Sheet parameter.
    A newly saved batch needs a new reviewed bundle before it can be displayed.
    Missing, stale or damaged state is never repaired or initialized on GET.
    """
    def __init__(self, journal, bundle_path, bundle_digest, owner):
        if type(journal) not in {LocalJournal, PersistentJournal}:
            deny('local_journal_required')
        if type(journal) is PersistentJournal and journal.scope['owner'] != owner:
            deny('case_owner_mismatch')
        self.journal, self.path = journal, Path(bundle_path)
        self.bundle_digest, self.owner = bundle_digest, owner

    def snapshots(self):
        try:
            with self.path.open('rb') as handle:
                raw = handle.read(MAX_BYTES + 1)
            if len(raw) > MAX_BYTES:
                deny('bundle_capacity')
            bundle = json.loads(raw)
            if (digest(bundle) != self.bundle_digest or set(bundle) != {
                    'schema', 'contract', 'raw_snapshot', 'sources', 'mappings'}
                    or bundle['schema'] != 'iphone-saved-display-v1'):
                deny('saved_bundle_changed')
            before, tag = self.journal.read_versioned()
            # This validates SAVED API payloads; it performs no observation/IO.
            batch = validate_snapshot(bundle['contract'], bundle['raw_snapshot'], bundle['sources'])
            if before['batch'] != asdict(batch):
                deny('stale_saved_snapshot')
            mappings = bundle['mappings']
            if type(self.journal) is PersistentJournal and self.journal.scope['owner'] != self.owner:
                deny('case_owner_mismatch')
            if type(self.journal) is PersistentJournal:
                details, detail_tag = self.journal.read_details()
                expected_details = {cid:{k:m[k] for k in ('source_observation', 'item_draft') if k in m}
                                    for cid,m in mappings.items()}
                if detail_tag != tag or details != expected_details:
                    deny('stale_saved_snapshot')
            if set(mappings) != CASE_IDS:
                deny('mapping_set_changed')
            result, native_ids = [], set()
            for draft in batch.drafts.values():
                cid = draft['case_id']; m = mappings[cid]
                if m['owner'] != self.owner:
                    deny('case_owner_mismatch')
                case = Case(**m['case'])
                if (m['native_case_id'] != case.case_id or case.case_id in native_ids
                        or m['input_digest'] != draft['input_snapshot_digest']
                        or m['native_digest'] != digest(current_snapshot(case))
                        or case.synthetic is not True):
                    deny('identity_or_snapshot_changed')
                native_ids.add(case.case_id)
                purpose = 'RECONCILIATION_REVIEW' if cid == 'SYN-H301' else (
                    'STRUCTURE_REVIEW' if draft['kind'] == 'PDF' else 'ACCOUNTING_POST')
                if m['purpose'] != purpose:
                    deny('purpose_mismatch')
                if cid == 'SYN-H301':
                    if (case.disposition != LEGACY_UNMAPPED_HOLD or case.identity != {}
                            or m['canonical'] is not None or m['version_binding'] is not None
                            or m['sheet_binding'] is not None or m['item_snapshot'] is not None):
                        deny('legacy_mapping_forbidden')
                else:
                    canonical = check_row(m['canonical'])
                    if canonical['kind'] != KINDS[draft['kind']]:
                        deny('kind_mismatch')
                    check_native_case(canonical, case, m['native_digest'])
                    check_version_binding(VersionBinding(**m['version_binding']),
                                          draft['identity'], canonical['identity'])
                    if type(self.journal) is PersistentJournal:
                        observation = SourceObservation(**m['source_observation'])
                        verify_saved(observation, draft['identity'], canonical['identity'],
                                     purpose, draft['input_snapshot_digest'])
                        if asdict(observation.binding()) != m['version_binding']:
                            deny('identity_or_snapshot_changed')
                    # Held G103 keeps its actual identity, but cannot be bound
                    # to an executable native operation by bind_case().
                    if cid == 'SYN-G103':
                        if (not canonical['hold'] or case.authority_status != 'held'
                                or m['sheet_binding'] is not None):
                            deny('hold_required')
                    elif (canonical['hold'] or m['sheet_binding'] != asdict(
                            bind_case(cid, canonical, m['native_digest']))):
                        deny('case_binding_changed')
                items = m['item_snapshot']
                if draft['kind'] == '一般':
                    if (not isinstance(items, dict) or digest(items) != m['item_digest']
                            or items['rows'] != case.fields or not items['items']):
                        deny('item_snapshot_changed')
                    identity = items['identity']; native = case.identity
                    if any(identity[k] != native[k] for k in (
                            'source_file_id', 'source_content_hash', 'stable_page_identity',
                            'receipt_unit_id', 'receipt_index')):
                        deny('item_identity_changed')
                    if type(self.journal) is PersistentJournal:
                        # Exact saved item contract is display evidence only;
                        # native validation occurs offline, never on GET.
                        item_draft = m['item_draft']
                        if (item_draft['snapshot_digest'] != digest(items)
                                or item_draft['native_snapshot'] != items
                                or digest(item_draft) != m['item_draft_digest']
                                or [e['old'] for e in item_draft['entries']] != items['items']
                                or any(e['identity'] != items['identity'] or e['token'] != items['token']
                                       or e['marker'] != 'item:'+e['item_id']
                                       or e['item_id'] != e['old']['item_id']
                                       or e['original_link'] != items['original_link'] for e in item_draft['entries'])):
                            deny('item_snapshot_changed')
                elif items is not None or m['item_digest'] is not None:
                    deny('item_purpose_mismatch')
                held = cid in HELD or bool(m['canonical'] and m['canonical']['hold'])
                result.append(dict(case_id=cid, purpose=purpose, state='held' if held else 'saved_draft',
                    disposition=LEGACY_UNMAPPED_HOLD if cid == 'SYN-H301' else '',
                    display=dict(native_case_id=case.case_id, kind=draft['kind'], held=held,
                        original_url='' if draft['identity'] is None else
                            'https://drive.google.com/file/d/' + draft['identity']['source_id'] + '/view',
                        source_identity=deepcopy(draft['identity']),
                        native_identity=deepcopy(case.identity), input_digest=m['input_digest'],
                        native_digest=m['native_digest'],
                        inputs=[dict(label=label, value=raw_value(draft['cells'][col]))
                                for col, label in LABELS.items()
                                if col in ({3, 10, 11, 12} if draft['kind'] == 'PDF' else
                                           {3, 4, 5, 6, 7, 8, 12} if draft['kind'] == '医療' else
                                           {3, 4, 5, 6, 7, 12})],
                        items=deepcopy(items['items']) if items else [],
                        item_entries=deepcopy(m['item_draft']['entries']) if items and type(self.journal) is PersistentJournal else [],
                        manual_only=draft['kind'] == '医療',
                        accounting_allowed=False, authorized=False), operation_targets=[]))
            # Atomic journal reads do not make a transaction with the bundle.
            # Detect observed races; otherwise display the pinned old view.
            if self.journal.read_versioned()[1] != tag:
                deny('concurrent_saved_snapshot')
            return result
        except StateError:
            raise
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            deny('invalid_saved_state')


def make_isolated_readonly_application(make_application, runtime_factory, repository, *,
                                       config=ShadowConfig(), cookies=None):
    """Compose AFTER the unchanged host factory. No production registration.

    The existing synthetic host retains its login/logout endpoints. The new
    namespace registers exactly two GETs; no acceptance or prepare/submit.
    """
    app = make_application()
    if config.enabled is not True:
        return app
    if config.environment != 'isolated_synthetic' or cookies is None:
        deny('isolation_required')
    rt = runtime_factory()
    if (rt.settings.get('mode') != 'synthetic_only'
            or getattr(rt.client, 'shadow_memory_only', False) is not True
            or type(repository) is not SavedDraftRepository):
        deny('isolation_required')
    gate = ExistingHostSessionGate(runtime_factory, cookies)
    boundary = PurposeBoundary(repository.owner, gate.post, gate.read, repository.snapshots,
                               enabled=False, clock=rt.clock)
    bp = Blueprint(BLUEPRINT, __name__, url_prefix=PREFIX, template_folder='unified_shadow_templates')

    @bp.before_request
    def safe_read():
        # Ordinary navigation has no Origin. Explicit cross-origin requests,
        # including null and cross-site fetches, must fail before reading state.
        if (request.headers.get('Origin') not in (None, rt.origin)
                or request.headers.get('Sec-Fetch-Site') == 'cross-site'):
            deny('origin_invalid')

    @bp.errorhandler(StateError)
    def rejected(error):
        code = str(error)
        status = 410 if 'expired' in code else 403 if (
            code.startswith('login_session_') or 'authenticated' in code or code in {
                'iphone_readonly_case_owner_mismatch', 'iphone_readonly_origin_invalid'}) else 409
        return Response(render_template('iphone_readonly.html', cases=[], detail=False,
            message='表示できません。本人確認の期限・権限または保存済みsnapshotを再確認してください。'), status=status)

    @bp.get('')
    def listing():
        return render_template('iphone_readonly.html', cases=boundary.get_list({}), detail=False, message='')

    @bp.get('/cases/<case_id>')
    def detail(case_id):
        return render_template('iphone_readonly.html', cases=[boundary.get_detail(case_id, {})], detail=True, message='')

    if (BLUEPRINT in app.blueprints or any(r.rule == PREFIX or r.rule.startswith(PREFIX + '/')
                                          for r in app.url_map.iter_rules())):
        deny('route_collision')
    app.register_blueprint(bp)
    return app
