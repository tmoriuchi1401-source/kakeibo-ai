"""M5/M6 fresh memory resources and pinned historical, credential-free code."""
from copy import deepcopy
import importlib
import importlib.util
from pathlib import Path
import sys
from types import ModuleType
from uuid import uuid4
import os
import pytest

from mandatory_gate_fixtures import Rig as ReceiptRig, joint_r1
from auto_native_fixtures import NOW
from staging_shadow.ledger_execution_boundary import LedgerExecutionBoundary
from staging_shadow.mandatory_receipt_gate import RetainedHistoryReader
from app.receipt_confirmation_contract import encoded
from test_legacy_contract_poc import mem

ROOT=Path(__file__).resolve().parents[1]


class PolicyRig:
    """No receipt runtime is constructed for launch-only negative tests."""
    frozen=ReceiptRig.frozen
    dispatches=ReceiptRig.dispatches
    def __init__(self):
        from app.bank_income import INCOME_SHEET
        tables={k:[] for k in ('レシート','支出明細','取込データ',INCOME_SHEET)}
        self.backends=(mem(dict(schema='unified-receipt-requests-v1',revision=0,operations={})),
            mem(dict(complete=True,snapshot=[deepcopy(tables),deepcopy(tables)])),mem({}))
        for p in self.backends[:2]:p.supports_strong_cas=True
        self.boundary=LedgerExecutionBoundary(ledger_id='synthetic-joint-ledger',shared_io=self.backends[0],
            ledger_io=self.backends[1],retained_update_ports=(),enabled=True,isolated_synthetic=True)
        self.instances=[]


class Rig:
    call=ReceiptRig.call
    frozen=ReceiptRig.frozen
    dispatches=ReceiptRig.dispatches
    def __init__(self,*,retained=()):
        first=ReceiptRig();self.h=first.h;self.backends=first.backends;self.history=first.history
        for p in self.backends[:2]:p.supports_strong_cas=True
        self.boundary=LedgerExecutionBoundary(ledger_id='synthetic-joint-ledger',shared_io=self.backends[0],
            ledger_io=self.backends[1],retained_update_ports=retained,enabled=True,isolated_synthetic=True)
        self.instances=[];self.new(fresh=True)
    def new(self,*,fresh=False):
        h=self.h
        c=self.boundary.compose_receipts(auto_native_io=self.backends[2],auth=h.auth,reader=h.reader,
            preflight=h.preflight,candidate_reader=h.candidate,binding_reader=h.bindings,
            history_reader=RetainedHistoryReader(lambda:deepcopy(self.history),legacy_ports=()),
            allow_new_attempts=fresh,clock=lambda:NOW)
        self.instances.append(c);self.c=c
        h.ledger_io,h.journal,h.shared=c.auto_ledger,c.journal,c.shared
        h.instances=[r.adapter for r in self.instances]
        return c


def historical(tmp_path):
    spec=importlib.util.spec_from_file_location('r256_pinned_bundle',ROOT/'auth-session/compose.py')
    bundle=importlib.util.module_from_spec(spec);spec.loader.exec_module(bundle)
    destination=tmp_path/'historical'
    # Windows global autocrlf must not rewrite the pinned patch output. All
    # original pin/hash/patch checks remain ON; no repository config is changed.
    with pytest.MonkeyPatch.context() as m:
        n=int(os.environ.get('GIT_CONFIG_COUNT','0'))
        m.setenv('GIT_CONFIG_COUNT',str(n+1));m.setenv('GIT_CONFIG_KEY_'+str(n),'core.autocrlf')
        m.setenv('GIT_CONFIG_VALUE_'+str(n),'false')
        # A workspace-local temp tree must not inherit its parent's checkout;
        # otherwise git apply ignores paths outside that parent-relative prefix.
        m.setenv('GIT_CEILING_DIRECTORIES',str(tmp_path.resolve()))
        bundle.compose(destination,test_tree=True)
    name='_r256_historical_'+uuid4().hex
    p=ModuleType(name);p.__path__=[str(destination/'app')];sys.modules[name]=p
    return lambda module:importlib.import_module(name+'.'+module)


class DeniedNative:
    def __init__(self,native,sink):self._native,self._sink=native,sink
    def read(self):return self._native.read()
    def replace(self,*args,**kwargs):return self._sink.replace(*args,**kwargs)
