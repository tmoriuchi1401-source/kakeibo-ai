"""Real existing synthetic Google login once; no daily auth or finance calls."""
from copy import deepcopy

from app.iphone_sheet_reader import RawReader
from app.receipt_confirmation_contract import encoded, load
from app.unified_receipt_review import digest
from app.unified_receipt_shadow import ExistingHostSessionGate
from staging_shadow.iphone_sheet_handoff import SheetDraftHandoff
from staging_shadow.sheets_only_surface import SyntheticSheetSurface
from staging_shadow.sheets_registration import SheetsRegistration
from staging_shadow.standing_permission import SyntheticStandingPermission, SCHEMA
from sheets_only_fixtures import Rig as OldRig
from test_legacy_contract_poc import mem
from unified_shadow_fixtures import ORIGIN, DOMAIN


class Rig(OldRig):
    def __init__(self, monkeypatch, tmp_path):
        super().__init__(monkeypatch, tmp_path)
        self.sheet = SyntheticSheetSurface(self.old.raw,self.old.contract,registration=True)
        self.old.sources = self.sheet.sources
        self.reader=RawReader(self.old.contract,self.sheet.core,self.sheet.observe_sources)
        self.adapter=SheetDraftHandoff(self.reader,self.old.journal,self.old.old.ui,enabled=True,isolated_synthetic=True)
        h=self.old.h
        assert h.login().status_code == 303
        self.runtime=dict(owner=self.adapter.owner,workbook=self.old.contract['workbook_id'],
                          executor='synthetic-worker-1',destination='synthetic-household-1',writer='existing-receipt-confirmation')
        routes={cid:dict(entry='medical_manual' if c.kind=='medical' else 'receipt_manual',
                       source_id=c.source_id,kind=c.kind,max_amount=100000) for cid,c in self.old.old.native.items()}
        self.policy_io=mem(dict(schema=SCHEMA,synthetic_only=True,revision=1,active=True,permissions_verified=True,
                    runtime=deepcopy(self.runtime),routes=routes,not_before=h.now-1,expires_at=h.now+86400,
                    acl_digest=digest(['synthetic-verified-permissions',self.runtime])))
        self.gate=ExistingHostSessionGate(h.configured,{'login':h.web.LOGIN_COOKIE,'ticket':h.web.COOKIE})
        self.permission=self.setup()
        def forbidden(*a,**k): raise AssertionError('per_transaction_auth_or_finance_forbidden')
        monkeypatch.setattr(self.gate,'current',forbidden)
        monkeypatch.setattr(self.gate,'post',forbidden)
        monkeypatch.setattr(self.adapter,'post',forbidden)
        monkeypatch.setattr(self.adapter,'prepare',forbidden)
        self.worker=self.rebuild()
        for c in self.old.old.native.values(): c.readers.draft.writes=0
        self.initial_auth=deepcopy(h.db.data)  # initial setup is read-only

    def setup(self, **kwargs):
        h=self.old.h
        cookies='; '.join(name+'='+h.client.get_cookie(name,domain=DOMAIN).value
                          for name in (h.web.LOGIN_COOKIE,h.web.COOKIE))
        with h.app.test_request_context('/',base_url=ORIGIN,headers={'Cookie':cookies}):
            return SyntheticStandingPermission(self.gate,self.policy_io,self.runtime,
                       enabled=True,isolated_synthetic=True,**kwargs)

    def rebuild(self):
        if not hasattr(self,'permission'):
            return OldRig.rebuild(self)
        return SheetsRegistration(self.adapter,self.sheet,self.permission,enabled=True,isolated_synthetic=True)

    def policy(self, change):
        v=load(self.policy_io.raw);change(v)
        self.policy_io.raw=encoded(v);self.policy_io.revision+=1

    def register(self,cid='SYN-G101',generation=1):
        self.request(cid,'登録 '+str(generation))
        first=self.worker.poll()
        if first and first[-1]['status']=='SYNC_WAIT':
            self.old.h.now+=3
            return self.worker.poll()[-1]
        return first[-1]
