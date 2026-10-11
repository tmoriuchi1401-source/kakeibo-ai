"""Opt-in request coordinator, never an accounting writer or authority issuer.

The host injects fresh native validator adapters and a shared conditional store.
There is intentionally no production client factory, CLI, scheduler or deploy.
Audit manifests/browser JSON are not accepted as validator evidence.
"""
from copy import deepcopy
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
import re
from uuid import UUID

from .drive_run_state import StateError
from .pdf_intake_registry import strong_etag
from .unified_receipt_review import digest, LEGACY_UNMAPPED_HOLD
from .unified_receipt_eligibility import evaluate_batch, identity_scope, current_snapshot

SCHEMA = 'unified-receipt-requests-v1'
STATES = ('not_received', 'validated_awaiting_owner', 'accepted', 'executing',
          'awaiting_readback', 'complete', 'held', 'partial_unknown')
LABELS = dict(zip(STATES, ('未受付', '検証済み・本人確認待ち', '要求受付済み', '実行中',
                           'read-back待ち', '完了', '保留・再確認', 'partial／unknown停止')))
FINANCIAL = {'post', 'link_existing'}
ACTIONS = FINANCIAL | {'confirm_grouping', 'confirm_page_kind', 'confirm_privacy'}
MAX_BYTES = 2 * 1024 * 1024
OLD_P1_SOURCE = '1fcmMMGj86DLq54G0inD8LI_XSyQSPfTY'
OLD_P1_HASH = 'ca1b8cba60addc14a364438d691c40164e250708103bce76a320f671629491bf'
OLD_REFERENCES = {'review-da12dd1ff1262d92d5e98e6a',
                  'e177252a30d6333cb7aa3799e0466ae490b1148dead81bcdbb546a8feea89073',
                  '055a196a415b77045252f2f1bbd1df38c907b9da375f0fd0d53d30d7ac1ec4a9'}


def prohibited(case, *, recovery=False):
    return (case.synthetic or case.history or case.related_to or case.issues
            or case.disposition == LEGACY_UNMAPPED_HOLD or case.legacy_id in OLD_REFERENCES
            or case.identity.get('review_id') in OLD_REFERENCES
            or case.source_id == OLD_P1_SOURCE and case.identity.get('source_content_hash') == OLD_P1_HASH
            and 1 in case.pages
            or case.authority_status in ({'superseded','closed_machine','closed_user'} if recovery else
                {'superseded','applied','imported','pending','claimed','held','unknown','partial','closed_machine','closed_user'}))


@dataclass(frozen=True)
class Pin:
    case_id: str
    source_hash: str
    unit_id: str
    expected_revision: int


@dataclass(frozen=True)
class WriteConfig:
    enabled: bool = False
    pin: Pin | None = None
    request_store_id: str = ''

    @classmethod
    def from_env(cls, env):
        # Unset, '1', truthy objects and partial configuration never opt in.
        enabled = env.get('UNIFIED_RECEIPT_WRITE_ENABLED', 'false') == 'true'
        names = ('UNIFIED_RECEIPT_CASE_ID', 'UNIFIED_RECEIPT_SOURCE_HASH',
                 'UNIFIED_RECEIPT_UNIT_ID', 'UNIFIED_RECEIPT_EXPECTED_REVISION')
        values = [env.get(n, '') for n in names]
        if not enabled:
            return cls()
        if not all(isinstance(v,str) and v for v in values):
            raise StateError('unified_write_pin_missing')
        if not re.fullmatch(r'0|[1-9][0-9]*', values[3]):
            raise StateError('unified_write_revision_invalid')
        return cls(True, Pin(*values[:3], int(values[3])), env.get('UNIFIED_RECEIPT_REQUEST_STORE_ID', ''))

    def allows(self, case, revision):
        return (self.enabled is True and self.pin is not None and bool(self.request_store_id)
                and type(revision) is int and type(self.pin.expected_revision) is int
                and self.pin == Pin(case.case_id, case.identity.get('source_content_hash'),
                                    case.identity.get('receipt_unit_id'), revision))


@dataclass(frozen=True)
class OperationRequest:
    case_id: str
    review_purpose: str
    source_file_id: str
    source_content_hash: str
    source_version: str
    stable_page_identity: str
    receipt_unit_id: str
    receipt_index: int
    current_snapshot_digest: str
    authority_revision: int
    requested_action: str
    request_id: str
    expected_state_revision: int
    operation_identity: str

    @property
    def purpose(self):
        return 'ACCOUNTING_POST'

    @classmethod
    def parse(cls, value):
        try:
            r = cls(**value)
            if (any(not isinstance(v,str) or not v for k,v in asdict(r).items()
                    if k not in {'receipt_index','authority_revision','expected_state_revision'})
                    or any(type(getattr(r,k)) is not int or getattr(r,k)<1
                           for k in ('receipt_index','authority_revision'))
                    or type(r.expected_state_revision) is not int or r.expected_state_revision<0
                    or r.requested_action not in ACTIONS
                    or any(not re.fullmatch(r'[0-9a-f]{64}',getattr(r,k)) for k in
                           ('source_content_hash','stable_page_identity','current_snapshot_digest','operation_identity'))
                    or str(UUID(r.request_id)) != r.request_id):
                raise ValueError()
            return r
        except (TypeError,ValueError,AttributeError):
            raise StateError('unified_request_invalid') from None

    def fingerprint(self):
        # Double clicks may have different UI request IDs; native operation does
        # not. State revision is an acceptance precondition, not a new operation.
        return digest({k:v for k,v in asdict(self).items()
                       if k not in {'request_id','expected_state_revision'}})


@dataclass(frozen=True)
class NativeInspection:
    """Server-created result. Only injected native adapters produce this type."""
    case: object
    evidence: dict
    operation_identity: str
    authority_revision: int
    owner_confirmed: bool
    expires_at: int
    legacy_shared_claim: bool
    outcome: str = 'unwritten'


def utc_now():
    return int(datetime.now(timezone.utc).timestamp())


class ConditionalRequests:
    """Existing read_versioned/replace_versioned CAS protocol, no live transport.

    Host must supply the SAME private store for both request entry points.
    No files are created or state seeded by the coordinator.
    """
    def __init__(self, io, store_id):
        self.io, self.store_id = io, store_id

    @staticmethod
    def parse_request(value):
        if 'purpose' in value:
            from .unified_receipt_purposes import PurposeRequest
            return PurposeRequest.parse(value)
        return OperationRequest.parse(value)

    def read(self):
        raw,tag = self.io.read_versioned()
        if type(raw) is not bytes or len(raw)>MAX_BYTES or not strong_etag(tag):
            raise StateError('unified_request_store_unavailable')
        def unique(pairs):
            out={}
            for k,v in pairs:
                if k in out: raise ValueError()
                out[k]=v
            return out
        try:
            value=json.loads(raw,object_pairs_hook=unique)
            if (set(value)!={'schema','revision','operations'} or value['schema']!=SCHEMA
                    or type(value['revision']) is not int or value['revision']<0
                    or not isinstance(value['operations'],dict)):
                raise ValueError()
            request_ids=set()
            for key,op in value['operations'].items():
                request=self.parse_request(op['request'])
                if (key!=request.operation_identity or set(op)!={'request','fingerprint','state','events'}
                        or op['fingerprint']!=request.fingerprint() or op['state'] not in STATES
                        or not isinstance(op['events'],list) or not op['events']
                        or op['events'][-1]['state']!=op['state']):
                    raise ValueError()
                if request.request_id in request_ids:raise ValueError()
                request_ids.add(request.request_id)
        except (ValueError,KeyError,TypeError):
            raise StateError('unified_request_store_invalid') from None
        return value,raw,tag

    def replace(self, before, raw, tag, after):
        if after['revision'] != before['revision']+1:
            raise StateError('unified_request_revision_invalid')
        proposed=json.dumps(after,sort_keys=True,separators=(',',':'),ensure_ascii=True).encode()
        if len(proposed)>MAX_BYTES:raise StateError('unified_request_store_full')
        # An unknown CAS response never permits entering the native writer.
        self.io.replace_versioned(raw,tag,proposed)
        actual,_,_=self.read()
        if actual!=after:raise StateError('unified_request_cas_readback_unknown')

    def transition(self, request, state, now):
        before,raw,tag=self.read();after=deepcopy(before)
        op=after['operations'].get(request.operation_identity)
        if not op or op['fingerprint']!=request.fingerprint():
            raise StateError('unified_operation_binding_changed')
        allowed={'accepted':{'executing','held'},'executing':{'awaiting_readback','partial_unknown'},
                 'awaiting_readback':{'complete','partial_unknown'},'partial_unknown':{'complete'},
                 'complete':set(),'held':set()}
        if state not in allowed.get(op['state'],set()):raise StateError('unified_state_transition_invalid')
        op['state']=state;op['events'].append(dict(state=state,at=now));after['revision']+=1
        self.replace(before,raw,tag,after)
        return deepcopy(op)


class UnifiedWriteBridge:
    def __init__(self, config=None, requests=None, adapters=None, *, clock=utc_now):
        self.config=config or WriteConfig();self.requests=requests
        self.adapters=adapters or {};self.clock=clock

    def _inspection(self, case, action):
        if action != 'post':raise StateError('unified_purpose_route_required')
        if prohibited(case):raise StateError('unified_case_write_forbidden')
        port=self.adapters.get((case.kind,action))
        if port is None:raise StateError('unified_native_route_unavailable')
        result=port.inspect(deepcopy(case),action)
        if not isinstance(result,NativeInspection) or result.case!=case:
            raise StateError('unified_native_inspection_changed')
        if (result.legacy_shared_claim is not True or type(result.owner_confirmed) is not bool or result.outcome!='unwritten'
                or type(result.authority_revision) is not int or result.authority_revision<1
                or type(result.expires_at) is not int or self.clock()>=result.expires_at):
            raise StateError('unified_native_authority_or_outcome_held')
        if getattr(port,'writer_name',result.evidence.get('writer',{}).get('name'))!=result.evidence.get('writer',{}).get('name'):
            raise StateError('unified_native_writer_changed')
        decision=evaluate_batch([case],{case.case_id:result.evidence})[case.case_id]
        if not decision.eligible or decision.proposed_target!=result.operation_identity:
            raise StateError('unified_native_eligibility_held')
        if (action=='link_existing') != (result.evidence['permissions']['posting']=='link_only'):
            raise StateError('unified_native_action_permission_mismatch')
        return result,port

    def prepare(self, case, action, request_id):
        # Default OFF is checked before touching a client, adapter or store.
        if not self.config.enabled or prohibited(case):
            return dict(state='not_received',label=LABELS['not_received'],operation_targets=[])
        if action not in ACTIONS:raise StateError('unified_action_invalid')
        if self.requests is None or self.config.request_store_id!=self.requests.store_id:
            raise StateError('unified_shared_request_store_missing')
        before,_,_=self.requests.read()
        if not self.config.allows(case,before['revision']):
            return dict(state='held',label=LABELS['held'],operation_targets=[])
        inspected,_=self._inspection(case,action)
        if not inspected.owner_confirmed:
            return dict(state='validated_awaiting_owner',label=LABELS['validated_awaiting_owner'],operation_targets=[])
        scope=identity_scope(case)
        request=OperationRequest.parse(dict(case_id=case.case_id,
            **{k:scope[k] for k in ('review_purpose','source_file_id','source_content_hash','source_version',
                                  'stable_page_identity','receipt_unit_id','receipt_index')},
            current_snapshot_digest=digest(current_snapshot(case)),authority_revision=inspected.authority_revision,
            requested_action=action,request_id=request_id,expected_state_revision=before['revision'],
            operation_identity=inspected.operation_identity))
        port=self.adapters[(case.kind,action)]
        if hasattr(port,'validate_request'):port.validate_request(request)
        return dict(state='not_received',label=LABELS['not_received'],operation_targets=[asdict(request)])

    def submit(self, case, payload, *, entry='unified'):
        if entry not in {'unified','legacy'}:raise StateError('unified_entry_invalid')
        if not self.config.enabled or prohibited(case):raise StateError('unified_case_write_forbidden')
        request=OperationRequest.parse(payload)
        if request.requested_action != 'post':raise StateError('unified_purpose_route_required')
        if self.requests is None or self.config.request_store_id!=self.requests.store_id:
            raise StateError('unified_shared_request_store_missing')
        before,raw,tag=self.requests.read()
        if not self.config.allows(case,request.expected_state_revision):
            raise StateError('unified_write_pin_mismatch')
        expected={**{k:identity_scope(case)[k] for k in ('review_purpose','source_file_id','source_content_hash',
                  'source_version','stable_page_identity','receipt_unit_id','receipt_index')},
                  'case_id':case.case_id,'current_snapshot_digest':digest(current_snapshot(case))}
        if any(getattr(request,k)!=v for k,v in expected.items()):raise StateError('unified_request_snapshot_changed')
        # Cross-purpose canonical IDs cannot bypass this check via replay.
        if any(op['request']['request_id']==request.request_id and key!=request.operation_identity
               for key,op in before['operations'].items()):raise StateError('unified_request_id_conflict')
        existing=before['operations'].get(request.operation_identity)
        if existing:
            if existing['fingerprint']!=request.fingerprint():raise StateError('unified_operation_conflict')
            # A repeated button/legacy request never invokes execute or recovery.
            # Read-back recovery is a separate operation with zero append rights.
            return dict(state=existing['state'],label=LABELS[existing['state']],replay=True,accounting_writes=0)
        if before['revision']!=request.expected_state_revision:raise StateError('unified_request_stale_revision')
        # A canonical accepted request ID belongs to exactly one native
        # operation. It cannot mint another operation, even for another Unit.
        if any(op['request']['request_id']==request.request_id for op in before['operations'].values()):
            raise StateError('unified_request_id_conflict')
        inspected,port=self._inspection(case,request.requested_action)
        if hasattr(port,'validate_request'):port.validate_request(request)
        if (not inspected.owner_confirmed or inspected.operation_identity!=request.operation_identity
                or inspected.authority_revision!=request.authority_revision):
            raise StateError('unified_request_authority_changed')
        # A Unit cannot get a second financial operation under another native
        # request ID/purpose. Complete/unknown operations never free this fence.
        for old in before['operations'].values():
            other=self.requests.parse_request(old['request'])
            if (isinstance(other,OperationRequest) and other.receipt_unit_id==request.receipt_unit_id
                    and other.requested_action in FINANCIAL):
                raise StateError('unified_unit_already_owned')
        after=deepcopy(before);after['revision']+=1
        after['operations'][request.operation_identity]=dict(request=asdict(request),fingerprint=request.fingerprint(),
            state='accepted',events=[dict(state='accepted',at=self.clock())])
        self.requests.replace(before,raw,tag,after)
        self.requests.transition(request,'executing',self.clock())
        try:
            fresh,_=self._inspection(case,request.requested_action)
            if fresh!=inspected:raise StateError('unified_changed_after_claim')
            port.execute(request)
            self.requests.transition(request,'awaiting_readback',self.clock())
            readback=port.readback(request)
            if readback is not True:raise StateError('unified_native_exact_readback_required')
            op=self.requests.transition(request,'complete',self.clock())
            return dict(state=op['state'],label=LABELS[op['state']],replay=False,exact_readback=True)
        except Exception:
            latest,_,_=self.requests.read();state=latest['operations'][request.operation_identity]['state']
            if state in {'executing','awaiting_readback'}:
                self.requests.transition(request,'partial_unknown',self.clock())
            raise StateError('unified_partial_unknown_readback_only') from None

    def recover_readback(self, case, payload):
        """Explicit recovery never calls native execute/append or issues authority."""
        request=OperationRequest.parse(payload)
        if (prohibited(case,recovery=True) or not self.config.allows(case,request.expected_state_revision)
                or self.requests is None or self.requests.store_id!=self.config.request_store_id):
            raise StateError('unified_recovery_forbidden')
        before,_,_=self.requests.read();old=before['operations'].get(request.operation_identity)
        if not old or old['fingerprint']!=request.fingerprint():raise StateError('unified_recovery_binding_changed')
        if request.case_id!=case.case_id or request.current_snapshot_digest!=digest(current_snapshot(case)):
            raise StateError('unified_recovery_snapshot_changed')
        port=self.adapters.get((case.kind,request.requested_action))
        if port is None or port.readback(request) is not True:
            return dict(state='partial_unknown',label=LABELS['partial_unknown'],accounting_writes=0)
        # A lost acceptance response never dispatched. It cannot be filled in.
        if old['state'] not in {'partial_unknown','awaiting_readback','executing','complete'}:
            raise StateError('unified_recovery_state_held')
        if old['state']=='executing':self.requests.transition(request,'awaiting_readback',self.clock())
        if old['state']!='complete':self.requests.transition(request,'complete',self.clock())
        return dict(state='complete',label=LABELS['complete'],accounting_writes=0,exact_readback=True)
