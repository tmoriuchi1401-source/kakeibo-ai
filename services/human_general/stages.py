"""Bounded, allowlisted status logging. No raw exception/request/header values."""
from hashlib import sha256
import json,time,os

STAGES={'confirm_received','session_verified','request_freshness_verified','request_claimed','actor_verified',
    'source_freshness_started','source_freshness_verified','authority_state_read_started','authority_state_read_complete',
    'conditional_write_started','conditional_write_complete','exact_readback_complete','request_complete',
    'response_sent','confirm_failed','replay_rejected'}

class Stages:
    def __init__(self,request_id):
        self.request_hash=sha256(request_id.encode()).hexdigest()[:12]
        self.started=time.monotonic();self.count=0
    def __call__(self,stage,*,outcome=None,exception=None,http_status=None):
        if stage not in STAGES or self.count>=64:return
        if os.environ.get('HGA_STAGE_DIAGNOSTICS')!='1' and stage not in {
                'request_claimed','request_complete','confirm_failed','response_sent','replay_rejected'}:return
        if outcome not in {None,'not_written','written','unknown'}:return
        if http_status is not None and (type(http_status) is not int or not 100<=http_status<=599):return
        self.count+=1
        item={'stage':stage,'request_hash':self.request_hash,'elapsed_seconds':round(time.monotonic()-self.started,3)}
        if outcome is not None:item['outcome']=outcome
        if exception is not None:item['exception_class']=type(exception).__name__
        if http_status is not None:item['http_status']=http_status
        print(json.dumps(item),flush=True)
