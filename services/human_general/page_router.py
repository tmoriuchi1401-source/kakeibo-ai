"""Operator-pinned per-page capabilities. Browser input cannot choose a page.

Routing records expire with the authentication request. Each page retains its
own private conditional authority file. No writer, image or Actions capability.
"""
from copy import deepcopy
from urllib.parse import parse_qs,urlsplit
from app.drive_run_state import StateError
from app.human_general_auth_transport import UUID,TTL
from app.page_receipt_model import digest
from .backend import timestamp
from .runtime import SyntheticRuntime
from .real_page import validate_config
from .real_runtime import RealPageRuntime

SCHEMA='human-general-page-profiles-v1'


def validate_profiles(value):
    if set(value) not in ({'schema','profiles'},{'schema','profiles','reconciliation'}) or value['schema']!=SCHEMA:
        raise StateError('real_page_profiles_invalid')
    profiles=value['profiles']
    if not isinstance(profiles,dict) or not profiles or set(profiles)-{'p4','p10','p14'}:
        raise StateError('real_page_profiles_invalid')
    files=set();source=None
    for name,config in profiles.items():
        validate_config(config)
        if name!='p'+str(config['page']['page_number']) or config['authority_file'] in files:
            raise StateError('real_page_profile_collision')
        files.add(config['authority_file'])
        identity=config['page']['source']
        if source is not None and source!=identity:raise StateError('real_page_profile_source_changed')
        source=identity
    if 'reconciliation' in value:
        from .reconciliation_readers import validate_reconciliation_config
        config=validate_reconciliation_config(value['reconciliation'])
        if config['drive']!=profiles.get('p14'):raise StateError('reconciliation_profile_source_changed')
    return deepcopy(value)


class PageRouter(SyntheticRuntime):
    def __init__(self,client,settings,key,profiles,info,*,runtime_factory=RealPageRuntime,**kwargs):
        super().__init__(client,settings,key,**kwargs)
        verified=validate_profiles(profiles)
        self.profiles=verified['profiles']
        if 'reconciliation' in verified:self.profiles={**self.profiles,'p1':verified['reconciliation']}
        self.info=info;self.runtime_factory=runtime_factory
        self.mode='independent_real_pages_authority_only'
        self.selected=None;self.selected_rid=None

    def _select(self,rid):
        if not isinstance(rid,str) or not UUID.fullmatch(rid):
            raise StateError('human_general_auth_request_invalid')
        if self.selected is not None:
            if rid!=self.selected_rid:raise StateError('real_page_request_cross_binding')
            return self.selected
        snap=self.client.collection('real_request_profiles').document(rid).get(retry=None,timeout=10)
        record=snap.to_dict() if snap.exists else None
        if not record or set(record)!={'profile','profile_digest','expires_at'}:
            raise StateError('human_general_auth_replay_or_unknown_request')
        if record['expires_at'].timestamp()<=self.clock():
            raise StateError('human_general_auth_request_expired')
        config=self.profiles.get(record['profile'])
        if config is None or digest(config)!=record['profile_digest']:
            raise StateError('real_page_profile_stale')
        factory=self.runtime_factory
        if record['profile']=='p1':
            from .reconciliation_runtime import LiveReconciliationRuntime
            factory=LiveReconciliationRuntime
        runtime=factory(self.client,self.settings,self.key,config,self.info,
            clock=self.clock,identity=self.identity,exchange=self.exchange)
        runtime.drive.begin_request()
        self.selected,self.selected_rid=runtime,rid
        return runtime

    @property
    def drive(self):
        # HTTP teardown always discards the selected request's fresh cache.
        return self.selected.drive if self.selected is not None else None

    def gateway(self,rid):return self._select(rid).gateway(rid)
    def state(self,rid,kind):return self._select(rid).state(rid,kind)
    def factory(self,rid,expected_tag=None):return self._select(rid).factory(rid,expected_tag)
    def label(self,rid):return self._select(rid).label(rid)
    def start_heading(self,rid):
        runtime=self._select(rid)
        return runtime.start_heading(rid) if hasattr(runtime,'start_heading') else '一般レシートの確認'
    def success_label(self,rid):return self._select(rid).success_label(rid)
    def confirmation_text(self,rid):
        runtime=self._select(rid)
        if hasattr(runtime,'confirmation_text'):return runtime.confirmation_text(rid)
        return {'heading':'一般レシートとして確定',
            'detail':'一般レシートであることを確認し、このページだけをGeminiへ送信して解析することを許可します。',
            'button':'一般レシートとして確定しGemini送信を許可'}
    def validate_result(self,rid,record):
        runtime=self._select(rid)
        if hasattr(runtime,'validate_result'):return runtime.validate_result(rid,record)
        page=runtime.gateway(rid).fresh(record)
        grant=runtime.factory(rid)(None).current(page)
        if grant['confirmation_digest']!=record['authority_digest']:raise StateError('real_page_readback_mismatch')
    def source(self,sid):
        if self.selected is None:raise StateError('real_page_profile_not_selected')
        return self.selected.source(sid)
    def reconcile(self,rid):return self._select(rid).reconcile(rid)
    @property
    def _stages(self):
        return getattr(self.selected,'_stages',lambda *_args,**_kw:None)

    def seed_page(self,name):
        # Administrative command only; never an HTTP route or Sheet event.
        config=self.profiles.get(name)
        if config is None or name=='p1':raise StateError('real_page_target_forbidden')
        runtime=self.runtime_factory(self.client,self.settings,self.key,config,self.info,
            clock=self.clock,identity=self.identity,exchange=self.exchange)
        link=runtime.seed();rid=parse_qs(urlsplit(link).query)['request'][0]
        self.client.collection('real_request_profiles').document(rid).create(
            {'profile':name,'profile_digest':digest(config),'expires_at':timestamp(int(self.clock())+TTL)},
            retry=None,timeout=10)
        return link

    def seed_reconciliation(self):
        from .reconciliation_runtime import LiveReconciliationRuntime
        config=self.profiles.get('p1')
        if config is None:raise StateError('reconciliation_profile_unavailable')
        runtime=LiveReconciliationRuntime(self.client,self.settings,self.key,config,self.info,
            clock=self.clock,identity=self.identity,exchange=self.exchange)
        runtime.drive.begin_request()
        try:
            decision=runtime.readers.decision()
            if decision not in {'same','different','unknown'}:raise StateError('reconciliation_selection_required')
            link=runtime.seed_decision(config['comparison_ledger_id'],config['ledger_snapshot_digest'],decision)
        finally:runtime.drive.end_request()
        rid=parse_qs(urlsplit(link).query)['request'][0]
        self.client.collection('real_request_profiles').document(rid).create(
            {'profile':'p1','profile_digest':digest(config),'expires_at':timestamp(int(self.clock())+TTL)},
            retry=None,timeout=10)
        return link
