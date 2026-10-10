"""Administrator-seeded synthetic operations on the EXISTING auth endpoint.

No Drive/Sheets, source images, API parser, financial writer or dispatch. All
operation state has ten-minute TTL. Reuses the real HGA/item gateway and screen.
"""
from dataclasses import asdict
import json
from pathlib import Path
from uuid import uuid4

from itsdangerous import URLSafeTimedSerializer
from app.drive_run_state import StateError
from app.human_general_auth_transport import AuthRequestStore, empty_auth_state, TTL
from app.receipt_item_auth_transport import AuthenticatedItemReview, validate_item_auth
from app.receipt_item_confirmation import binding
from app.receipt_item_review import evaluate
from app.page_receipt_model import digest
from .runtime import SyntheticRuntime, BINDING, synthetic_page
from .backend import FirestoreConditionalState, canonical, timestamp

FIXTURE=json.loads(Path(__file__).with_name('session_fixture.json').read_text('utf8'))
PROFILE_DIGEST=digest(['shared-login-synthetic-only-v1',FIXTURE])
CATEGORIES=tuple(tuple(pair) for pair in FIXTURE['categories'])


class SessionCanaryRuntime(SyntheticRuntime):
    mode='shared_login_synthetic_only'

    def _stages(self,*_args,**_kwargs):
        pass  # No synthetic claims/cookies/request values are logged.

    def operation(self,rid):
        snap=self.client.collection('session_canary_routes').document(rid).get(retry=None,timeout=10)
        value=snap.to_dict() if snap.exists else None
        if (not value or set(value)!={'operation','fixture_digest','expires_at'}
                or value['operation'] not in {'hga','items'} or value['fixture_digest']!=PROFILE_DIGEST
                or value['expires_at'].timestamp()<=self.clock()):
            raise StateError('human_general_auth_request_expired')
        return value['operation']

    def state(self,rid,kind):
        if kind not in {'requests','authorities'}:raise StateError('session_canary_state_forbidden')
        return FirestoreConditionalState(self.client.collection('session_canary_'+kind).document(rid),clock=self.clock)

    def current_page(self,rid,source_id,number):
        page=synthetic_page()
        if source_id!=page.source.source_file_id or number!=1:
            raise StateError('synthetic_source_only')
        self.operation(rid)
        return page

    def gateway(self,rid):
        if self.operation(rid)=='hga':return super().gateway(rid)
        return self.with_login(AuthenticatedItemReview(
            AuthRequestStore(self.state(rid,'requests'),BINDING,preflight=lambda:None,validator=validate_item_auth),
            self.identity,readers=lambda:(FIXTURE['candidate'],FIXTURE['snapshot'],CATEGORIES),
            redirect_uri=self.origin+'/oauth/callback',exchange_code=self.exchange,clock=self.clock))

    def factory(self,rid,expected_tag=None):
        if self.operation(rid)=='hga':return super().factory(rid,expected_tag)
        runtime=self
        class Save:
            def confirm(self,candidate,snapshot,current,proof):
                if candidate!=FIXTURE['candidate'] or snapshot!=FIXTURE['snapshot']:
                    raise StateError('item_confirmation_snapshot_stale')
                result=evaluate(candidate,current,CATEGORIES,confirmation=proof)
                if result['status']!='ready_to_confirm':raise StateError('item_confirmation_not_ready')
                state=runtime.state(rid,'authorities');before,tag=state.read_versioned()
                if tag!=expected_tag or expected_tag=='*':raise StateError('HTTP_412')
                value=json.loads(before)
                if value['requests']:raise StateError('human_general_auth_replay_or_unknown_request')
                value['requests'][rid]={'proof':asdict(proof),'snapshot_digest':digest(snapshot),
                                        'binding':binding(candidate,snapshot,rid)}
                value['audit']=[{'request_id':rid,'actor_id':proof.actor_id,'authority_digest':proof.authority_digest}]
                after=canonical(value);state.replace_versioned(before,tag,after)
                if state.read_versioned()[0]!=after:raise StateError('synthetic_readback_mismatch')
                return {'authority_digest':proof.authority_digest}
        return lambda _:Save()

    def label(self,rid):return 'synthetic '+('一般レシートの送信許可' if self.operation(rid)=='hga' else '商品明細の記帳要求（会計writeなし）')
    def success_label(self,rid):return 'synthetic確認を保存しました（会計writeは未実行）'
    def confirmation_text(self,rid):
        if self.operation(rid)=='hga':
            return {'heading':'一般レシートとして確定','detail':'syntheticページの送信許可だけを保存します。AI送信・会計writeは行いません。',
                    'button':'一般レシートとして確定しGemini送信を許可'}
        return {'heading':'商品明細を確認して記帳要求を保存','detail':'syntheticの商品明細の確認だけを保存します。会計writeは行いません。',
                'button':'原本と明細を確認し記帳要求を保存'}

    def validate_result(self,rid,record):
        if self.operation(rid)=='hga':
            page=self.current_page(rid,synthetic_page().source.source_file_id,1)
            grant=self.factory(rid)(None).current(page)
            if grant['confirmation_digest']!=record['authority_digest']:raise StateError('synthetic_readback_mismatch')
        else:
            value=json.loads(self.state(rid,'authorities').read_versioned()[0])
            if (set(value['requests'])!={rid} or len(value['audit'])!=1
                    or value['requests'][rid]['proof']['authority_digest']!=record['authority_digest']):
                raise StateError('synthetic_readback_mismatch')

    def reconcile(self,rid):
        # Observation only; never retry or adopt a timed-out confirmation.
        try:
            value=json.loads(self.state(rid,'authorities').read_versioned()[0])
            if self.operation(rid)=='hga':
                if len(value['grants'])==len(value['audit'])==1 and value['audit'][0]['request_id']==rid:
                    return 'written'
                if not value['grants'] and not value['audit']:return 'not_written'
            else:
                if set(value['requests'])=={rid} and len(value['audit'])==1:return 'written'
                if not value['requests'] and not value['audit']:return 'not_written'
        except Exception:pass
        return 'unknown'

    def seed_operation(self,operation):
        if self.settings.get('session_canary_enabled') is not True or operation not in {'hga','items'}:
            raise StateError('session_canary_disabled')
        rid=str(uuid4());expiry=timestamp(int(self.clock())+TTL)
        self.client.collection('session_canary_routes').document(rid).create(
            {'operation':operation,'fixture_digest':PROFILE_DIGEST,'expires_at':expiry},retry=None,timeout=10)
        self.client.collection('session_canary_requests').document(rid).create(
            {'payload':canonical(empty_auth_state(BINDING)),'expires_at':expiry},retry=None,timeout=10)
        if operation=='hga':
            from app.human_general_authority import empty_state
            initial=empty_state(BINDING)
        else:initial={'requests':{},'audit':[]}
        self.client.collection('session_canary_authorities').document(rid).create(
            {'payload':canonical(initial),'expires_at':expiry},retry=None,timeout=10)
        gateway=self.gateway(rid)
        expected=synthetic_page() if operation=='hga' else binding(FIXTURE['candidate'],FIXTURE['snapshot'],rid)
        gateway.prepare(expected,request_id=rid)
        proof=URLSafeTimedSerializer(self.key,salt='hga-link-v1').dumps({'request':rid})
        return self.origin+'/start?request='+rid+'&proof='+proof
