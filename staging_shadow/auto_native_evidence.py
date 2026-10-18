"""Synthetic-only whole-source execution journal. No native writer/authority API.

Shared claim, this journal and ledger observations are THREE independent stores.
ConditionalRequests.replace supplies the existing CAS and exact read-back engine.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from html import escape
import json
from uuid import UUID

from app.drive_run_state import StateError
from app.iphone_accounting_ownership import parse_ownership, hashed, relation
from app.iphone_accounting_gate import AccountingProbeRequest
from app.pdf_intake_registry import strong_etag
from app.pdf_intake_runner import exact_readback, ledger_snapshot
from app.unified_receipt_review import digest
from app.unified_receipt_write import ConditionalRequests, MAX_BYTES
from app.bank_income import INCOME_SHEET, INCOME_HEADERS
from app.sheets import HEADERS
from app.utils import canonical_hash
from staging_shadow.accounting_recovery_model import SnapshotDB

SCHEMA='synthetic-auto-native-evidence-v1'
LEDGER_SCHEMA='synthetic-auto-ledger-observation-v1'
PHASES={'claimed','writer_ready','writer_started','complete','partial','unknown'}
POINTS=('before_shared_claim','after_shared_claim','before_native_evidence','after_native_evidence',
        'after_native_validation','before_writer_start_evidence','after_writer_start_evidence',
        'after_probe','after_partial_ledger','after_all_ledger','before_completion_evidence',
        'after_native_complete','after_shared_update')


def fail(reason):raise StateError('synthetic_auto_'+reason)


def load(raw):
    def unique(pairs):
        out={}
        for k,v in pairs:
            if k in out:raise ValueError()
            out[k]=v
        return out
    if type(raw) is not bytes or len(raw)>MAX_BYTES:raise ValueError()
    return json.loads(raw,object_pairs_hook=unique)


def request(value):
    """A server-frozen candidate, not browser fields or an authority factory."""
    try:
        v=deepcopy(value)
        if set(v)!={'schema','synthetic_only','native_request_id','shared_request_id','expected_revision',
                    'ownership','kind','inputs','input_digest','plan','plan_digest','before','before_digest','binding'}:
            raise ValueError()
        o=parse_ownership(v['ownership'])
        if (v['schema']!=SCHEMA or v['synthetic_only'] is not True or o.scope!='whole' or o.unit is not None
                or v['kind'] not in {'normal_auto','buyback_income'}
                or any(str(UUID(v[k]))!=v[k] for k in ('native_request_id','shared_request_id'))
                or type(v['expected_revision']) is not int or v['expected_revision']<0
                or not isinstance(v['inputs'],dict) or digest(v['inputs'])!=v['input_digest']
                or digest(v['inputs']['parsed'])!=o.input_snapshot
                or digest(v['plan'])!=v['plan_digest'] or digest(v['before'])!=v['before_digest']
                or not isinstance(v['binding'],dict)):
            raise ValueError()
        operations=v['plan']['operations'];sid=o.source['source_id']
        expected_tables=['レシート',INCOME_SHEET if v['kind']=='buyback_income' else '支出明細','取込データ']
        if [x['table'] for x in operations]!=expected_tables:raise ValueError()
        ids=[]
        for operation in operations:
            if set(operation)!={'table','rows'} or not operation['rows']:raise ValueError()
            header=INCOME_HEADERS if operation['table']==INCOME_SHEET else HEADERS[operation['table']]
            for row in operation['rows']:
                if type(row) is not list or len(row)!=len(header) or not isinstance(row[0],str) or not row[0]:raise ValueError()
                ids.append(row[0])
        if (len(set(ids))!=len(ids) or len(operations[0]['rows'])!=1 or len(operations[-1]['rows'])!=1
                or operations[0]['rows'][0][0]!='R-'+sid or operations[-1]['rows'][0][0]!='receipt:'+sid):
            raise ValueError()
        p=v['inputs']['parsed'];receipt=operations[0]['rows'][0];imported=operations[-1]['rows'][0]
        if (receipt[1:5]!=[p['date'],p['merchant'],p['total'],p['payment_method']]
                or receipt[6]!='解析済' or receipt[8]!=p['note']
                or imported[1]!=receipt[7] or imported[2:9]!=['receipt',sid,p['date'],p['merchant'],p['total'],p['payment_method'],'解析済']
                or imported[9:]!=['',canonical_hash(p),'']):raise ValueError()
        middle=operations[1]['rows']
        if v['kind']=='normal_auto':
            if p['transaction_kind']!='purchase' or len(middle)!=len(p['items']):raise ValueError()
            for index,(row,item) in enumerate(zip(middle,p['items']),1):
                if (row[0]!='R-'+sid+f'-{index:02d}' or row[1:]!=[p['date'],p['merchant'],item['name'],item['amount'],
                    item['major_category'],item['minor_category'],p['payment_method'],'receipt',receipt[0],imported[0],item['note'],'active']):raise ValueError()
        elif p['transaction_kind']!='buyback' or len(middle)!=1 or middle[0][1:5]!=[p['date'],p['total'],'その他確認済収入',p['merchant']]:raise ValueError()
        if ledger_outcome(v,v['before'])!='not_written':raise ValueError()
        return v
    except (ValueError,KeyError,TypeError,AttributeError):fail('request_invalid')


def ledger_view(io):
    raw,tag=io.read_versioned()
    try:
        v=load(raw)
        if (not strong_etag(tag) or set(v)!={'schema','generation','complete','snapshot'}
                or v['schema']!=LEDGER_SCHEMA or v['complete'] is not True
                or type(v['generation']) is not int or v['generation']<0):raise ValueError()
        validate_snapshot(v['snapshot'])
        return v['snapshot'],raw,tag
    except (ValueError,KeyError,TypeError):fail('ledger_observation_unknown')


def validate_snapshot(snapshot):
    tables=set(HEADERS)&{'レシート','支出明細','取込データ'} | {INCOME_SHEET}
    if type(snapshot) is not list or len(snapshot)!=2:raise ValueError()
    for part in snapshot:
        if set(part)!=tables:raise ValueError()
        for title,rows in part.items():
            header=INCOME_HEADERS if title==INCOME_SHEET else HEADERS[title]
            if not rows or rows[0]!=header or len(rows)>=10000:raise ValueError()
            if any(type(r) is not list or len(r)>len(header) for r in rows):raise ValueError()


def ledger_outcome(req,snapshot):
    """Existing exact_readback plus income extension; preserve ALL old rows."""
    validate_snapshot(snapshot);validate_snapshot(req['before'])
    expense_ops=[o for o in req['plan']['operations'] if o['table']!=INCOME_SHEET]
    baseline=ledger_snapshot(SnapshotDB(req['before']))
    status=exact_readback(SnapshotDB(snapshot),{'operations':expense_ops},digest(baseline))
    clean=deepcopy(snapshot);matched=present=total=0
    for operation in req['plan']['operations']:
        title=operation['table']
        for wanted in operation['rows']:
            total+=1
            pair=[[r for r in part[title][1:] if r and str(r[0])==str(wanted[0])] for part in snapshot]
            if any(pair):present+=1
            if digest(pair)==digest([[wanted],[wanted]]):matched+=1
            elif any(pair):return 'partial'
            for part in clean:part[title]=[r for r in part[title] if not r or str(r[0])!=str(wanted[0])]
    if digest(clean)!=req['before_digest'] or status=='partial_or_unknown':return 'partial'
    return 'complete' if matched==total else 'not_written' if present==0 else 'partial'


class NativeEvidenceJournal(ConditionalRequests):
    """New SYNTHETIC evidence schema over the UNCHANGED existing CAS engine."""
    def __init__(self,io,*,enabled=False):
        if enabled is not True or getattr(io,'synthetic_memory_only',False) is not True:fail('journal_disabled')
        super().__init__(io,'synthetic-auto-native-evidence')

    def read(self):
        raw,tag=self.io.read_versioned()
        try:
            v=load(raw)
            if (not strong_etag(tag) or set(v)!={'schema','revision','operations'} or v['schema']!=SCHEMA
                    or type(v['revision']) is not int or v['revision']<0 or type(v['operations']) is not dict):raise ValueError()
            native_ids=set();shared_ids=set()
            for op,r in v['operations'].items():
                if set(r)!={'request','fingerprint','phase','shared_claim','validation','writer_start','completion','events'}:raise ValueError()
                req=request(r['request'])
                if (op!=req['ownership']['operation_identity'] or r['fingerprint']!=digest(req)
                        or r['phase'] not in PHASES or not r['events'] or r['events'][-1]['phase']!=r['phase']
                        or req['native_request_id'] in native_ids or req['shared_request_id'] in shared_ids):raise ValueError()
                native_ids.add(req['native_request_id']);shared_ids.add(req['shared_request_id'])
                claim=r['shared_claim'];validation=r['validation']
                if (set(claim)!={'operation_identity','request_id','fingerprint','accepted_revision','observed_tag'}
                        or claim['operation_identity']!=op or claim['request_id']!=req['shared_request_id']
                        or not hashed(claim['fingerprint']) or claim['accepted_revision']!=req['expected_revision']+1
                        or not strong_etag(claim['observed_tag'])
                        or set(validation)!={'preflight_digest','authority_revision','mode','native_authority'}
                        or validation['mode']!='synthetic_preflight_only' or validation['native_authority'] is not False
                        or validation['authority_revision']!=req['ownership']['authority_revision']
                        or not hashed(validation['preflight_digest'])):raise ValueError()
                if r['phase'] in {'claimed','writer_ready'} and (r['writer_start'] is not None or r['completion'] is not None):raise ValueError()
                if r['phase'] in {'writer_started','complete','partial'} and r['writer_start']!=self.marker(req,r['shared_claim']):raise ValueError()
                if r['phase']=='complete' and (not isinstance(r['completion'],dict) or
                    set(r['completion'])!={'request_fingerprint','ledger_digest','plan_digest','writer_start_digest','classification'}
                    or r['completion']['request_fingerprint']!=r['fingerprint'] or r['completion']['plan_digest']!=req['plan_digest']
                    or r['completion']['classification']!='COMPLETE'
                    or r['completion']['writer_start_digest']!=digest(r['writer_start'])
                    or not hashed(r['completion']['ledger_digest'])):raise ValueError()
            return v,raw,tag
        except (ValueError,TypeError,KeyError,AttributeError):fail('journal_quarantined')

    @staticmethod
    def marker(req,claim):
        return dict(synthetic_only=True,sequence=1,request_fingerprint=digest(req),
                    shared_fingerprint=claim['fingerprint'],plan_digest=req['plan_digest'])

    def claim(self,req,shared,tag):
        req=request(req);before,raw,etag=self.read();after=deepcopy(before)
        op=req['ownership']['operation_identity']
        outer=AccountingProbeRequest.parse(shared['request'])
        if (shared['state']!='accepted' or shared['fingerprint']!=outer.fingerprint()
                or outer.operation_identity!=op or outer.ownership!=req['ownership']
                or outer.request_id!=req['shared_request_id'] or outer.expected_state_revision!=req['expected_revision']):
            fail('shared_claim_binding_invalid')
        for old in before['operations'].values():
            if (old['request']['native_request_id']==req['native_request_id']
                    or old['request']['shared_request_id']==req['shared_request_id']):fail('request_id_already_used')
            if relation(old['request']['ownership'],req['ownership'])!='independent':fail('operation_scope_already_owned')
        if op in before['operations']:fail('operation_already_claimed')
        after['operations'][op]=dict(request=req,fingerprint=digest(req),phase='claimed',
            shared_claim=dict(operation_identity=op,request_id=req['shared_request_id'],fingerprint=shared['fingerprint'],
                accepted_revision=req['expected_revision']+1,observed_tag=tag),
            validation=dict(preflight_digest=shared['request']['native_preflight_digest'],
                authority_revision=req['ownership']['authority_revision'],mode='synthetic_preflight_only',native_authority=False),
            writer_start=None,completion=None,events=[dict(phase='claimed')])
        after['revision']+=1
        self.replace(before,raw,etag,after)  # Inherited ConditionalRequests CAS + exact read-back.

    def move(self,req,phase,completion=None):
        before,raw,tag=self.read();after=deepcopy(before)
        r=after['operations'][req['ownership']['operation_identity']]
        if r['fingerprint']!=digest(req):fail('request_content_changed')
        allowed={'claimed':{'writer_ready','unknown'},'writer_ready':{'writer_started','unknown'},
                 'writer_started':{'complete','partial','unknown'},'complete':set(),'partial':set(),'unknown':set()}
        if phase not in allowed[r['phase']]:fail('phase_transition_forbidden')
        r['phase']=phase;r['events'].append(dict(phase=phase))
        if phase=='writer_started':r['writer_start']=self.marker(req,r['shared_claim'])
        if phase=='complete':r['completion']=deepcopy(completion)
        after['revision']+=1;self.replace(before,raw,tag,after)


class SyntheticAutoEvidenceAdapter:
    """Callback adapter for the already isolated SyntheticAccountingGate.

    Only a non-effect counter is built in. No arbitrary writer callback exists.
    Ledger observations are read-only; TEST faults may project them independently.
    """
    def __init__(self,journal,shared,auth,candidate_reader,binding_reader,ledger_io,*,enabled=False):
        if (enabled is not True or type(journal) is not NativeEvidenceJournal
                or type(shared) is not ConditionalRequests or not shared.synthetic_accounting
                or any(getattr(io,'synthetic_memory_only',False) is not True for io in (shared.io,ledger_io))):fail('adapter_disabled')
        self.journal,self.shared,self.auth=journal,shared,auth
        self.candidate_reader,self.binding_reader,self.ledger_io=candidate_reader,binding_reader,ledger_io
        self._attempt=ContextVar('synthetic_auto_attempt',default=None)
        self._ready=ContextVar('synthetic_auto_ready',default=None)
        self.fault=None;self.read_hook=None;self.probe_calls=[];self.boundary_calls=[]

    def point(self,name):
        if self.fault:self.fault(name)

    @contextmanager
    def attempt(self,entry,key,native_id,shared_id,revision,context=None):
        self.auth._post(context or {},'ACCOUNTING_POST',shared_id,None,revision)
        candidate=deepcopy(self.candidate_reader(entry,key))
        req=request(dict(candidate,native_request_id=native_id,shared_request_id=shared_id,expected_revision=revision))
        self.auth._post(context or {},'ACCOUNTING_POST',shared_id,req['ownership']['input_snapshot'],revision)
        for old in self.journal.read()[0]['operations'].values():
            if native_id==old['request']['native_request_id'] or shared_id==old['request']['shared_request_id']:
                if old['fingerprint']!=digest(req):fail('request_id_content_mismatch')
                yield dict(replay=True,report=self.report(native_id,context));return
        token=self._attempt.set((entry,key,req));ready=self._ready.set(None)
        try:yield None
        finally:self._ready.reset(ready);self._attempt.reset(token)

    def checkpoint(self,point,entry,key,observation):
        current=self._attempt.get()
        if current is None:
            if entry in {'normal_auto','buyback_income'}:fail('native_attempt_required')
            return  # Other legacy probes share the financial gate only.
        if (entry,key)!=current[:2] or observation.value()!=current[2]['ownership']:fail('server_candidate_changed')
        req=current[2]
        if point=='before_shared_claim':self.point(point)
        elif point=='after_shared_claim':
            self.point(point);self.point('before_native_evidence')
            state,_,tag=self.shared.read();shared=state['operations'][observation.operation_identity]
            if shared['state']!='accepted' or shared['request']['request_id']!=req['shared_request_id']:fail('shared_claim_required')
            self.journal.claim(req,shared,tag);self.point('after_native_evidence')
        elif point=='after_native_validation':
            self.journal.move(req,'writer_ready');self._ready.set(current)
            self.point(point)
        elif point=='after_shared_update':self.point(point)

    def observe_boundary(self,entry,key,op):
        self.boundary_calls.append((entry,key,op))
        current=self._attempt.get()
        if current is None:
            if entry in {'normal_auto','buyback_income'}:fail('native_attempt_required')
            return  # inert legacy spy, no writer/native adapter
        if self._ready.get() is not current or current[:2]!=(entry,key):fail('current_gate_lease_required')
        self._ready.set(None);req=current[2]
        if op!=req['ownership']['operation_identity'] or self.binding_reader(entry,key)!=req['binding']:fail('binding_changed_before_probe')
        self.point('before_writer_start_evidence')
        r=self.journal.read()[0]['operations'][op]
        outer=self.shared.read()[0]['operations'].get(op)
        if (not outer or outer['state']!='executing' or outer['fingerprint']!=r['shared_claim']['fingerprint']
                or self.binding_reader(entry,key)!=req['binding'] or ledger_view(self.ledger_io)[0]!=req['before']):fail('changed_before_start_marker')
        self.journal.move(req,'writer_started')  # Persist + exact read-back BEFORE counter.
        self.point('after_writer_start_evidence')
        fresh=self.shared.read()[0]['operations'].get(op)
        if (not fresh or fresh['state']!='executing' or fresh['fingerprint']!=r['shared_claim']['fingerprint']
                or self.binding_reader(entry,key)!=req['binding'] or ledger_view(self.ledger_io)[0]!=req['before']):fail('changed_after_start_marker')
        # This is the only writer surrogate. It has no append or external callback.
        self.probe_calls.append(op)
        try:
            self.point('after_probe');self.point('after_partial_ledger');self.point('after_all_ledger')
            self.point('before_completion_evidence')
            snapshot,lraw,ltag=ledger_view(self.ledger_io)
            if ledger_outcome(req,snapshot)!='complete':fail('complete_readback_required')
            if self.binding_reader(entry,key)!=req['binding']:fail('binding_changed_before_completion')
            if (lraw,ltag)!=ledger_view(self.ledger_io)[1:]:fail('ledger_changed_before_completion')
            self.journal.move(req,'complete',dict(request_fingerprint=digest(req),ledger_digest=digest(snapshot),
                plan_digest=req['plan_digest'],writer_start_digest=digest(NativeEvidenceJournal.marker(req,
                    self.journal.read()[0]['operations'][op]['shared_claim'])),classification='COMPLETE'))
            self.point('after_native_complete')
        except Exception:
            try:
                r=self.journal.read()[0]['operations'][op]
                if r['phase']=='writer_started':
                    status=ledger_outcome(req,ledger_view(self.ledger_io)[0])
                    self.journal.move(req,'partial' if status=='partial' else 'unknown')
            except Exception:pass  # Preserve the durable fence; never retry/gap-fill.
            raise

    def report(self,native_id,context=None):
        self.auth._read(context)
        result=dict(synthetic_only=True,real_native_complete=False,native_authority=False,accounting_permission=False,
            classification='UNKNOWN',quarantined=True,retry_allowed=False,reason='evidence_missing',review={})
        try:
            journal,jraw,jtag=self.journal.read();shared,sraw,stag=self.shared.read()
            rows=[r for r in journal['operations'].values() if r['request']['native_request_id']==native_id]
            if len(rows)!=1:return result
            r=rows[0];req=r['request'];o=parse_ownership(req['ownership'])
            if o.owner!=self.auth.owner:fail('owner_mismatch')
            observed,lraw,ltag=ledger_view(self.ledger_io)
            # The server reader identifies the saved canonical operation, not a
            # browser row or a supplied URL. Its returned candidate must match.
            binding=self.binding_reader(req['binding']['entry'],req['binding']['key'])
            result['review']=dict(source=o.source,scope=o.scope,input_snapshot=req['inputs'],plan=req['plan'],
                before=req['before'],observed=observed,native_phase=r['phase'],writer_start=r['writer_start'],
                shared_claim=r['shared_claim'],completion=r['completion'])
            outer=shared['operations'].get(o.operation_identity)
            result['review']['shared_state']=outer['state'] if outer else 'missing'
            if (binding!=req['binding'] or not outer or outer['fingerprint']!=r['shared_claim']['fingerprint']
                    or outer['request']['request_id']!=req['shared_request_id'] or outer['request']['ownership']!=req['ownership']
                    or outer['request']['native_preflight_digest']!=r['validation']['preflight_digest']):return dict(result,reason='binding_mismatch')
            status=ledger_outcome(req,observed)
            if self.read_hook:self.read_hook()
            if ((jraw,jtag)!=self.journal.read()[1:] or (sraw,stag)!=self.shared.read()[1:]
                    or (lraw,ltag)!=ledger_view(self.ledger_io)[1:]
                    or binding!=self.binding_reader(req['binding']['entry'],req['binding']['key'])):
                return dict(result,reason='changed_during_readback')
            if status=='partial':return dict(result,classification='PARTIAL',reason='partial_or_conflicting_ledger')
            if (r['phase']=='complete' and status=='complete' and r['completion']['ledger_digest']==digest(observed)
                    and outer['state'] in {'executing','awaiting_readback','partial_unknown','complete'}):
                return dict(result,classification='COMPLETE',quarantined=False,reason='synthetic_completion_evidence_only')
            if (r['phase'] in {'claimed','writer_ready'} and r['writer_start'] is None and status=='not_written'
                    and outer['state'] in {'accepted','executing','partial_unknown'}):
                return dict(result,classification='NOT_EXECUTED',quarantined=False,reason='durable_zero_start_and_exact_baseline')
            return dict(result,reason='outcome_not_proven')
        except Exception:return dict(result,classification='UNKNOWN',quarantined=True,reason='invalid_or_incomplete_evidence')

    @staticmethod
    def render_report(value):
        return '<!doctype html><meta charset="utf-8"><h1>完全合成の実行証跡照合</h1><p>本番完了証明・復旧権限ではありません。再実行は禁止です。</p><pre>'+escape(json.dumps(value,ensure_ascii=False,indent=2))+'</pre>'
