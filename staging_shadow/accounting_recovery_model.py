"""Step C ONLY: memory evidence projection, inert spy, read-only reconciliation.

Never a native claim/grant, writer adapter, recovery command or production store.
The projection names come from the existing implementations; auto intake has no
equivalent native durable claim and deliberately cannot produce a proof here.
"""
from copy import deepcopy
from html import escape
import json

from app.drive_run_state import StateError
from app.iphone_accounting_ownership import parse_ownership
from app.pdf_intake_registry import strong_etag
from app.pdf_intake_runner import exact_readback, ledger_snapshot
from app.receipt_confirmation import ReceiptConfirmation
from app.receipt_plan.posting import outcome
from app.unified_receipt_review import digest

SCHEMA = 'synthetic-recovery-evidence-v1'
POINTS = ('before_shared_claim', 'after_shared_claim', 'after_native_validation',
          'before_native_claim', 'after_native_claim', 'before_writer_start',
          'after_writer_start', 'after_partial_ledger', 'after_native_complete', 'after_shared_update')
PROFILES = {
    'receipt_confirmation': ('waiting', 'pending', 'applied'),
    'pdf_intake': ('absent', 'claimed', 'complete'),
    'pdf_unit_processing': ('absent', 'pending', 'applied'),
    'p14_posting': ('unused', 'claimed', 'complete'),
    'receipt_unit_posting': ('unused', 'claimed', 'complete'),
    'unsupported_auto': ('unsupported', 'unsupported', 'unsupported'),
}


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode()


class Crash(BaseException):
    """Abrupt termination: bypass Exception cleanup, unlike a handled timeout."""


class SnapshotDB:
    """Read view only. No append/update/delete interface."""
    def __init__(self, snapshot): self.snapshot = snapshot
    def get_raw(self, region): return deepcopy(self.snapshot[0][region.split("'")[1]])
    def get_formula(self, region): return deepcopy(self.snapshot[1][region.split("'")[1]])
    def get(self, region): return deepcopy(self.snapshot[0][region.split("'")[1]][1:])


class LedgerReaders:
    def __init__(self, snapshot): self.snapshot = snapshot
    def sheet_rows(self, region, *, render='UNFORMATTED_VALUE'):
        return deepcopy(self.snapshot[1 if render == 'FORMULA' else 0][region.split("'")[1]])


class EvidenceModel:
    def __init__(self, io, requests, auth, binding_reader, plan_reader, *, enabled=False):
        if (enabled is not True or getattr(io, 'synthetic_memory_only', False) is not True
                or getattr(requests.io, 'synthetic_memory_only', False) is not True
                or not requests.synthetic_accounting):
            raise StateError('synthetic_recovery_disabled')
        self.io, self.requests, self.auth = io, requests, auth
        self.binding_reader, self.plan_reader = binding_reader, plan_reader
        self.fault = None
        self.read_hook = None

    def read(self):
        raw, tag = self.io.read_versioned()
        if type(raw) is not bytes or not strong_etag(tag): raise StateError('synthetic_evidence_unavailable')
        try:
            def unique(pairs):
                result = {}
                for key, value in pairs:
                    if key in result: raise ValueError()
                    result[key] = value
                return result
            state = json.loads(raw, object_pairs_hook=unique)
            if set(state) != {'schema', 'generation', 'records', 'ledger'} or state['schema'] != SCHEMA:
                raise ValueError()
            if type(state['generation']) is not int or state['generation'] < 0 or type(state['records']) is not dict:
                raise ValueError()
            for op, r in state['records'].items():
                if set(r) != {'entry','key','request_id','ownership','fingerprint','profile','native_state',
                               'writer_starts','ledger','before','expected','trace','bindings','validated','intent_binding'}:
                    raise ValueError()
                o = parse_ownership(r['ownership'])
                if (op != o.operation_identity or r['profile'] not in PROFILES
                        or r['native_state'] not in set(PROFILES[r['profile']]) | {'unknown'}
                        or type(r['writer_starts']) is not int or r['writer_starts'] not in (0,1)
                        or type(r['validated']) is not bool or not isinstance(r['trace'],list)
                        or r['ledger'] != state['ledger']):
                    raise ValueError()
            return state, raw, tag
        except (ValueError, TypeError, KeyError, AttributeError):
            raise StateError('synthetic_evidence_corrupt') from None

    def _update(self, op, mutate):
        # Existing versioned IO semantics; no retry, independent lock or CAS engine.
        before, raw, tag = self.read(); after = deepcopy(before)
        mutate(after['records'][op]); after['generation'] += 1
        # Every ingress observes ONE projected ledger in the same atomic image.
        after['ledger']=deepcopy(after['records'][op]['ledger'])
        for row in after['records'].values():row['ledger']=deepcopy(after['ledger'])
        self.io.replace_versioned(raw, tag, encoded(after))
        if self.read()[0] != after: raise StateError('synthetic_evidence_readback_unknown')

    def _fault(self, point):
        if self.fault: self.fault(point)

    def checkpoint(self, point, entry, key, observation):
        op = observation.operation_identity
        if point == 'after_shared_claim':
            shared = self.requests.read()[0]['operations'][op]
            state, raw, tag = self.read()
            if op in state['records']: raise StateError('synthetic_evidence_duplicate_attempt')
            plan, baseline, profile = self.plan_reader(entry, key)
            if baseline != state['ledger']: raise StateError('synthetic_native_ledger_projection_changed')
            initial = PROFILES[profile][0]
            state['records'][op] = dict(entry=entry,key=key,request_id=shared['request']['request_id'],
                ownership=observation.value(),fingerprint=shared['fingerprint'],profile=profile,
                native_state=initial,writer_starts=0,ledger=deepcopy(baseline),before=deepcopy(baseline),
                expected=deepcopy(plan),trace=[],bindings=self.binding_reader(entry,key),validated=False,intent_binding=None)
            state['generation'] += 1
            self.io.replace_versioned(raw,tag,encoded(state))
            if self.read()[0] != state: raise StateError('synthetic_evidence_readback_unknown')
        if point != 'before_shared_claim':
            def mark(r):
                r['trace'].append(point)
                if point == 'after_native_validation': r['validated'] = True
            self._update(op,mark)
        self._fault(point)

    def observe_boundary(self, entry, key, op):
        """Called once by the existing guarded probe; NEVER calls a writer."""
        r = self.read()[0]['records'][op]
        if not r['validated'] or (r['entry'],r['key']) != (entry,key):
            raise StateError('synthetic_evidence_not_validated')
        def point(name):
            self._update(op,lambda row:row['trace'].append(name));self._fault(name)
        point('before_native_claim')
        def claim(row):
            if row['native_state'] != PROFILES[row['profile']][0] or row['writer_starts']:
                raise StateError('synthetic_native_duplicate_claim')
            row['native_state'] = PROFILES[row['profile']][1]
            if row['profile']!='unsupported_auto':row['intent_binding']=self._intent(row)
        self._update(op,claim)
        point('after_native_claim')
        point('before_writer_start')
        def start(row):
            if row['writer_starts']: raise StateError('synthetic_double_probe')
            row['writer_starts'] = 1
        self._update(op,start)
        try:
            point('after_writer_start')
            self._update(op,lambda row:self._project_rows(row,partial=True))
            point('after_partial_ledger')
            self._update(op,lambda row:self._project_rows(row,partial=False))
            self._update(op,lambda row:row.update(native_state=PROFILES[row['profile']][2]))
            point('after_native_complete')
        except Exception:
            # posting/unit_posting catches an incomplete write and holds unknown;
            # legacy/PDF/grouped retain pending. Crash bypasses this handler.
            if r['profile'] in {'p14_posting','receipt_unit_posting'}:
                latest=self.read()[0]['records'][op]
                if self._ledger_outcome(latest) != 'complete':
                    self._update(op,lambda row:row.update(native_state='unknown'))
            raise

    @staticmethod
    def _intent(row):
        # A synthetic projection binding only; never a native proof/schema.
        return digest([row['ownership'],row['request_id'],row['fingerprint'],row['expected']])

    @staticmethod
    def _project_rows(row, *, partial):
        # Direct synthetic observation projection, NOT a fake append API.
        targets=[(o['table'],r) for o in row['expected']['operations'] for r in o['rows']]
        if partial: targets=targets[:1]
        for title,wanted in targets:
            for part in row['ledger']:
                part[title]=[r for r in part[title] if not r or r[0]!=wanted[0]]+[deepcopy(wanted)]

    @staticmethod
    def _ledger_outcome(r):
        result=EvidenceModel._native_ledger_outcome(r)
        if result=='complete':
            expected=deepcopy(r);expected['ledger']=deepcopy(r['before'])
            EvidenceModel._project_rows(expected,partial=False)
            if expected['ledger']!=r['ledger']:return 'partial_or_unknown'
        return result

    @staticmethod
    def _native_ledger_outcome(r):
        profile=r['profile'];db=SnapshotDB(r['ledger'])
        if profile in {'p14_posting','receipt_unit_posting'}:
            rows=[(o['table'],row) for o in r['expected']['operations'] for row in o['rows']]
            state,_=outcome(LedgerReaders(r['ledger']),{'rows':rows},
                {'before_ledger_digest':digest(ledger_snapshot(SnapshotDB(r['before'])))})
            return {'written':'complete','not_written':'not_written','partial':'partial_or_unknown'}[state]
        if profile == 'receipt_confirmation':
            rows=[(o['table'],row) for o in r['expected']['operations'] for row in o['rows']]
            # _complete uses existing same_row/identity checks on a detached view.
            from types import SimpleNamespace
            svc=SimpleNamespace(db=db)
            if ReceiptConfirmation._complete(svc,rows):
                # Native _complete alone does not prove all unrelated rows unchanged.
                expected=deepcopy(r);expected['ledger']=deepcopy(r['before'])
                EvidenceModel._project_rows(expected,partial=False)
                return 'complete' if expected['ledger']==r['ledger'] else 'partial_or_unknown'
            return 'not_written' if r['ledger']==r['before'] else 'partial_or_unknown'
        if profile == 'unsupported_auto':
            # Coverage of the observed plan, NOT native completion/intent proof.
            # Include buyback income and the import commit marker, not just the
            # receipt or three expense tables. Missing native evidence remains
            # UNKNOWN even when every projected value is present.
            if r['ledger']==r['before']:return 'not_written'
            expected=deepcopy(r);expected['ledger']=deepcopy(r['before'])
            EvidenceModel._project_rows(expected,partial=False)
            return 'complete' if expected['ledger']==r['ledger'] else 'partial_or_unknown'
        return exact_readback(db,r['expected'],digest(ledger_snapshot(SnapshotDB(r['before']))))

    def report(self, request_id, context=None):
        """Explicit read-back/GET data: NEVER changes any of the three stores."""
        self.auth._read(context)
        result=dict(synthetic_only=True,native_authority=False,accounting_permission=False,
            classification='UNKNOWN',quarantined=True,retry_allowed=False,shared_state=None,
            native_state=None,ledger_outcome=None,reason='evidence_unavailable',review={})
        try:
            shared,raw,tag=self.requests.read(); evidence,eraw,etag=self.read()
            ops=[o for o in shared['operations'].values() if o['request']['request_id']==request_id]
            if len(ops)!=1: return dict(result,reason='no_protected_shared_claim')
            outer=ops[0];req=outer['request'];o=parse_ownership(req['ownership'])
            if req['owner']!=self.auth.owner or o.owner!=self.auth.owner:
                raise StateError('synthetic_recovery_owner_mismatch')
            result['shared_state']=outer['state']
            r=evidence['records'].get(o.operation_identity)
            if not r: return dict(result,reason='missing_native_or_writer_evidence')
            current=self.binding_reader(r['entry'],r['key'])
            result['review']=dict(case_id=o.case_id,source=o.source,unit=o.unit,input_snapshot=o.input_snapshot,
                authority_revision=o.authority_revision,ledger_targets=o.ledger_targets,
                writer_starts=r['writer_starts'],checkpoint=r['trace'][-1] if r['trace'] else None)
            result['review']['ledger']=[dict(table=operation['table'],transaction_id=row[0],expected=row,
                before=[x for x in r['before'][0][operation['table']][1:] if x and x[0]==row[0]],
                observed=[x for x in r['ledger'][0][operation['table']][1:] if x and x[0]==row[0]],
                formulas=[x for x in r['ledger'][1][operation['table']][1:] if x and x[0]==row[0]])
                for operation in r['expected']['operations'] for row in operation['rows']]
            result['review'].update(before_ledger_digest=digest(r['before']),observed_ledger_digest=digest(r['ledger']),
                pending_state=r['native_state'],checkpoints=r['trace'])
            if (r['request_id']!=request_id or r['fingerprint']!=outer['fingerprint']
                    or r['ownership']!=req['ownership'] or current!=r['bindings']):
                return dict(result,reason='binding_or_observation_changed')
            initial,claimed,complete=PROFILES[r['profile']]
            expected_intent=None if r['native_state']==initial else self._intent(r)
            if r['profile']!='unsupported_auto' and r['intent_binding']!=expected_intent:
                return dict(result,reason='pending_intent_binding_unknown')
            if self.read_hook: self.read_hook()
            ledger=self._ledger_outcome(r)
            result.update(native_state=r['native_state'],ledger_outcome=ledger)
            # Co-observation: source/partition/native/ledger/pending + both strong
            # versioned stores must remain exactly stable across this read-back.
            if ((raw,tag)!=self.requests.read()[1:] or (eraw,etag)!=self.read()[1:]
                    or current!=self.binding_reader(r['entry'],r['key'])):
                return dict(result,reason='changed_during_readback')
            if ledger=='partial_or_unknown':
                return dict(result,classification='PARTIAL',reason='partial_or_conflicting_ledger')
            if r['profile']=='unsupported_auto': return dict(result,reason='native_durable_claim_unavailable')
            if outer['state']=='complete' and (r['native_state']!=complete or ledger!='complete'):
                return dict(result,reason='shared_native_ledger_mismatch')
            if (ledger=='complete' and r['native_state']==complete and r['writer_starts']==1 and r['validated']
                    and outer['state'] in {'executing','awaiting_readback','partial_unknown','complete'}
                    and 'after_native_complete' in r['trace']):
                return dict(result,classification='COMPLETE',quarantined=False,reason='exact_native_and_ledger_readback')
            if (outer['state'] in {'accepted','executing','partial_unknown'} and ledger=='not_written'
                    and r['native_state'] in {initial,claimed} and r['writer_starts']==0):
                return dict(result,classification='NOT_EXECUTED',quarantined=False,
                    reason='protected_claim_intent_zero_writer_and_full_baseline')
            return dict(result,reason='execution_not_proven')
        except Exception:
            # No raw exception, credential, internal URL or partial success leaks.
            return dict(result,classification='UNKNOWN',quarantined=True,reason='invalid_or_incomplete_evidence')

    @staticmethod
    def render_report(result):
        return '<!doctype html><meta charset="utf-8"><h1>合成復旧照合</h1><p>再実行は禁止。原本・入力・台帳・pendingを人間が確認してください。</p><pre>'+escape(json.dumps(result,ensure_ascii=False,indent=2))+'</pre>'
