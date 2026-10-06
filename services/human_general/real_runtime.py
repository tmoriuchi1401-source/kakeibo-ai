"""Single p14 auth capability; ordinary Actions processing stays separate."""
from uuid import uuid4
from itsdangerous import URLSafeTimedSerializer
from app.drive_run_state import StateError
from app.human_general_auth_transport import AuthRequestStore,AuthenticatedGeneralConfirmation,empty_auth_state,TTL,AI_CONSENT_ACTION
from app.human_general_authority import HumanGeneralConfirmation
from .backend import canonical,timestamp,FirestoreConditionalState
from .runtime import SyntheticRuntime,ExpectedTagAuthorityStore
from .real_page import RealPageDrive,VerifiedDriveTransport


class RealPageRuntime(SyntheticRuntime):
    def __init__(self,client,settings,key,config,info,**kwargs):
        super().__init__(client,settings,key,**kwargs)
        self.drive=RealPageDrive(config,info)
        self.config=config
        self.mode='real_p14_authority_only'

    def state(self,rid,kind):
        from app.human_general_auth_transport import UUID
        if not isinstance(rid,str) or not UUID.fullmatch(rid):raise StateError('human_general_auth_request_invalid')
        if kind=='requests':return FirestoreConditionalState(self.client.collection('real_requests').document(rid),clock=self.clock)
        if kind=='authorities':return VerifiedDriveTransport(self.drive,self.identity.owner_subject)
        raise StateError('real_page_state_forbidden')

    def current_page(self,rid,source_id,number):
        if source_id!=self.drive.page.source.source_file_id or number!=14:raise StateError('real_page_target_forbidden')
        return self.drive.fresh()

    def source(self,source_id):return self.drive.source(source_id)

    def gateway(self,rid):
        return AuthenticatedGeneralConfirmation(
            AuthRequestStore(self.state(rid,'requests'),self.config['binding'],preflight=lambda:None),self.identity,
            lambda sid,n:self.current_page(rid,sid,n),self.source,
            redirect_uri=self.origin+'/oauth/callback',exchange_code=self.exchange,clock=self.clock,
            requested_action=AI_CONSENT_ACTION)

    def factory(self,rid,expected_tag=None):
        def build(actor):
            verified=None
            if actor is not None:
                verified=self.gateway(rid).verified_actor(rid)
                if actor(rid)!=verified.legacy_actor():raise StateError('real_page_actor_replaced')
            transport=VerifiedDriveTransport(self.drive,self.identity.owner_subject,actor=verified)
            return HumanGeneralConfirmation(
                ExpectedTagAuthorityStore(transport,self.config['binding'],preflight=lambda:None,expected_tag=expected_tag),
                lambda sid,n:self.current_page(rid,sid,n),actor,load_source=self.source)
        return build

    def label(self,rid):
        self.gateway(rid).record(rid,'prepared')
        return 'p14（今回の対象ページのみ）'

    def seed(self):
        # Operator only; fail closed if this page already has any HGA.
        current=self.drive.fresh();store=ExpectedTagAuthorityStore(self.state(str(uuid4()),'authorities'),self.config['binding'],preflight=lambda:None)
        if store.load()['grants']:raise StateError('real_page_authority_already_exists')
        rid,now=str(uuid4()),int(self.clock())
        self.client.collection('real_requests').document(rid).create(
            {'payload':canonical(empty_auth_state(self.config['binding'])),'expires_at':timestamp(now+TTL)},retry=None,timeout=10)
        self.gateway(rid).prepare(current,request_id=rid)
        proof=URLSafeTimedSerializer(self.key,salt='hga-link-v1').dumps({'request':rid})
        return self.origin+'/start?request='+rid+'&proof='+proof
