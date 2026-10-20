"""M5/M6 memory composition; no workflow, host, credential or production wiring.

M6 operations are observations/drain plans, NEVER financial permissions. Existing
receipt requests/CAS schemas remain unchanged. The whole-ledger fence below is
an admission check on their existing shared CAS, not another lock/CAS engine.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass
from hashlib import sha256
import inspect
from pathlib import Path

from app.drive_run_state import StateError
from app.receipt_confirmation_contract import memory_only, snapshot
from .auto_native_evidence import load
from .mandatory_receipt_gate import MandatoryReceiptComposition, _Port

BASE = '540427f865c47066a7d8b3e3a0e0c7ae714fd7c8'
HISTORICAL = 'c29ecc44e1fb9fcc854cde1da5cce940e633e8fa'
PINS = {
    'staging_shadow/mandatory_receipt_gate.py': '23b54d625ac0afd4aa553569123ec71f0c7e4cf3f26461e72fabb802774141ca',
    'staging_shadow/synthetic_auto_writer.py': '1a2d0a147532e50b1a5bc96c1124792aaefc1a987fdcc7608f2945d323859b38',
    'staging_shadow/synthetic_draft_writer.py': 'c6ce307834c16cc252d8292bae1ac1d46a754c0deff5c093fb53ba5b9de9bc90',
    'app/unified_receipt_write.py': 'ded31e5c9ecda57b777040f49abc75bb0f4c4f334ae6018c2d47698298898f36',
}
UPDATES = tuple('A'+str(n).zfill(2) for n in range(1,14))
LEGACY = ('R03','R04','R06','R07','R08','R09','R10','R11','R12','R14','R15')
DRAIN = (
    'individual_human_approval', 'close_new_admissions_all_hosts_and_fixed_artifacts',
    'enumerate_queued_jobs_and_native_inflight', 'wait_without_unapproved_cancel',
    'retain_pending_applied_UNKNOWN_and_fences', 'independent_source_native_all_ledger_readback',
    'prove_credentials_and_direct_SDK_paths_closed', 'connect_existing_native_authority_and_strong_CAS',
    'rollback_to_readonly_never_unconditional_writer',
)


def refuse(reason):
    raise StateError('ledger_boundary_'+reason)


def _memory(io):
    memory_only(io)
    if any(hasattr(io,k) for k in ('credentials','client','service','session','drive','sheets')):
        refuse('external_transport_forbidden')


def _ledger(io):
    raw,tag=io.read_versioned();v=load(raw)
    if set(v)!={'complete','snapshot'} or v['complete'] is not True:
        refuse('full_ledger_required')
    from app.pdf_intake_registry import strong_etag
    if not strong_etag(tag):refuse('strong_tag_required')
    return snapshot(v['snapshot']),raw,tag


def verify_pins():
    root=Path(__file__).resolve().parents[1]
    for name,pin in PINS.items():
        if sha256((root/name).read_bytes().replace(b'\r\n',b'\n')).hexdigest()!=pin:
            refuse('reviewed_runtime_pin_changed')


@dataclass
class _Run:
    runtime: object
    ledger: tuple
    retained: tuple
    uncertain: bool=False


class _BoundaryPort:
    synthetic_memory_only=True

    def __init__(self, boundary, backend, role):
        self._boundary,self._backend,self._role=boundary,backend,role

    def read_versioned(self):return self._backend.read_versioned()

    def replace_versioned(self,before,tag,after):
        b=self._boundary;run=b._active.get()
        # A raw SDK/old writer cannot enter merely because a receipt is active.
        if (run is None or run.uncertain or inspect.currentframe().f_back.f_code is not _Port.replace_versioned.__code__
                or run.runtime._receipt._active.get() is None):
            refuse('mandatory_receipt_context_required')
        try:
            b._fresh(run)
            if self._role=='shared':
                old,new=load(before),load(after)
                added=set(new['operations'])-set(old['operations'])
                if added:
                    if len(added)!=1:refuse('single_operation_required')
                    for previous in old['operations'].values():
                        if previous['state']!='complete' or 'ownership' not in previous['request']:
                            refuse('whole_ledger_inflight_or_UNKNOWN')
                        b._completed_native(previous,run)
            self._backend.replace_versioned(before,tag,after)
            current,new_tag=self._backend.read_versioned()
            if current!=after or new_tag==tag or self._backend.read_versioned()!=(current,new_tag):
                refuse('CAS_readback_UNKNOWN')
            if self._role=='ledger':
                _ledger(self._backend)
                run.ledger=(current,new_tag)
        except BaseException:
            run.uncertain=True
            raise


class _ReceiptHandle:
    def __init__(self,runtime,entry,key):self._runtime,self._entry,self._key=runtime,entry,key
    def process_bytes(self,*args,**kwargs):
        r=self._runtime
        with r._boundary._run(r):
            return r._receipt.make_receipt_pipeline(self._entry,self._key).process_bytes(*args,**kwargs)
    def _materialize_result(self,*args,**kwargs):refuse('bare_writer_closed')


class ReceiptRuntime:
    """Only factories created by LedgerExecutionBoundary construct this host."""
    def __init__(self,boundary,receipt):self._boundary,self._receipt=boundary,receipt
    @property
    def shared(self):return self._receipt.shared
    @property
    def ledger(self):return self._receipt.ledger
    @property
    def auto_ledger(self):return self._receipt.auto_ledger
    @property
    def journal(self):return self._receipt.journal
    @property
    def adapter(self):return self._receipt.adapter
    @property
    def writer(self):return self._receipt.writer
    def r1_native_port(self,*args,**kwargs):return self._receipt.r1_native_port(*args,**kwargs)
    def make_receipt_pipeline(self,entry,key):
        self._receipt.make_receipt_pipeline(entry,key)
        return _ReceiptHandle(self,entry,key)
    def submit_r1(self,*args,**kwargs):
        with self._boundary._run(self):return self._receipt.submit_r1(*args,**kwargs)
    def report(self,*args,**kwargs):return self._receipt.report(*args,**kwargs)
    def legacy_confirmation(self,*args,**kwargs):return self._receipt.legacy_confirmation(*args,**kwargs)


class LedgerExecutionBoundary:
    def __init__(self,*,ledger_id,shared_io,ledger_io,retained_update_ports,
                 enabled=False,isolated_synthetic=False):
        if enabled is not True or isolated_synthetic is not True or not ledger_id.startswith('synthetic-'):
            refuse('isolation_required')
        if type(retained_update_ports) is not tuple:refuse('explicit_history_inventory_required')
        for io in (shared_io,ledger_io,*retained_update_ports):_memory(io)
        # This declaration is supplied only by the lock-backed memory fixture.
        # It is not proof of live Sheets CAS; live read-back alone cannot qualify.
        if any(getattr(io,'supports_strong_cas',False) is not True for io in (shared_io,ledger_io)):
            refuse('strong_CAS_backend_required')
        if shared_io is ledger_io:refuse('independent_stores_required')
        verify_pins();self.ledger_id=ledger_id
        self._shared,self._ledger=shared_io,ledger_io
        self._retained=retained_update_ports
        self._active=ContextVar('isolated_ledger_execution',default=None)
        self._shared_port=_BoundaryPort(self,shared_io,'shared')
        self._ledger_port=_BoundaryPort(self,ledger_io,'ledger')
        self._runtimes=[]

    def compose_receipts(self,**ports):
        if any(k in ports for k in ('shared_io','ledger_io','enabled','isolated_synthetic')):
            refuse('shared_store_override_forbidden')
        c=MandatoryReceiptComposition(shared_io=self._shared_port,ledger_io=self._ledger_port,
            enabled=True,isolated_synthetic=True,**ports)
        r=ReceiptRuntime(self,c);self._runtimes.append(r);return r

    def _retained_observation(self):
        frames=tuple(p.read_versioned() for p in self._retained)
        # No generic inference of native bank/card/category completion. Until
        # a native-specific adapter is reviewed, ANY retained update doc holds.
        # Empty inventory is valid only for newly provisioned memory fixtures.
        if frames:refuse('retained_update_requires_native_reconciliation')
        return frames

    @contextmanager
    def _run(self,runtime):
        if runtime not in self._runtimes or self._active.get() is not None:refuse('runtime_required')
        _,raw,tag=_ledger(self._ledger)
        run=_Run(runtime,(raw,tag),self._retained_observation());token=self._active.set(run)
        try:yield
        except BaseException:
            run.uncertain=True
            raise
        finally:self._active.reset(token)

    def _fresh(self,run):
        if run.uncertain or self._retained_observation()!=run.retained:
            refuse('retained_update_changed')
        if _ledger(self._ledger)[1:]!=run.ledger:
            refuse('all_ledger_version_changed')

    def _completed_native(self,previous,run):
        """A shared 'complete' label alone never clears a ledger-wide hold."""
        c=run.runtime._receipt;request=previous['request'];op=request['operation_identity']
        rows,_,_=_ledger(self._ledger)
        record=c.journal.read()[0]['operations'].get(op)
        planned=[]
        if record:
            req=record['request']
            if (record['phase']!='complete' or record['shared_claim']['fingerprint']!=previous['fingerprint'] or
                    req['shared_request_id']!=request['request_id']):refuse('completed_native_mismatch')
            expected=deepcopy(req['before'])
            from app.unified_receipt_review import digest
            for operation in req['plan']['operations']:
                planned.extend((operation['table'],r) for r in operation['rows'])
                for part in expected:part[operation['table']].extend(deepcopy(operation['rows']))
            if digest(expected)!=record['completion']['ledger_digest']:refuse('completed_native_mismatch')
        else:
            for store in c._r1_stores.values():
                record=store.read()[0]['operations'].get(op)
                if record:
                    if (record.get('phase')!='complete_fixture' or record['claim']['fingerprint']!=previous['fingerprint'] or
                            record['request']['shared_request_id']!=request['request_id']):
                        refuse('completed_native_mismatch')
                    planned=record['request']['plan'];break
        if not planned:refuse('complete_shared_without_native_proof')
        for table,wanted in planned:
            for part in rows:
                if [r for r in part.get(table,[]) if r[0]==wanted[0]]!=[wanted]:
                    refuse('completed_target_changed')

    def inspect_update(self,entry,*,ledger_id,source_identity,native_io,targets):
        """Frozen SERVER/native target projection; never a new authority schema.

        targets: table, stable id (optional only for table-wide maintenance),
        observed row number and columns. Row numbers are diagnostics, not IDs.
        Even independent targets remain financially disconnected/drain-required.
        """
        if entry not in UPDATES:refuse('unmapped_update_entry')
        _memory(native_io);rows,raw,tag=_ledger(self._ledger)
        native=native_io.read_versioned()
        if type(source_identity) is not dict or not source_identity or type(targets) is not list or not targets:
            refuse('unmapped_source_or_target')
        details=[]
        for target in deepcopy(targets):
            if set(target)!={'table','id','row','columns'}:refuse('target_shape_unknown')
            table,identity,number,columns=(target[k] for k in ('table','id','row','columns'))
            if (not isinstance(table,str) or not table or not isinstance(identity,str) or
                    not isinstance(columns,list) or any(type(c) is not int or c<0 for c in columns)):
                refuse('target_shape_unknown')
            present=[(n+2,r) for n,r in enumerate(rows[0].get(table,[])) if r[0]==identity] if identity else []
            if number is not None and (type(number) is not int or number<2 or not identity or
                    len(present)!=1 or present[0][0]!=number):refuse('row_identity_changed')
            details.append(dict(target,observed=deepcopy(present),table_wide=not identity,
                formulas=[(n+2,deepcopy(r)) for n,r in enumerate(rows[1].get(table,[])) if identity and r[0]==identity]))
        if native_io.read_versioned()!=native or self._ledger.read_versioned()!=(raw,tag):
            refuse('independent_observation_changed')
        relation='shared_full_snapshot' if ledger_id==self.ledger_id else 'independent_ledger_unconnected'
        return dict(classification='DRAIN_REQUIRED',entry=entry,relation=relation,source_identity=deepcopy(source_identity),
            targets=details,before=(sha256(raw).hexdigest(),tag),native=(sha256(native[0]).hexdigest(),native[1]),
            requirements=list(DRAIN),financial_dispatches=0,retry_allowed=False,synthetic_only=True,
            production_status='本番未確認')

    def execute_update(self,*args,**kwargs):refuse('M6_financial_stage_drain_required')

    def inspect_daily_correction(self,store,db,request_id,*,native_io):
        """Representative adapter reuses the existing fixed-ID native readers.

        No prepare/apply, projection write, permission minting or pending repair.
        A native queued item still yields DRAIN_REQUIRED, never a financial lease.
        """
        from app.daily_corrections import DailyCorrections
        from app.monthly_projection import fingerprint
        service=DailyCorrections(store,db);before=service._requests()
        item=before['requests'].get(request_id)
        if not item or item.get('state')!='queued':refuse('native_correction_not_new_queued')
        number,row=service._read_target(item['expense_id'])
        if fingerprint(row)!=item['before']:refuse('native_correction_before_changed')
        if load(native_io.read_versioned()[0])!=before:refuse('native_correction_store_mismatch')
        report=self.inspect_update('A10',ledger_id=self.ledger_id,native_io=native_io,
            source_identity=dict(namespace='daily_corrections',request_id=request_id,expense_id=item['expense_id']),
            targets=[dict(table='支出明細',id=item['expense_id'],row=number,columns=[c for c,_ in item['cells']])])
        if service._requests()!=before or service._read_target(item['expense_id'])!=(number,row):
            refuse('native_correction_changed')
        return dict(report,adapter='existing_DailyCorrections_readers')

    def legacy_launch(self,entry,*,checkout_sha,origin):
        # No callback, credentials, arbitrary module import or dispatch exists.
        # This controls ONLY this new host, not a historical host outside it.
        refuse('legacy_launch_unmigrated_'+str(entry))

    def denied_sink(self):return DeniedSink()


class DeniedSink:
    """Memory-side terminal boundary for old DB/native/SDK reproductions."""
    synthetic_memory_only=True
    def __init__(self):self.denials=0
    def _deny(self,*args,**kwargs):
        self.denials+=1;refuse('uncontrolled_sink_no_financial_capability')
    append=append_raw=update_row=update_row_raw=update_rows=set_raw_range=_deny
    update_expense_fields=update_expense_categories=write=save=replace=replace_versioned=_deny
    release_after_reconciliation=initialize=_deny
    def spreadsheets(self):return self
    def values(self):return self
    update=batchUpdate=clear=_deny
