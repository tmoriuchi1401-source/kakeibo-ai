"""Purpose-specific contracts for an existing authenticated host, not routes.

No live factory, seeding, authority schema, credentials or HTTP implementation.
The host injects its verified read/POST gates and existing native services.
GET reads an already observed snapshot; native display/prepare is never used.
"""
from copy import deepcopy
from dataclasses import asdict, dataclass
import re
from uuid import UUID

from .drive_run_state import StateError
from .pdf_page_identity import page_identity
from .unified_receipt_review import digest, LEGACY_UNMAPPED_HOLD

STRUCTURE_REVIEW = 'STRUCTURE_REVIEW'
METADATA_REVIEW = 'METADATA_REVIEW'
RECONCILIATION_REVIEW = 'RECONCILIATION_REVIEW'
ACCOUNTING_POST = 'ACCOUNTING_POST'
PURPOSE_ACTIONS = {STRUCTURE_REVIEW: {'confirm_grouping'},
                   METADATA_REVIEW: {'confirm_page_kind', 'confirm_privacy'},
                   RECONCILIATION_REVIEW: {'link_existing'}, ACCOUNTING_POST: {'post'}}
RECONCILIATION_SELECTIONS = {'同一レシート': 'same', '別のレシート': 'different', '判断できない': 'unknown'}
TTL = 600


def checked_hash(value):
    return isinstance(value, str) and re.fullmatch('[0-9a-f]{64}', value) is not None


def revision(value):
    return type(value) is int and value >= 0


def source_pages(evidence):
    source, pages = evidence['source'], evidence['pages']
    if (set(source) != {'source_file_id', 'source_content_hash', 'source_version', 'page_count'}
            or not isinstance(source['source_file_id'], str)
            or not re.fullmatch('[A-Za-z0-9_-]{1,150}', source['source_file_id'])
            or not checked_hash(source['source_content_hash'])
            or not isinstance(source['source_version'], str) or not source['source_version']
            or type(source['page_count']) is not int or not 1 <= source['page_count'] <= 50
            or not isinstance(pages, list) or not pages):
        raise ValueError()
    numbers = []
    for p in pages:
        if (set(p) != {'page_number', 'stable_page_identity'}
                or p['stable_page_identity'] != page_identity(source['source_content_hash'], p['page_number'], source['page_count'])):
            raise ValueError()
        numbers.append(p['page_number'])
    if numbers != sorted(set(numbers)):
        raise ValueError()


@dataclass(frozen=True)
class ReviewObservation:
    """Fresh trusted reader output. Native arguments never enter browser/state."""
    case_id: str
    purpose: str
    requested_action: str
    evidence: dict
    native_binding_digest: str
    native_arguments: dict
    disposition: str = ''
    legacy_shared_claim: bool = False
    outcome: str = 'unwritten'

    @property
    def operation_identity(self):
        # Both entry points use this exact identity. No request ID or actor.
        # A display-card ID cannot mint a second operation for the same scope.
        return digest(['unified-native-review-operation-v1', self.purpose,
                       self.requested_action, self.evidence])


def validate_observation(observation, request_id, *, recovery=False):
    """Metadata never acquires financial eligibility or a made-up Unit."""
    if not isinstance(observation, ReviewObservation):
        raise StateError('unified_native_reader_required')
    o = observation
    if (o.disposition == LEGACY_UNMAPPED_HOLD or o.outcome not in ({'unwritten','complete','partial_unknown'} if recovery else {'unwritten'})
            or o.legacy_shared_claim is not True):
        raise StateError('unified_case_write_forbidden')
    if o.purpose == ACCOUNTING_POST or o.requested_action not in PURPOSE_ACTIONS.get(o.purpose, set()):
        raise StateError('unified_purpose_route_required')
    try:
        if not o.case_id or not checked_hash(o.native_binding_digest):
            raise ValueError()
        e, args = o.evidence, o.native_arguments
        source_pages(e)
        from .unified_receipt_write import OLD_P1_SOURCE, OLD_P1_HASH
        if (e['source']['source_file_id']==OLD_P1_SOURCE and e['source']['source_content_hash']==OLD_P1_HASH
                and any(p['page_number']==1 for p in e['pages'])):
            raise StateError('unified_legacy_page_reuse_forbidden')
        common = {'source', 'pages', 'target_revision'}
        if not revision(e['target_revision']):
            raise ValueError()
        if o.purpose == STRUCTURE_REVIEW:
            if set(e) != common | {'proposal_digest', 'operation', 'partition'} or not checked_hash(e['proposal_digest']):
                raise ValueError()
            if [p['page_number'] for p in e['pages']] != list(range(1, e['source']['page_count'] + 1)):
                raise ValueError()
            if e['operation'] not in {'confirm', 'edit', 'reject', 'hold'}:
                raise ValueError()
            if e['operation'] == 'edit':
                parts = e['partition']
                if (not isinstance(parts, list) or not parts or any(not isinstance(p, list) or not p or p != sorted(set(p)) for p in parts)
                        or [n for p in parts for n in p] != list(range(1, e['source']['page_count'] + 1))):
                    raise ValueError()
            elif e['partition'] is not None:
                raise ValueError()
            expected = dict(request_id=request_id, source_file_id=e['source']['source_file_id'],
                            source_content_hash=e['source']['source_content_hash'], proposal_digest=e['proposal_digest'],
                            grouping_revision=e['target_revision'], operation=e['operation'], partition=e['partition'])
            if set(args) != {'request', 'intent_digest'} or args['request'] != expected or args['intent_digest'] != digest(expected):
                raise ValueError()
        elif o.purpose == METADATA_REVIEW:
            if (set(e) != common | {'observation_digest', 'selection', 'automatic_kind'}
                    or len(e['pages']) != 1 or not checked_hash(e['observation_digest'])
                    or e['automatic_kind'] not in {'normal', 'unknown', 'sensitive_unknown', 'medical', 'payroll'}):
                raise ValueError()
            if o.requested_action == 'confirm_page_kind':
                if e['selection'] not in {'一般', '医療', '給与', '判定不能'}:
                    raise ValueError()
                expected = args['expected']
                if (set(args) != {'expected', 'selections', 'request_id', 'intent_digest'}
                        or args['request_id'] != request_id or not checked_hash(args['intent_digest'])
                        or args['selections'] != {e['pages'][0]['page_number']: e['selection']}
                        or expected['source_file_id'] != e['source']['source_file_id']
                        or expected['source_content_hash'] != e['source']['source_content_hash']
                        or expected['grouping_version'] != e['target_revision']
                        or expected['proposal_digest'] != e['observation_digest']):
                    raise ValueError()
                if e['automatic_kind'] in {'medical', 'payroll'} and e['selection'] != {'medical': '医療', 'payroll': '給与'}[e['automatic_kind']]:
                    raise ValueError()
            else:
                if e['selection'] not in {'allow_single_page_ai', 'deny'}:
                    raise ValueError()
                if e['automatic_kind'] in {'medical', 'payroll'} and e['selection'] != 'deny':
                    raise ValueError()
                if (set(args) != {'expected', 'operation', 'request_id'} or args['request_id'] != request_id
                        or args['operation'] != ('confirm_general_receipt_ai' if e['selection'] == 'allow_single_page_ai' else 'hold_general_receipt_ai')):
                    raise ValueError()
                p = args['expected'].model_dump()
                if (p['source']['source_file_id'] != e['source']['source_file_id']
                        or p['source']['source_content_hash'] != e['source']['source_content_hash']
                        or p['stable_page_identity'] != e['pages'][0]['stable_page_identity']
                        or p['page_number'] != e['pages'][0]['page_number']
                        or p['source']['page_count'] != e['source']['page_count']
                        or p['automatic_classification'] != e['automatic_kind']
                        or p['authority_revision'] != e['target_revision'] or digest(p) != e['observation_digest']):
                    raise ValueError()
        else:
            if set(e) != common | {'identity', 'candidate_ledger_id', 'ledger_snapshot_digest', 'selection'}:
                raise ValueError()
            if e['selection'] not in RECONCILIATION_SELECTIONS or not checked_hash(e['ledger_snapshot_digest']):
                raise ValueError()
            ident = e['identity']
            if (set(ident) != {'source_file_id', 'source_content_hash', 'page_count', 'page_number', 'page_identity',
                              'receipt_unit_id', 'review_identity', 'revision'}
                    or ident['source_file_id'] != e['source']['source_file_id']
                    or ident['source_content_hash'] != e['source']['source_content_hash']
                    or ident['page_count'] != e['source']['page_count'] or len(e['pages']) != 1
                    or ident['page_number'] != e['pages'][0]['page_number']
                    or ident['page_identity'] != e['pages'][0]['stable_page_identity']
                    or not checked_hash(ident['review_identity']) or type(ident['revision']) is not int or ident['revision'] < 1
                    or not isinstance(ident['receipt_unit_id'], str)
                    or not isinstance(e['candidate_ledger_id'], str)
                    or not re.fullmatch(r'[A-Za-z0-9_:.\-]{1,160}', e['candidate_ledger_id'])
                    or set(args) != {'request_id', 'selected_decision', 'method', 'explicit_post'}
                    or args != dict(request_id=request_id, selected_decision=e['selection'], method='POST', explicit_post=True)):
                raise ValueError()
        return o
    except (ValueError, TypeError, KeyError, AttributeError, StateError):
        raise StateError('unified_' + o.purpose.lower() + '_evidence_invalid') from None


@dataclass(frozen=True)
class PurposeRequest:
    case_id: str
    purpose: str
    requested_action: str
    request_id: str
    expected_state_revision: int
    operation_identity: str
    evidence_digest: str
    target_revision: int

    @classmethod
    def parse(cls, value):
        try:
            r = cls(**value)
            if (r.purpose == ACCOUNTING_POST or r.requested_action not in PURPOSE_ACTIONS.get(r.purpose, set())
                    or not isinstance(r.case_id, str) or not r.case_id or str(UUID(r.request_id)) != r.request_id
                    or not revision(r.expected_state_revision) or not revision(r.target_revision)
                    or not checked_hash(r.operation_identity) or not checked_hash(r.evidence_digest)):
                raise ValueError()
            return r
        except (ValueError, TypeError, AttributeError):
            raise StateError('unified_request_invalid') from None

    def fingerprint(self):
        return digest({k: v for k, v in asdict(self).items() if k not in {'request_id', 'expected_state_revision'}})


@dataclass(frozen=True)
class VerifiedPost:
    """In-process attestation from the EXISTING host gate, never browser JSON."""
    actor_id: str
    purpose: str
    request_id: str
    binding_digest: str
    expected_revision: int
    created_at: int
    expires_at: int
    method: str = 'POST'
    csrf_valid: bool = False
    session_active: bool = False
    issuer: str = 'https://accounts.google.com'


@dataclass(frozen=True)
class VerifiedRead:
    actor_id: str
    expires_at: int
    session_active: bool


class ExistingReviewPort:
    """Calls a bound EXISTING service method with its trusted native arguments.

    reader must verify source/version, current revision and native binding.
    No authority is accepted in HTTP payload or synthesized by this adapter.
    """
    def __init__(self, reader, submit_native, readback):
        self.reader, self.submit_native, self.readback = reader, submit_native, readback

    def observe(self, case_id, purpose, action, request_id, *, recovery=False):
        o = validate_observation(self.reader(case_id, purpose, action, request_id), request_id, recovery=recovery)
        if (o.case_id, o.purpose, o.requested_action) != (case_id, purpose, action):
            raise StateError('unified_purpose_binding_changed')
        return o

    def execute(self, observation):
        # Privacy denial uses the existing hold method, whose signature has no
        # operation argument. It never means an AI submission.
        arguments = deepcopy(observation.native_arguments)
        if observation.requested_action == 'confirm_privacy' and observation.evidence['selection'] == 'deny':
            arguments.pop('operation')
        return self.submit_native(**arguments)


def project_current_snapshot(snapshot):
    """Pure projection: no clients, display(), observations, prepare or grants."""
    if not isinstance(snapshot, list) or any(not isinstance(c, dict) or not c.get('case_id') for c in snapshot):
        raise StateError('unified_projection_invalid')
    if len({c['case_id'] for c in snapshot}) != len(snapshot):
        raise StateError('unified_duplicate_case_reference')
    return [dict(case_id=c['case_id'], purpose=c.get('purpose'), state=c.get('state', 'held'),
                 disposition=c.get('disposition', ''), display=deepcopy(c.get('display', {})),
                 operation_targets=[]) for c in snapshot]


class PurposeBoundary:
    """Future host list/detail and authenticated POST prepare/submit boundary.

    All switches are default OFF. No production environment switch enables
    this class. Explicit injection is for isolated contracts until host review.
    """
    def __init__(self, owner_actor_id, verify_post, verify_read, snapshot_reader, requests=None,
                 ports=None, accounting=None, *, enabled=False, clock):
        if not checked_hash(owner_actor_id):
            raise StateError('unified_owner_configuration_invalid')
        self.owner, self.verify_post, self.verify_read = owner_actor_id, verify_post, verify_read
        self.snapshot_reader, self.requests, self.ports = snapshot_reader, requests, ports or {}
        self.accounting, self.enabled, self.clock = accounting, enabled, clock
        if accounting is not None and requests is not accounting.bridge.requests:
            raise StateError('unified_shared_request_store_missing')

    def _read(self, context):
        proof = self.verify_read(context)
        if (not isinstance(proof, VerifiedRead) or proof.actor_id != self.owner
                or proof.session_active is not True or type(proof.expires_at) is not int or self.clock() >= proof.expires_at):
            raise StateError('unified_authenticated_read_required')

    def get_list(self, context):
        self._read(context)
        return project_current_snapshot(self.snapshot_reader())

    def get_detail(self, case_id, context):
        rows = [c for c in self.get_list(context) if c['case_id'] == case_id]
        if len(rows) != 1:
            raise StateError('unified_case_not_unique')
        return rows[0]

    def _post(self, context, purpose, rid, binding_digest, expected_revision):
        p = self.verify_post(context, purpose, rid, binding_digest, expected_revision)
        if (not isinstance(p, VerifiedPost) or p.actor_id != self.owner or p.issuer != 'https://accounts.google.com'
                or (p.purpose, p.request_id, p.expected_revision) != (purpose, rid, expected_revision)
                or binding_digest is not None and p.binding_digest != binding_digest
                or not revision(p.expected_revision)
                or p.method != 'POST' or p.csrf_valid is not True or p.session_active is not True
                or type(p.created_at) is not int or type(p.expires_at) is not int
                or p.expires_at - p.created_at != TTL or not p.created_at <= self.clock() < p.expires_at):
            raise StateError('unified_authenticated_post_required')

    def _observation(self, case_id, purpose, action, rid, *, recovery=False):
        port = self.ports.get((purpose, action))
        if port is None:
            raise StateError('unified_native_route_unavailable')
        return port.observe(case_id, purpose, action, rid, recovery=recovery), port

    def prepare(self, case_id, purpose, action, rid, expected_revision, context):
        if not self.enabled:
            raise StateError('unified_case_write_forbidden')
        if purpose not in PURPOSE_ACTIONS or action not in PURPOSE_ACTIONS[purpose] or not revision(expected_revision):
            raise StateError('unified_purpose_route_required')
        self._post(context,purpose,rid,None,expected_revision)
        if purpose == ACCOUNTING_POST:
            if action != 'post' or self.accounting is None:
                raise StateError('unified_purpose_route_required')
            case = self.accounting.case(case_id)
            from .unified_receipt_eligibility import current_snapshot
            self._post(context, purpose, rid, digest(current_snapshot(case)), expected_revision)
            state, _, _ = self.accounting.bridge.requests.read()
            if state['revision'] != expected_revision:
                raise StateError('unified_request_stale_revision')
            if any(op['request']['request_id']==rid for op in state['operations'].values()):
                raise StateError('unified_request_id_conflict')
            return self.accounting.prepare(case_id, action, rid)
        o, _ = self._observation(case_id, purpose, action, rid)
        self._post(context, purpose, rid, o.native_binding_digest, expected_revision)
        if self.requests is None:
            raise StateError('unified_shared_request_store_missing')
        state, _, _ = self.requests.read()
        if state['revision'] != expected_revision:
            raise StateError('unified_request_stale_revision')
        if any(op['request']['request_id'] == rid for op in state['operations'].values()):
            raise StateError('unified_request_id_conflict')
        r = PurposeRequest(case_id, purpose, action, rid, expected_revision,
                           o.operation_identity, digest(o.evidence), o.evidence['target_revision'])
        PurposeRequest.parse(asdict(r))
        # Preparation validates only, does not seed or issue native authority.
        return dict(state='validated_awaiting_owner', operation_targets=[asdict(r)])

    def submit(self, payload, context, *, entry='unified'):
        if not self.enabled:
            raise StateError('unified_case_write_forbidden')
        if entry not in {'unified', 'legacy'}:
            raise StateError('unified_entry_invalid')
        if 'purpose' not in payload:
            from .unified_receipt_write import OperationRequest
            r = OperationRequest.parse(payload)
            if r.requested_action != 'post' or self.accounting is None:
                raise StateError('unified_purpose_route_required')
            self._post(context, ACCOUNTING_POST, r.request_id, r.current_snapshot_digest, r.expected_state_revision)
            return self.accounting.bridge.submit(self.accounting.case(r.case_id), payload, entry=entry)
        r = PurposeRequest.parse(payload)
        self._post(context,r.purpose,r.request_id,None,r.expected_state_revision)
        o, port = self._observation(r.case_id, r.purpose, r.requested_action, r.request_id)
        self._post(context, r.purpose, r.request_id, o.native_binding_digest, r.expected_state_revision)
        if (o.operation_identity, digest(o.evidence), o.evidence['target_revision']) != (r.operation_identity, r.evidence_digest, r.target_revision):
            raise StateError('unified_request_snapshot_changed')
        if self.requests is None:
            raise StateError('unified_shared_request_store_missing')
        before, raw, tag = self.requests.read()
        # Canonical request ID is shared with accounting; check before replay.
        if any(op['request']['request_id'] == r.request_id and key != r.operation_identity for key, op in before['operations'].items()):
            raise StateError('unified_request_id_conflict')
        old = before['operations'].get(r.operation_identity)
        if old:
            if old['fingerprint'] != r.fingerprint():
                raise StateError('unified_operation_conflict')
            return dict(state=old['state'], replay=True, accounting_writes=0)
        if before['revision'] != r.expected_state_revision:
            raise StateError('unified_request_stale_revision')
        after = deepcopy(before)
        after['revision'] += 1
        after['operations'][r.operation_identity] = dict(request=asdict(r), fingerprint=r.fingerprint(),
                state='accepted', events=[dict(state='accepted', at=self.clock())])
        self.requests.replace(before, raw, tag, after)
        self.requests.transition(r, 'executing', self.clock())
        try:
            fresh, _ = self._observation(r.case_id, r.purpose, r.requested_action, r.request_id)
            self._post(context, r.purpose, r.request_id, fresh.native_binding_digest, r.expected_state_revision)
            if fresh != o:
                raise StateError('unified_changed_after_claim')
            port.execute(fresh)
            self.requests.transition(r, 'awaiting_readback', self.clock())
            if port.readback(r, fresh) is not True:
                raise StateError('unified_native_exact_readback_required')
            self.requests.transition(r, 'complete', self.clock())
            return dict(state='complete', replay=False, accounting_writes=0)
        except Exception:
            self.requests.transition(r, 'partial_unknown', self.clock())
            raise StateError('unified_partial_unknown_readback_only') from None

    def recover_readback(self, payload, context):
        """POST authorization remains mandatory; execute is never called."""
        if not self.enabled or self.requests is None:
            raise StateError('unified_recovery_forbidden')
        r = PurposeRequest.parse(payload)
        self._post(context,r.purpose,r.request_id,None,r.expected_state_revision)
        o, port = self._observation(r.case_id, r.purpose, r.requested_action, r.request_id, recovery=True)
        self._post(context, r.purpose, r.request_id, o.native_binding_digest, r.expected_state_revision)
        if (o.operation_identity, digest(o.evidence), o.evidence['target_revision']) != (r.operation_identity, r.evidence_digest, r.target_revision):
            raise StateError('unified_recovery_binding_changed')
        before, _, _ = self.requests.read()
        old = before['operations'].get(r.operation_identity)
        if not old or old['fingerprint'] != r.fingerprint() or old['state'] not in {'executing','awaiting_readback','partial_unknown','complete'}:
            raise StateError('unified_recovery_state_held')
        if port.readback(r, o) is not True:
            return dict(state='partial_unknown', accounting_writes=0)
        if old['state'] == 'executing':
            self.requests.transition(r, 'awaiting_readback', self.clock())
        if old['state'] != 'complete':
            self.requests.transition(r, 'complete', self.clock())
        return dict(state='complete', accounting_writes=0, exact_readback=True)
