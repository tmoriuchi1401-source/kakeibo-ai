"""Setup-time account verification + fresh, backend-owned synthetic policy.

NOT a production grant, authority factory or writer capability. There is no
live permission verifier yet. Cell data cannot supply this policy or runtime.
The existing host verifies Google identity ONCE at setup; daily work has no
session, POST, CSRF or editor identity requirement. A revoked/changed policy
fails closed, including changes after the journal intent was claimed.
"""
from copy import deepcopy

from app.drive_run_state import StateError
from app.receipt_confirmation_contract import memory_only, token, load, encoded
from app.unified_receipt_review import digest
from app.unified_receipt_shadow import ExistingHostSessionGate

SCHEMA = 'synthetic-standing-permission-v1'
ENTRIES = {'receipt_manual', 'medical_manual', 'cash_manual', 'receipt_auto',
           'bank_auto', 'paypay_auto', 'amazon_auto'}
KINDS = dict(receipt_manual='normal',receipt_auto='normal',medical_manual='medical',
             cash_manual='cash',bank_auto='bank',paypay_auto='paypay',amazon_auto='amazon')
RUNTIME = {'owner', 'workbook', 'executor', 'destination', 'writer'}


def deny(code):
    raise StateError('standing_permission_'+code)


class SyntheticStandingPermission:
    def __init__(self, gate, policy_io, runtime, *, enabled=False, isolated_synthetic=False):
        if (enabled is not True or isolated_synthetic is not True
                or type(gate) is not ExistingHostSessionGate):
            deny('isolation_required')
        memory_only(policy_io)
        if any(hasattr(policy_io, k) for k in ('session', 'service', 'credentials', 'client', 'drive', 'sheets')):
            deny('external_transport_forbidden')
        # Existing signed Google login/owner binding, not a new auth subsystem.
        rt, _, record, actor = gate.current()
        if (rt.settings.get('mode') != 'synthetic_only'
                or getattr(rt.client, 'shadow_memory_only', False) is not True
                or actor.issuer != 'https://accounts.google.com'
                or actor.actor_id != digest([actor.issuer, actor.subject])
                or actor.actor_id != runtime.get('owner')
                or not record['created_at'] <= rt.clock() < record['expires_at']):
            deny('setup_owner_required')
        self.io, self.runtime, self.clock = policy_io, deepcopy(runtime), rt.clock
        self.setup_actor = actor.actor_id
        _, self.pin = self._read()  # Bind this setup to this exact policy revision.

    def _read(self):
        memory_only(self.io)
        if any(hasattr(self.io,k) for k in ('session','service','credentials','client','drive','sheets')):
            deny('external_transport_forbidden')
        raw, tag = self.io.read_versioned()
        token(raw, tag)
        v = load(raw)
        try:
            if (set(v) != {'schema','synthetic_only','revision','active','permissions_verified',
                          'runtime','routes','not_before','expires_at','acl_digest'}
                    or v['schema'] != SCHEMA or v['synthetic_only'] is not True
                    or type(v['revision']) is not int or v['revision'] < 1
                    or v['active'] is not True or v['permissions_verified'] is not True
                    or set(v['runtime']) != RUNTIME or v['runtime'] != self.runtime
                    or any(not isinstance(x,str) or not x for x in v['runtime'].values())
                    or v['runtime']['owner'] != self.setup_actor
                    or not isinstance(v['acl_digest'],str) or len(v['acl_digest']) != 64
                    or type(v['not_before']) is not int or type(v['expires_at']) is not int
                    or not v['not_before'] <= self.clock() < v['expires_at']
                    or type(v['routes']) is not dict or not v['routes']):
                deny('unverified_or_out_of_scope')
            for cid, route in v['routes'].items():
                if (not isinstance(cid,str) or not cid
                        or set(route) != {'entry','source_id','kind','max_amount'}
                        or route['entry'] not in ENTRIES or route['kind'] not in {'normal','medical','cash','bank','paypay','amazon'}
                        or not isinstance(route['source_id'],str) or not route['source_id']
                        or type(route['max_amount']) is not int or not 1 <= route['max_amount'] <= 99999999
                        or route['kind'] != KINDS[route['entry']]):
                    deny('route_invalid')
            if encoded(v) != raw or self.io.read_versioned() != (raw,tag):
                deny('changed')
            return v, digest([v,tag])
        except (KeyError,TypeError,ValueError):
            deny('invalid')

    def check(self, cid, *, source_id=None, kind=None, amount=None, automatic=None, before=None):
        """Pure eligibility observation; NEVER returns a financial authority.

        Automatic decisions must come from a trusted native adapter, not cells.
        The receipt Sheet worker only binds the two existing image adapters.
        Bank/PayPay/Amazon/cash have explicit scopes but remain unbound there.
        """
        v, observation = self._read()
        if observation != self.pin or before is not None and observation != before['policy_observation']:
            deny('changed_since_setup')
        route = v['routes'].get(cid)
        if not route:
            deny('source_not_allowed')
        if (source_id is not None and source_id != route['source_id']
                or kind is not None and kind != route['kind']
                or amount is not None and (type(amount) is not int or not 1 <= amount <= route['max_amount'])):
            deny('source_or_value_out_of_scope')
        if route['entry'].endswith('_auto') and automatic != 'eligible':
            deny('automatic_native_eligibility_required')
        return dict(policy_observation=observation, policy_revision=v['revision'],
                    setup_actor=self.setup_actor, route=deepcopy(route),
                    runtime=deepcopy(self.runtime), synthetic_only=True)
