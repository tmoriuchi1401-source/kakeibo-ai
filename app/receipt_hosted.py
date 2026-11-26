"""Single receipt owner using the existing private Google receipt store.

No credential issuer, scheduler, local fence, new infrastructure, or live
financial activation. All side effects are preceded by a read-back durable
intent. The existing Actions lock is REQUIRED; this store is not a CAS lock.
"""
from copy import deepcopy
from hashlib import sha256
import json
import re

from .drive_run_state import StateError
from .models import ReceiptItem, ReceiptResult
from .page_receipt_model import digest
from .receipt_pipeline import ReceiptPipeline
from .receipt_privacy_gate import evaluate_receipt_privacy
from .receipt_review_cases import (make_case, make_row, preview, request_id,
                                   source_identity, validate_snapshot)
from .receipt_review_snapshot import candidate_rows
from .receipt_validation import validate_receipt_result
from .utils import canonical_hash

KEY = 'receipt_hosted_v1'
BUSY = {'ai_started', 'review_started', 'writer_started', 'publishing'}
STATES = BUSY | {'analyzed', 'review', 'held', 'validated_unposted', 'synthetic_complete', 'posted'}


class ReceiptOwner:
    def __init__(self, store, *, financial_apply=False):
        self.store = store
        self.financial_apply = financial_apply
        self.read()

    def read(self):
        value = self.store.value.get(KEY)
        base = {'schema', 'enabled', 'financial_write_enabled', 'records', 'requests', 'stopped'}
        fences = {'legacy_source_fences', 'legacy_hash_fences'}
        if (type(value) is not dict or set(value) not in (base, base | fences)
                or value['schema'] != KEY or value['enabled'] is not True
                or type(value['financial_write_enabled']) is not bool
                or value['financial_write_enabled'] and not self.financial_apply
                or type(value['records']) is not dict or type(value['requests']) is not dict
                or type(value['stopped']) is not bool):
            raise StateError('receipt_hosted_state_missing_or_invalid')
        for key, pattern in (('legacy_source_fences', r'[A-Za-z0-9_-]{1,150}'),
                             ('legacy_hash_fences', r'[a-f0-9]{64}')):
            if key in value and (type(value[key]) is not list
                    or any(type(v) is not str or not re.fullmatch(pattern, v) for v in value[key])
                    or len(set(value[key])) != len(value[key])):
                raise StateError('receipt_hosted_legacy_fence_inventory_invalid')
        hashes = set()
        for sid, record in value['records'].items():
            source_identity(record['source'])
            if (sid != record['source']['source_id'] or record['state'] not in STATES
                    or record['source']['sha256'] in hashes):
                raise StateError('receipt_hosted_state_invalid')
            hashes.add(record['source']['sha256'])
        for rid, record in value['requests'].items():
            request_id(rid)
            if (record['state'] not in {'started', 'complete'}
                    or record['source_id'] not in value['records']
                    or len(record['snapshot_digest']) != 64):
                raise StateError('receipt_hosted_request_invalid')
        if (value['stopped'] or any(r['state'] in BUSY for r in value['records'].values())
                or any(r['state'] == 'started' for r in value['requests'].values())):
            raise StateError('receipt_hosted_unknown_no_retry')
        return deepcopy(value)

    def save(self, value):
        # ReimportStore.save already checks the old payload, writes once and
        # independently reads the same Drive file back. Preserve all old keys.
        whole = deepcopy(self.store.value)
        old = whole.get(KEY, {})
        for key in ('legacy_source_fences', 'legacy_hash_fences'):
            if key in old and (type(value.get(key)) is not list or not set(old[key]) <= set(value[key])):
                raise StateError('receipt_hosted_legacy_fence_removal_forbidden')
        whole[KEY] = deepcopy(value)
        self.store.save(whole)


class IsolatedReceiptWriter:
    """Exercise the unchanged materializer against its existing PlanningDB.

    Only this explicit, in-memory sink is provided in this change. There is no
    flag that converts an unposted request into live financial authority.
    """
    def __init__(self, categories):
        from .receipt_plan.planning import PlanningDB
        self.db = PlanningDB(categories)

    def write(self, parsed, sid, link):
        categories = self.db.categories()
        valid, _ = validate_receipt_result(parsed, categories)
        if not valid or parsed.transaction_kind != 'purchase':
            raise StateError('receipt_hosted_invalid_post')
        from .receipt_plan.planning import PlanningDB
        expected = PlanningDB(categories)
        clock = lambda: '2026-01-01 00:00:00'
        ReceiptPipeline(expected, None, clock=clock)._materialize_result(parsed, sid, link, [])
        ReceiptPipeline(self.db, None, clock=clock)._materialize_result(parsed, sid, link, [])
        # Independent read of the memory ledger, including every field/ID.
        actual = [(t, r) for t, r in self.db.plan if r[0] in {r[0] for _, r in expected.plan}]
        if actual != expected.plan:
            raise StateError('receipt_hosted_readback_mismatch')
        return digest(expected.plan)


class HostedReceiptPipeline:
    def __init__(self, store, pipeline, *, sources, verify_source, review=None,
                 writer=None, duplicate_check=None, native_apply=False, pdf_owner_check=None):
        self.owner = ReceiptOwner(store, financial_apply=native_apply)
        self.pipeline, self.sources = pipeline, sources
        self.verify_source, self.review = verify_source, review
        from .receipt_hosted_writer import NativeReceiptWriter
        if writer is not None and type(writer) not in {IsolatedReceiptWriter, NativeReceiptWriter}:
            raise StateError('receipt_hosted_live_write_disabled')
        self.writer, self.duplicate_check = writer, duplicate_check
        self.native_apply = native_apply
        self.pdf_owner_check = pdf_owner_check

    def _fresh(self, source):
        if self.verify_source(deepcopy(source)) is not True:
            raise StateError('receipt_hosted_source_changed')

    def process_bytes(self, raw, mime, sid, image_url='', *, known_source_classification=None):
        state = self.owner.read()
        if (sid in state.get('legacy_source_fences', [])
                or sha256(raw).hexdigest() in state.get('legacy_hash_fences', [])):
            raise StateError('receipt_hosted_legacy_attempt_fenced')
        before = self.sources[sid]
        source = dict(source_id=sid, version=before['version'], sha256=sha256(raw).hexdigest(),
                      mime_type=mime, classification='normal', transaction_kind='purchase')
        source_identity(source)
        if before['sha256'] != source['sha256'] or before['mime_type'] != mime:
            raise StateError('receipt_hosted_source_changed')
        self._fresh(source)
        if mime == 'application/pdf':
            from .pdf_intake_production import page_count
            if page_count(raw) != 1:
                return dict(status='privacy_blocked', reason='whole_source_manual_structure_required')
            if self.pdf_owner_check is not None and self.pdf_owner_check(source) is not True:
                return dict(status='privacy_blocked', reason='native_pdf_owner_or_unconfirmed')
        gate = evaluate_receipt_privacy(raw, mime, known_source_classification=known_source_classification)
        if gate.classification != 'normal' or not gate.gemini_allowed or gate.buyback_evidence:
            return dict(status='privacy_blocked', classification=gate.classification)
        # Old native receipts/holds are never silently re-owned by a new UUID.
        for old in self.owner.store.value.get('confirmation_items', {}).values():
            historical = old.get('source', {})
            if historical.get('source_id') == sid or historical.get('sha256') == source['sha256']:
                raise StateError('receipt_hosted_historical_owner_conflict')
        if sid in state['records']:
            record = state['records'][sid]
            if record['source'] != source:
                raise StateError('receipt_hosted_source_changed')
            if record['state'] != 'analyzed':
                return dict(status='needs_review' if record['state'] in {'review', 'held'} else 'skipped',
                            reason='durable_receipt_exists', accounting_readback_complete=False)
        else:
            if any(r['source']['sha256'] == source['sha256'] for r in state['records'].values()):
                raise StateError('receipt_hosted_duplicate_content')
            legacy = self.owner.store.value.get('records', {}).get(sid)
            legacy_source = next((s for s in self.owner.store.value.get('manifest', {}).get('sources', [])
                                  if s.get('source_id') == sid), None)
            if legacy is not None and legacy.get('phase') in {'requested', 'failed'}:
                raise StateError('receipt_hosted_historical_unknown')
            if legacy is not None and legacy.get('phase') == 'complete':
                if (legacy_source is None or any(legacy_source.get(k) != source[k]
                        for k in ('source_id', 'sha256', 'version', 'mime_type'))):
                    raise StateError('receipt_hosted_historical_source_changed')
                record = dict(source=source, state='analyzed',
                              parsed=ReceiptResult.model_validate(legacy['parsed']).model_dump())
                state['records'][sid] = record
                self.owner.save(state)
                return self.process_bytes(raw, mime, sid, image_url,
                    known_source_classification=known_source_classification)
            # Google reservation BEFORE AI. Death/transport failure leaves busy
            # on Google, so a fresh runner cannot resend or mint another case.
            record = dict(source=source, state='ai_started')
            state['records'][sid] = record
            self.owner.save(state)
            categories = self.pipeline.db.categories()
            parsed = self.pipeline._analyze(raw, mime, categories,
                dict(known_source_classification=known_source_classification))
            self._fresh(source)
            record.update(state='analyzed', parsed=parsed.model_dump())
            self.owner.save(state)
        parsed = ReceiptResult.model_validate(record['parsed'])
        categories = self.pipeline.db.categories()
        valid, issues = validate_receipt_result(parsed, categories)
        if parsed.transaction_kind != 'purchase':
            # Do not mint purchase identity/authority from an unknown/buyback
            # analysis. Its native income/manual handling is a separate unit.
            record.update(state='held', issues=['purchase_required'])
            self.owner.save(state)
            return dict(status='privacy_blocked', classification='transaction_kind_unresolved')
        if self.duplicate_check is not None and self.duplicate_check(source, parsed):
            valid = False
            issues.append('existing_receipt_duplicate')
        if valid:
            return self._post(state, record, parsed, image_url)
        if self.review is None:
            # Preserve analyzed output on Google; connecting a review transport
            # later must reuse it, never call AI to create a case again.
            raise StateError('receipt_hosted_pat_free_review_connection_required')
        iid = 'receipt:' + sid
        imported = [iid, self.pipeline.clock(), 'receipt', sid, parsed.date, parsed.merchant,
                    parsed.total, parsed.payment_method, '要確認', '', canonical_hash(parsed.model_dump()), '; '.join(issues)]
        entry = make_case(source, imported, candidate_rows(iid, imported[10], parsed), categories)
        record.update(state='review_started', entry=entry)
        self.owner.save(state)
        self.review.intake(entry, make_row(entry))
        if self.review.lookup_source(sid, source['sha256']) != entry:
            raise StateError('receipt_hosted_review_readback_unknown')
        record['state'] = 'review'
        self.owner.save(state)
        return dict(status='needs_review', accounting_readback_complete=False)

    def _post(self, state, record, parsed, link):
        self._fresh(record['source'])
        if (not validate_receipt_result(parsed, self.pipeline.db.categories())[0]
                or self.duplicate_check is not None and self.duplicate_check(record['source'], parsed)):
            raise StateError('receipt_hosted_prewrite_revalidation_failed')
        if self.writer is None:
            record['state'] = 'validated_unposted'
            self.owner.save(state)
            return dict(status='skipped', reason='financial_write_off', accounting_readback_complete=False)
        from .receipt_hosted_writer import NativeReceiptWriter
        if type(self.writer) is NativeReceiptWriter:
            plan = self.writer.prepare(parsed, record['source'], link)
            record.update(native_plan=plan, state='validated_unposted')
            self.owner.save(state)
            if not self.native_apply or state['financial_write_enabled'] is not True:
                return dict(status='skipped', reason='financial_write_off', accounting_readback_complete=False)
            record['state'] = 'writer_started'
            self.owner.save(state)
            def verify_intent(expected):
                if (self.owner.store.transport.read() != self.owner.store.payload
                        or state['stopped'] or record['state'] != 'writer_started'
                        or record['native_plan'] != expected):
                    raise StateError('receipt_native_durable_intent_changed')
                self._fresh(record['source'])
                rid = record.get('active_request')
                if rid:
                    frozen = self.review.read(rid)
                    if (frozen['entry'] != record['entry'] or frozen['row'][7:9] != ['検証中', '']
                            or frozen['row'][6] != state['requests'][rid]['snapshot_digest']):
                        raise StateError('receipt_native_snapshot_changed')
            record['native_readback'] = self.writer.apply(plan, verify_intent=verify_intent)
            self._fresh(record['source'])
            record['state'] = 'posted'
            self.owner.save(state)
            return dict(status='imported', accounting_readback_complete=True,
                        financial_write_count=len(plan['rows']), **record['native_readback'])
        record['state'] = 'writer_started'
        self.owner.save(state)
        record['synthetic_readback'] = self.writer.write(parsed, record['source']['source_id'], link)
        self._fresh(record['source'])
        record['state'] = 'synthetic_complete'
        self.owner.save(state)
        return dict(status='skipped', reason='synthetic_complete', accounting_readback_complete=False)

    def register(self, rid):
        """Consume one existing immutable registration; no current UI read/AI."""
        request_id(rid)
        state = self.owner.read()
        if rid in state['requests']:
            raise StateError('receipt_hosted_registration_replay')
        record = next((r for r in state['records'].values()
                       if r.get('entry', {}).get('request_id') == rid), None)
        if record is None or record['state'] != 'review':
            raise StateError('receipt_hosted_registration_missing')
        entry = record['entry']
        frozen = self.review.read(rid)
        row = frozen['row']
        snapshot = json.loads(row[5])
        if (frozen['entry'] != entry or len(row) != 10 or row[0] != rid
                or row[1:5] != [entry['case_id'], entry['source']['source_id'],
                               entry['source']['version'], entry['source']['sha256']]
                or row[7:9] != ['受付済み', ''] or digest(snapshot) != row[6]):
            raise StateError('receipt_hosted_registration_binding')
        self._fresh(entry['source'])
        validation = validate_snapshot(snapshot, entry)
        if self.duplicate_check is not None:
            parsed_before = ReceiptResult.model_validate(record['parsed'])
            if self.duplicate_check(entry['source'], parsed_before):
                validation.update(state='保留', issues=['existing_receipt_duplicate'])
        state['requests'][rid] = dict(state='started', source_id=entry['source']['source_id'], snapshot_digest=row[6])
        self.owner.save(state)
        claimed = self.review.claim(rid, row)
        if claimed[:7] != row[:7] or claimed[7:9] != ['検証中', '']:
            raise StateError('receipt_hosted_claim_unknown')
        if self.review.read(rid) != dict(entry=entry, row=claimed):
            raise StateError('receipt_hosted_snapshot_changed')
        posted = None
        if validation['state'] == '検証済み・未記帳':
            corrected = preview(entry, snapshot['operations'], total_confirmed=True)
            original = ReceiptResult.model_validate(record['parsed'])
            parsed = original.model_copy(update={'items': [ReceiptItem(name=i['name'], quantity=i['quantity'],
                amount=i['amount'], major_category=i['category'][0], minor_category=i['category'][1])
                for i in corrected['items']]})
            if not validate_receipt_result(parsed, self.pipeline.db.categories())[0]:
                validation.update(state='修正が必要', issues=['current_category_or_amount_invalid'])
            else:
                record['active_request'] = rid
                posted = self._post(state, record, parsed, make_row(entry)[2])
        result = dict(request_id=rid, snapshot_digest=row[6], state=validation['state'],
                      issues=validation['issues'], posting_authority=False, financial_write_count=0)
        for key in ('warnings', 'validator_revision', 'receipt_total', 'item_total', 'difference'):
            if key in validation:
                result[key] = validation[key]
        if posted and posted.get('accounting_readback_complete') is True:
            result.update(state='記帳済み', financial_write_count=posted['financial_write_count'],
                          exact_readback=True, plan_digest=posted['plan_digest'])
        record['state'] = 'publishing'
        self.owner.save(state)
        finished = self.review.finish(rid, claimed, result)
        actual = self.review.read(rid)
        if (actual != dict(entry=entry, row=finished) or finished[:7] != row[:7]
                or finished[7] != result['state'] or json.loads(finished[8]) != result):
            raise StateError('receipt_hosted_result_unknown')
        record['state'] = ('posted' if 'native_readback' in record else 'synthetic_complete'
            if 'synthetic_readback' in record else 'held' if result['issues'] else 'validated_unposted')
        state['requests'][rid]['state'] = 'complete'
        state['requests'][rid]['result'] = deepcopy(result)
        self.owner.save(state)
        # Only a definite validation failure with zero writer attempts may be
        # rearmed. Keep the old UUID/snapshot/result immutable in both stores.
        if (result['issues'] and 'native_plan' not in record and 'synthetic_readback' not in record
                and 'existing_receipt_duplicate' not in result['issues']
                and hasattr(self.review, 'rearm')):
            from uuid import uuid4
            replacement = deepcopy(entry)
            replacement['request_id'] = str(uuid4())
            record.update(state='review_started', next_entry=replacement)
            self.owner.save(state)
            self.review.rearm(rid, finished, replacement)
            if self.review.lookup_source(entry['source']['source_id'], entry['source']['sha256']) != replacement:
                raise StateError('receipt_hosted_rearm_unknown')
            record.setdefault('entry_history', []).append(deepcopy(entry))
            record.update(state='review', entry=replacement)
            record.pop('next_entry')
            self.owner.save(state)
        return result

    def process_pending(self):
        state = self.owner.read()
        if self.review is None:
            if any(r['state'] == 'review' for r in state['records'].values()):
                raise StateError('receipt_hosted_pat_free_review_connection_required')
            return 0
        pending = self.review.pending()
        if type(pending) is not list or len(set(pending)) != len(pending):
            raise StateError('receipt_hosted_pending_invalid')
        for rid in pending[:1]:
            self.register(rid)
        return min(len(pending), 1)

    def process_multipage(self, settings, plans, env):
        """Keep the existing page/Unit engine and its purpose/privacy gates.

        Native PDF authority is not converted into whole-source permission.
        An unavailable native connection holds these PDFs only. During this
        phase native page writes and original archival remain disabled.
        """
        from .pdf_intake_production import Context, process
        state = self.owner.read()
        legacy = self.owner.store.value.get('confirmation_items', {}).values()
        safe, held = [], 0
        for source in plans:
            conflicts = [r['source'] for r in state['records'].values()]
            conflicts += [r.get('source', {}) for r in legacy]
            if any(s.get('source_id') == source['source_id'] or s.get('sha256') == source['sha256']
                   for s in conflicts):
                held += 1
            else:
                safe.append(source)
        counts = dict(needs_review=held)
        if safe:
            try:
                context = Context(env, settings, self.pipeline.db, writable=True, remote_proof=False)
            except Exception:
                counts['needs_review'] += len(safe)
                return counts
            native = process(context, settings, safe, apply=True, financial_write=False)
            for key, value in native.items(): counts[key] = counts.get(key, 0) + value
        return counts


def open_pipeline(settings, pipeline, sources, env):
    """Existing hosted entry. Owner review connected; native financial apply OFF."""
    from .production_flow import verify_execution_boundary
    verify_execution_boundary(env, env.get('GITHUB_SHA', ''))
    if env.get('RUNNER_ENVIRONMENT') != 'github-hosted' or env.get('RUNNER_OS') != 'Linux':
        raise StateError('receipt_hosted_runner_required')
    from .receipt_confirmation_production import open_context
    _, store, _, verify = open_context(env, apply=True)
    if not {'legacy_source_fences', 'legacy_hash_fences'} <= store.value.get(KEY, {}).keys():
        raise StateError('receipt_hosted_legacy_fence_inventory_required')
    from .receipt_hosted_review import open_review
    from .drive_receipts import normalize_folder_id
    review = open_review(env, store.value['manifest'], settings.spreadsheet_id,
                         normalize_folder_id(settings.receipt_drive_folder_id))
    from .drive_run_state import StateBinding, DriveStateTransport
    from .google_clients import read_only_drive_service
    from .production_ledger import ProductionLedger
    from .receipt_hosted_writer import NativeReceiptWriter, ProductionReceiptAuthority
    binding = StateBinding('production_run', settings.spreadsheet_id,
        env['KAKEIBO_STATE_FOLDER_ID'], env['KAKEIBO_RUN_LEDGER_FILE_ID'])
    ledger = ProductionLedger(DriveStateTransport(read_only_drive_service(), binding), binding)
    writer = NativeReceiptWriter(pipeline.db, ProductionReceiptAuthority(env, ledger), clock=pipeline.clock)
    by_id = {s['source_id']: s for s in sources}
    def fresh(source):
        verify(source, settings.receipt_drive_folder_id)
        return True
    from .receipt_plan.planning import check_duplicates
    def duplicate(source, parsed):
        try:
            check_duplicates(pipeline.db, source['source_id'], parsed)
            return False
        except StateError:
            return True
    def pdf_owner_check(source):
        from .pdf_intake_production import Context
        try:
            context = Context(env, settings, pipeline.db, writable=False, remote_proof=False)
            pages = context.store.load()['pages'].values()
            return not any(r['page']['source']['source_file_id'] == source['source_id']
                or r['page']['source']['source_content_hash'] == source['sha256'] for r in pages)
        except Exception:
            return False  # Hold this PDF only; no whole-document AI fallback.
    return HostedReceiptPipeline(store, pipeline, sources=by_id, verify_source=fresh,
                                 duplicate_check=duplicate, review=review, writer=writer,
                                 native_apply=False, pdf_owner_check=pdf_owner_check)
