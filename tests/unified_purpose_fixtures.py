"""Existing hash-pinned validators with memory IO; no deployed service imports."""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
from hashlib import sha256
import importlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
from types import ModuleType, SimpleNamespace
from uuid import uuid4

from app.pdf_page_identity import page_identity
from app.unified_receipt_review import digest
from app.unified_receipt_purposes import (ReviewObservation, ExistingReviewPort, PurposeBoundary,
    VerifiedPost, VerifiedRead, STRUCTURE_REVIEW, METADATA_REVIEW, RECONCILIATION_REVIEW)
from app.unified_receipt_write import ConditionalRequests
from test_unified_receipt_write import Memory

NOW = 1791700000
OWNER = digest(['https://accounts.google.com', '101'])
ROOT = Path(__file__).resolve().parents[1]
_temporary = None
_native = None


def native_modules():
    global _temporary, _native
    if _native is None:
        spec = importlib.util.spec_from_file_location('purpose_pinned_bundle', ROOT / 'auth-session/compose.py')
        bundle = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(bundle)
        _temporary = tempfile.TemporaryDirectory(prefix='unified-native-contracts-')
        destination = Path(_temporary.name) / 'fixture'
        bundle.compose(destination, test_tree=True)
        package = ModuleType('app._unified_pinned')
        package.__path__ = [str(destination / 'app')]
        sys.modules[package.__name__] = package
        names = ('receipt_audit', 'receipt_reconciliation_authority', 'human_general_auth_transport',
                 'pdf_grouping_authority', 'pdf_page_kind', 'receipt_pdf_units', 'receipt_pdf_grouping',
                 'human_general_authority', 'page_receipt_model')
        _native = SimpleNamespace(**{name: importlib.import_module(package.__name__ + '.' + name) for name in names})
    return _native


def source(count=2):
    raw = b'%PDF-isolated-purpose-fixture-source-v1'
    return dict(source_file_id='isolated-purpose-source', source_content_hash=sha256(raw).hexdigest(), source_version='1', page_count=count), raw


def pages(s):
    return [dict(page_number=n, stable_page_identity=page_identity(s['source_content_hash'], n, s['page_count']))
            for n in range(1, s['page_count'] + 1)]


def gate(context, purpose, rid, bound, expected):
    # Test host attestation only. Production has no implementation/bypass.
    defaults = dict(actor_id=OWNER, purpose=purpose, request_id=rid, binding_digest=bound,
                    expected_revision=expected, created_at=NOW, expires_at=NOW+600,
                    csrf_valid=True, session_active=True)
    defaults.update(context or {})
    return VerifiedPost(**defaults)


def readonly(context):
    return VerifiedRead(**dict(dict(actor_id=OWNER, expires_at=NOW+600, session_active=True), **(context or {})))


class Fixture:
    def __init__(self, action, decision='同一レシート'):
        self.action, self.decision = action, decision
        self.purpose = { 'confirm_grouping': STRUCTURE_REVIEW, 'confirm_page_kind': METADATA_REVIEW,
                        'confirm_privacy': METADATA_REVIEW, 'link_existing': RECONCILIATION_REVIEW}[action]
        self.rid, self.case_id = str(uuid4()), 'isolated-purpose-case'
        self.io = Memory()
        self.store = ConditionalRequests(self.io, 'isolated-shared-cas')
        self.native = native_modules()
        self.state_io = None
        self.accounting_appends = self.ai_sends = self.source_moves = 0
        self.native_calls = 0
        self.change = None
        self.mode = 'complete'
        self.saved_arguments = []
        if action in {'confirm_grouping', 'confirm_page_kind'}:
            n = self.native
            self.s, self.raw = source()
            observed = n.receipt_pdf_units.PdfObservations(self.s['source_file_id'], self.s['source_content_hash'], tuple(
                n.receipt_pdf_units.PageObservation(self.s['source_file_id'], self.s['source_content_hash'], i,
                    sha256(('fixture-render-'+str(i)).encode()).hexdigest(), 'extracted', 'normal', 'normal',
                    effective_render_scale=1.0, observation_complete=True, render_attempts=1) for i in (1, 2)))
            self.state_io = Memory()
            binding = digest(['isolated-grouping-store'])
            self.state_io.raw = n.pdf_grouping_authority.encoded(n.pdf_grouping_authority.empty_state(binding))
            state = n.pdf_grouping_authority.DriveGroupingStore(self.state_io, binding)
            proposer = n.receipt_pdf_grouping.AdjacentPageGrouping(lambda obs: [n.receipt_pdf_grouping.PageEvidence() for _ in obs.pages])
            self.grouping = n.pdf_grouping_authority.DurablePdfGrouping(state, lambda *_: observed, proposer=proposer,
                        clock=lambda: datetime.fromtimestamp(NOW, timezone.utc).isoformat())
            # Native setup is POST-equivalent fixture initialization, never GET.
            view = self.grouping.display(self.s['source_file_id'])
            self.proposal = view['proposal']
            self.kind = n.pdf_page_kind.PageKindConfirmation(self.grouping)
        elif action == 'link_existing':
            n = self.native
            self.s, self.raw = source(1)
            self.ident = dict(source_file_id=self.s['source_file_id'], source_content_hash=self.s['source_content_hash'],
                page_count=1, page_number=1, page_identity=pages(self.s)[0]['stable_page_identity'],
                receipt_unit_id='', review_identity=digest(['native-page-level-review', self.s]), revision=1)
            # Actual validator permits page-local identity with NO fabricated Unit.
            self.repo = n.receipt_audit.MemoryAuditRepository()
            self.candidate = dict(ledger_id='existing-ledger-fixture', snapshot_digest=digest(['existing-ledger-row']), active=True, readback_complete=True)
        else:
            n = self.native
            self.s, self.raw = source(1)
            native_source = n.page_receipt_model.SourceRef(source_file_id=self.s['source_file_id'], source_content_hash=self.s['source_content_hash'], page_count=1)
            p = n.page_receipt_model.PageUnit(source=native_source, page_number=1, stable_page_identity=pages(self.s)[0]['stable_page_identity'],
                automatic_classification='unknown', automatic_reason='insufficient_evidence', observation_complete=True,
                extraction_status='extracted', observation_render_hash=sha256(b'fixture-visible-page').hexdigest(), review_identity='0'*64, authority_revision=1)
            self.page = p.model_copy(update={'review_identity': n.human_general_authority.review_identity(p)})
            self.state_io = Memory()
            binding = digest(['isolated-privacy-store'])
            self.state_io.raw = json.dumps(n.human_general_authority.empty_state(binding)).encode()
            state = n.human_general_authority.HumanGeneralAuthorityStore(self.state_io, binding, preflight=lambda: None)
            self.privacy = n.human_general_authority.HumanGeneralConfirmation(state, lambda *_: self.page,
                lambda _: {'provider':'google','subject':'owner@example.test'}, load_source=lambda _: self.raw,
                clock=lambda: datetime.fromtimestamp(NOW, timezone.utc).isoformat())
        self.port = ExistingReviewPort(self.observe, self.execute, self.readback)
        self.snapshot = [dict(case_id=self.case_id, purpose=self.purpose, state='held', display={'原本':'fixture', '次にすること':'原本確認'})]
        self.boundary = PurposeBoundary(OWNER, gate, readonly, lambda: deepcopy(self.snapshot), self.store,
                    {(self.purpose, action): self.port}, enabled=True, clock=lambda: NOW)

    def observe(self, cid, purpose, action, rid):
        if self.action == 'confirm_grouping':
            p = self.proposal
            e = dict(source=self.s, pages=pages(self.s), target_revision=p['grouping_version'], proposal_digest=p['proposal_digest'], operation='confirm', partition=None)
            request = dict(request_id=rid, source_file_id=self.s['source_file_id'], source_content_hash=self.s['source_content_hash'],
                proposal_digest=p['proposal_digest'], grouping_revision=p['grouping_version'], operation='confirm', partition=None)
            args = dict(request=request, intent_digest=digest(request))
            bound = digest(request)
        elif self.action == 'confirm_page_kind':
            p = self.proposal
            expected = {k:p[k] for k in ('source_file_id','source_content_hash','proposal_digest','grouping_version')}
            e = dict(source=self.s, pages=pages(self.s)[:1], target_revision=p['grouping_version'], observation_digest=p['proposal_digest'], selection='一般', automatic_kind='normal')
            args = dict(expected=expected, selections={1:'一般'}, request_id=rid, intent_digest=digest([expected,1,'一般']))
            bound = digest([expected, args['selections'], rid])
        elif self.action == 'link_existing':
            n = self.native
            b = n.receipt_reconciliation_authority.binding(self.ident, request_id=rid, candidate_ledger_id=self.candidate['ledger_id'],
                ledger_snapshot_digest=self.candidate['snapshot_digest'], decision=n.receipt_reconciliation_authority.DECISIONS[self.decision], current_state_revision=0)
            self.native_record = dict(binding=b, digest=n.receipt_audit.digest(b),
                created_at=datetime.fromtimestamp(NOW, timezone.utc).isoformat(), expires_at=datetime.fromtimestamp(NOW+600, timezone.utc).isoformat())
            self.actor = n.human_general_auth_transport.VerifiedActor('https://accounts.google.com','101','owner@example.test',
                'google_oidc_shared_session_v2',NOW,rid,n.receipt_audit.digest(b),1,2)
            self.reconciliation = n.receipt_reconciliation_authority.ReconciliationConfirmation(self.repo,
                trusted_request=lambda _:deepcopy(self.native_record), verified_actor=lambda _:self.actor,
                current_identity=lambda *_:deepcopy(self.ident), load_source_hash=lambda _:self.s['source_content_hash'],
                ledger_readback=lambda _:deepcopy(self.candidate), owner_actor_id=OWNER,
                clock=lambda:datetime.fromtimestamp(NOW, timezone.utc))
            e = dict(source=self.s, pages=pages(self.s), target_revision=0, identity=self.ident,
                candidate_ledger_id=self.candidate['ledger_id'], ledger_snapshot_digest=self.candidate['snapshot_digest'], selection=self.decision)
            args = dict(request_id=rid, selected_decision=self.decision, method='POST', explicit_post=True)
            bound = n.receipt_audit.digest(b)
        else:
            e = dict(source=self.s, pages=pages(self.s), target_revision=self.page.authority_revision,
                     observation_digest=digest(self.page.model_dump()), selection='allow_single_page_ai', automatic_kind=self.page.automatic_classification)
            args = dict(expected=self.page, operation='confirm_general_receipt_ai', request_id=rid)
            transport = self.native.human_general_auth_transport
            bound = self.native.page_receipt_model.digest(transport.request_binding(self.page, rid, transport.AI_CONSENT_ACTION))
        o = ReviewObservation(cid, purpose, action, deepcopy(e), bound, deepcopy(args), legacy_shared_claim=True)
        return self.change(o) if self.change else o

    def execute(self, **args):
        self.native_calls += 1
        self.saved_arguments.append(deepcopy(args))
        if self.mode == 'partial':
            raise TimeoutError('isolated partial')
        if self.action == 'confirm_grouping':
            result = self.grouping.review(**args)
        elif self.action == 'confirm_page_kind':
            result = self.kind.confirm(**args)
        elif self.action == 'link_existing':
            result = self.reconciliation.confirm(**args)
        else:
            result = self.privacy.confirm(**args)
        if self.mode == 'lost':
            raise TimeoutError('isolated lost response')
        return result

    def readback(self, r, o):
        if self.mode == 'partial':
            return False
        if self.action == 'confirm_grouping':
            result = self.grouping.replayed(r.request_id, self.s['source_file_id'], o.native_arguments['intent_digest'])
            return result is not None and result['status'] == 'grouping_confirmed' and result['accounting_allowed'] is False
        if self.action == 'confirm_page_kind':
            _, _, answers = self.kind.current(self.s['source_file_id'])
            return answers[1]['human_classification'] == 'normal' and answers[1]['accounting_allowed'] is False
        if self.action == 'link_existing':
            if self.decision == '判断できない':
                return self.repo.lookup(r.request_id) is None and self.repo.get_current(self.ident)['status'] == 'reconciliation_required'
            marker = self.repo.lookup(r.request_id)
            expected = 'reconciled_existing' if self.decision == '同一レシート' else 'confirmed_distinct'
            return marker is not None and self.repo.get_event(marker['event_ref'])['event_type'] == expected
        return self.privacy.current(self.page)['accounting_allowed'] is False

    def prepare(self):
        return self.boundary.prepare(self.case_id, self.purpose, self.action, self.rid, self.io.revision, {})['operation_targets'][0]


def existing_review_fixture(action, decision='同一レシート'):
    return Fixture(action, decision)
