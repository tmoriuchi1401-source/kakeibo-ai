"""Synthetic owner acknowledgements. Never a financial request or authority.

No clients, writer/execute/submit ports or production registration. Native
adapters are used for validation only on explicit POST, never on GET.
"""
from copy import deepcopy
from dataclasses import asdict, dataclass, field
import json
import re
from uuid import UUID, uuid5, NAMESPACE_URL

from cryptography.fernet import Fernet, InvalidToken

from .drive_run_state import StateError
from .iphone_item_drafts import issue, inspect
from .iphone_native_dry_run import medical_fields_only
from .iphone_persistent_drafts import PersistentJournal, unique
from .iphone_review_drafts import snapshot
from .iphone_review_preview import preview_medical, preview_structure, check_native_case
from .iphone_source_observation import SourceObservation, revalidate, observe
from .receipt_item_review import card, check_snapshot, evaluate, snapshot as item_snapshot
from .unified_receipt_review import digest
from .unified_receipt_eligibility import current_snapshot

SCHEMA = 'synthetic-owner-acknowledgements-v1'
ACTIONS = {'save_draft', 'acknowledge_snapshot'}
FIELD_ORDER = ('date', 'merchant', 'amount', 'category', 'payment', 'memo')
FIELDS = set(FIELD_ORDER)
RESULT_KEYS = {'schema','synthetic_only','request_id','case_id','purpose','action','input_digest',
               'reviewed_revision','stored_revision','native_authority','accounting_permission','reviewed_input','source_snapshot','status'}


def deny(reason):
    raise StateError('iphone_confirmation_' + reason)


@dataclass
class NativeDraftContext:
    canonical: dict
    case: object
    source_reader: object
    record: dict | None = None
    saved_items: dict | None = None
    categories: list | None = None
    medical_port: object = None
    structure_observation: object = None
    structure_request_id: str = ''
    structure_proposal: dict | None = None
    structure_validator: object = None
    structure_reader: object = None
    native_digest: str = field(init=False)

    def __post_init__(self):
        self.native_digest = digest(current_snapshot(self.case))

    def initial(self):
        return dict(fields={k:deepcopy(self.canonical['inputs'][k]) for k in FIELD_ORDER},
                    item_draft=issue(self.record, self.saved_items, self.categories) if self.record else None,
                    structure_snapshot=dict(proposal=deepcopy(self.structure_proposal), evidence=deepcopy(self.structure_observation.evidence))
                        if self.structure_observation else None)

    def validate(self, value):
        if (not isinstance(value, dict) or set(value) != {'fields', 'item_draft', 'structure_snapshot'}
                or not isinstance(value['fields'], dict) or set(value['fields']) != FIELDS
                or any(type(v) not in (str, int) or isinstance(v, str) and (len(v)>4000 or v.startswith('='))
                       for v in value['fields'].values())):
            deny('input_invalid')
        row = deepcopy(self.canonical); row['inputs'].update(value['fields'])
        check_native_case(row, self.case, self.native_digest)
        if row['kind'] == 'normal':
            if value['fields']['category'] != '' or value['structure_snapshot'] is not None:
                deny('receipt_category_broadcast_forbidden')
            checked = inspect(value['item_draft'], self.record, self.saved_items, self.categories)
            current = check_snapshot(checked['proposed_snapshot'], card(self.record, categories=self.categories))
            for f in ('date', 'merchant', 'amount', 'payment', 'memo'):
                current[f] = value['fields'][f]
            current['action'] = '未選択'
            proposed = item_snapshot(card(self.record, current, categories=self.categories), current)
            checked_native = evaluate(self.record, check_snapshot(proposed, card(self.record, categories=self.categories)), self.categories)
            ready = checked['status'] == 'ready_for_owner_redisplay' and checked_native['status'] == 'ready_to_confirm'
        elif row['kind'] == 'medical':
            if value['item_draft'] is not None or value['structure_snapshot'] is not None:
                deny('medical_manual_only')
            fake_draft = dict(kind='医療', cells=[{} for _ in range(17)])
            for f,col in dict(date=4,merchant=5,amount=6,category=7,payment=8,memo=12).items():
                v = value['fields'][f]
                fake_draft['cells'][col] = {'userEnteredValue':{'numberValue' if type(v) is int else 'stringValue':v}}
            ready = medical_fields_only(fake_draft, self.categories)['status'] == 'manual_fields_validated_only'
            if ready:
                result = preview_medical(row, self.canonical, snapshot(row), self.case,
                    self.native_digest, self.medical_port)
                ready = result['validation']['status'] == 'validated_not_authorized'
        elif row['kind'] == 'grouping':
            if value['item_draft'] is not None or any(value['fields'][k] != '' for k in FIELDS - {'memo'}):
                deny('structure_purpose_required')
            if not callable(self.structure_reader) or not callable(self.structure_validator):
                deny('structure_observation_UNKNOWN')
            try:
                proposal = self.structure_reader()
            except (KeyError,TypeError,ValueError,OSError):
                deny('structure_observation_UNKNOWN')
            if not isinstance(proposal,dict) or not {'proposal_digest','source_file_id','source_content_hash','page_count','pages'} <= set(proposal):
                deny('structure_observation_UNKNOWN')
            structural = value['structure_snapshot']
            if (structural != dict(proposal=self.structure_proposal,evidence=self.structure_observation.evidence)
                    or proposal != self.structure_proposal
                    or proposal['proposal_digest'] != self.structure_observation.evidence['proposal_digest']
                    or not self.structure_validator(proposal,{k:proposal[k] for k in
                        ('source_file_id','source_content_hash','page_count','pages')})):
                deny('structure_snapshot_changed')
            preview_structure(row, self.canonical, snapshot(row), self.structure_observation, self.structure_request_id)
            ready = True
        else:
            deny('native_kind_invalid')
        return 'ready_for_owner_redisplay' if ready else 'partial_or_held'


class ConfirmationService:
    """Exact, encrypted CAS acknowledgement ledger with a stateless view proof.

    The proof binds an existing session and the server's actual canonical view.
    It carries no native token/permission. No review state is written on GET.
    """
    def __init__(self, saved_repository, contexts, key, *, clock):
        if type(saved_repository.journal) is not PersistentJournal:
            deny('persistent_repository_required')
        self.saved = saved_repository; self.journal = saved_repository.journal
        self.contexts, self.cipher, self.clock = contexts, Fernet(key), clock
        self.owner = saved_repository.owner
        for cid,context in contexts.items():
            if context.case.synthetic is not True or context.native_digest != self.saved_mapping(cid)['native_digest']:
                deny('canonical_context_changed')

    def read(self):
        cards = {c['case_id']:c for c in self.saved.snapshots()}
        state, _, tag = self.journal._state()
        value = deepcopy(state.get('confirmation', dict(schema=SCHEMA, drafts={}, requests={}, history=[])))
        try:
            if (set(value) != {'schema', 'drafts', 'requests', 'history'} or value['schema'] != SCHEMA
                    or any(not isinstance(value[k], dict) for k in ('drafts', 'requests'))
                    or not isinstance(value['history'], list) or not set(value['drafts']) <= set(cards)):
                deny('journal_quarantined')
            for cid,draft in value['drafts'].items():
                if (set(draft) != {'value', 'input_digest', 'validation'} or digest(draft['value']) != draft['input_digest']
                        or set(draft['value']) != {'fields','item_draft','structure_snapshot'}
                        or not isinstance(draft['value']['fields'],dict) or set(draft['value']['fields']) != FIELDS
                        or draft['validation'] not in {'ready_for_owner_redisplay', 'partial_or_held'}):
                    deny('journal_quarantined')
            for rid,entry in value['requests'].items():
                result = entry['result']
                if (str(UUID(rid)) != rid or set(entry) != {'fingerprint', 'result'} or set(result) != RESULT_KEYS
                        or not isinstance(entry['fingerprint'],str) or not re.fullmatch('[a-f0-9]{64}',entry['fingerprint'])
                        or result['case_id'] not in cards or result['purpose'] != cards[result['case_id']]['purpose']
                        or result['action'] not in ACTIONS or result['status'] !=
                           ('draft_saved' if result['action']=='save_draft' else 'synthetic_owner_acknowledged')
                        or type(result['reviewed_revision']) is not int or result['stored_revision'] != result['reviewed_revision']+1
                        or set(result['reviewed_input']) != {'fields','items','structure'} or set(result['reviewed_input']['fields']) != FIELDS
                        or entry['result']['schema'] != SCHEMA or entry['result']['synthetic_only'] is not True
                        or entry['result']['request_id'] != rid or entry['result']['native_authority'] is not False
                        or entry['result']['accounting_permission'] is not False):
                    deny('journal_quarantined')
        except (KeyError, TypeError, ValueError):
            deny('journal_quarantined')
        # The source bundle and journal are not a cross-resource transaction.
        if self.journal.read_versioned()[1] != tag:
            deny('concurrent_snapshot')
        return cards, state, value, tag

    def draft(self, cid, ledger):
        if cid not in self.contexts:
            deny('held_or_unmapped')
        context = self.contexts[cid]
        initial = context.initial()  # pure snapshot validation, no native observation
        stored = ledger['drafts'].get(cid)
        return deepcopy(stored if stored else dict(value=initial, input_digest=digest(initial), validation='partial_or_held'))

    def proof(self, cid, action, auth, *, read=None):
        cards, state, ledger, tag = read or self.read()
        if cid not in cards or cards[cid]['display']['held'] or cid not in self.contexts:
            deny('held_or_unmapped')
        _, ticket, record, actor = auth
        if (actor.issuer != 'https://accounts.google.com' or actor.actor_id != self.owner
                or digest([actor.issuer, actor.subject]) != self.owner):
            deny('owner_required')
        draft = self.draft(cid, ledger)
        claims = dict(schema=SCHEMA, case_id=cid, purpose=cards[cid]['purpose'], action=action,
            actor=self.owner, session=digest([record['request_id'], ticket.cookie]),
            created_at=record['created_at'], expires_at=record['expires_at'],
            expected_revision=state['legacy']['revision'], expected_tag=tag,
            input_digest=draft['input_digest'], baseline_digest=digest(cards[cid]),
            source_observation_digest=self.saved_mapping(cid)['source_observation']['snapshot_digest'],
            generation=self.journal.scope['generation'])
        claims['request_id'] = str(uuid5(NAMESPACE_URL, digest(claims)))
        return self.cipher.encrypt(json.dumps(claims, sort_keys=True).encode()).decode(), claims

    def open(self, token):
        try:
            if not isinstance(token, str) or len(token) > 12000:
                deny('proof_invalid')
            claims = json.loads(self.cipher.decrypt(token.encode()), object_pairs_hook=unique)
            keys = {'schema','case_id','purpose','action','actor','session','created_at','expires_at','expected_revision',
                    'expected_tag','input_digest','baseline_digest','source_observation_digest','generation','request_id'}
            if (not isinstance(claims, dict) or set(claims) != keys or claims['schema'] != SCHEMA or claims['action'] not in ACTIONS
                    or str(UUID(claims['request_id'])) != claims['request_id']
                    or type(claims['expected_revision']) is not int or claims['expected_revision'] < 0
                    or type(claims['generation']) is not int or claims['generation'] < 1
                    or any(type(claims[k]) is not int for k in ('created_at','expires_at'))
                    or any(not isinstance(claims[k],str) or not re.fullmatch('[a-f0-9]{64}',claims[k]) for k in
                           ('actor','session','input_digest','baseline_digest','source_observation_digest'))):
                deny('proof_invalid')
            return claims
        except (InvalidToken, ValueError, TypeError, KeyError, AttributeError):
            deny('proof_invalid')

    def edit(self, before, edits):
        if not isinstance(edits, dict) or set(edits) != {'fields', 'items'}:
            deny('input_invalid')
        value = deepcopy(before['value']); value['fields'] = deepcopy(edits['fields'])
        if value['item_draft'] is None:
            if edits['items'] != []:
                deny('item_identity_changed')
        else:
            entries = value['item_draft']['entries']
            if not isinstance(edits['items'], list) or len(entries) != len(edits['items']):
                deny('item_identity_changed')
            for e,incoming in zip(entries, edits['items']):
                if (not isinstance(incoming, dict) or set(incoming) != {'item_id', 'name', 'amount', 'category', 'state', 'note'}
                        or incoming['item_id'] != e['item_id']):
                    deny('item_identity_changed')
                for k in ('name', 'amount', 'category'): e['proposed'][k] = deepcopy(incoming[k])
                for k in ('state', 'note'): e[k] = deepcopy(incoming[k])
        return value

    @staticmethod
    def public_input(value):
        return dict(fields=deepcopy(value['fields']), items=[dict(item_id=e['item_id'], old=deepcopy(e['old']),
            proposed=deepcopy(e['proposed']), state=e['state'], note=e['note'])
            for e in value['item_draft']['entries']] if value['item_draft'] else [],
            structure=deepcopy(value['structure_snapshot']))

    def post(self, payload, auth, gate):
        if not isinstance(payload, dict):
            deny('request_invalid')
        claims = self.open(payload.get('proof'))
        action = claims['action']; expected = {'proof', 'case_id', 'purpose', 'action', 'request_id', 'expected_revision', 'input_digest'}
        expected.add('edits' if action == 'save_draft' else 'acknowledged')
        if (set(payload) != expected or type(payload['expected_revision']) is not int
                or any(payload[k] != claims[k] for k in expected - {'proof', 'edits', 'acknowledged'})):
            deny('request_binding_changed')
        _, ticket, record, actor = auth
        attested = gate.post({}, claims['purpose'], claims['request_id'], claims['input_digest'], claims['expected_revision'])
        if (actor.issuer != 'https://accounts.google.com' or actor.actor_id != self.owner
                or digest([actor.issuer, actor.subject]) != self.owner or attested.actor_id != self.owner
                or claims['actor'] != self.owner or claims['session'] != digest([record['request_id'], ticket.cookie])
                or claims['expires_at'] != record['expires_at'] or claims['created_at'] != record['created_at']
                or claims['expires_at'] - claims['created_at'] != 600
                or not claims['created_at'] <= self.clock() < claims['expires_at']):
            deny('session_or_request_expired')
        cards, state, ledger, tag = self.read(); cid = claims['case_id']
        if (cid not in cards or cards[cid]['display']['held'] or cid not in self.contexts
                or cards[cid]['purpose'] != claims['purpose'] or digest(cards[cid]) != claims['baseline_digest']
                or self.saved_mapping(cid)['source_observation']['snapshot_digest'] != claims['source_observation_digest']
                or claims['generation'] != self.journal.scope['generation']):
            deny('case_or_purpose_changed')
        fingerprint = digest(dict(claims=claims, content=payload.get('edits') if action == 'save_draft' else payload.get('acknowledged')))
        old = ledger['requests'].get(claims['request_id'])
        if old:
            if old['fingerprint'] != fingerprint:
                deny('request_id_content_conflict')
            return deepcopy(old['result']), True
        draft = self.draft(cid, ledger)
        if (state['legacy']['revision'] != claims['expected_revision'] or tag != claims['expected_tag']
                or draft['input_digest'] != claims['input_digest']):
            deny('stale_snapshot')
        if action == 'save_draft':
            proposed = self.edit(draft, payload['edits'])
            status = self.contexts[cid].validate(proposed)
        else:
            if payload.get('acknowledged') is not True:
                deny('explicit_owner_confirmation_required')
            proposed = draft['value']; status = self.contexts[cid].validate(proposed)
            if status != 'ready_for_owner_redisplay':
                deny('native_validation_held')
            # A new request/session may observe the SAME already acknowledged
            # synthetic snapshot. It must not create a second acknowledgement.
            duplicate_result = None
            for entry in ledger['requests'].values():
                prior = entry['result']
                if (prior['action'] == action and prior['case_id'] == cid
                        and prior['input_digest'] == draft['input_digest']):
                    duplicate_result = deepcopy(prior)
        # Revalidate the trusted co-observation. No browser source/URL/version
        # or digest is used as independent freshness evidence.
        mapping = self.saved_mapping(cid)
        fresh = revalidate(SourceObservation(**mapping['source_observation']), self.contexts[cid].source_reader,
            self.journal_source(cid), self.contexts[cid].canonical['identity'], purpose=cards[cid]['purpose'],
            input_snapshot=mapping['input_digest'], stage='prepare' if action == 'save_draft' else 'acceptance', clock=self.clock)
        # Co-observe again with the exact HOST-edited input, rather than reuse
        # the baseline Sheet digest or an old observation timestamp as current.
        current = observe(self.contexts[cid].source_reader, self.journal_source(cid), self.contexts[cid].canonical['identity'],
            purpose=cards[cid]['purpose'], input_snapshot=digest(proposed), clock=self.clock)
        identity=lambda o:{k:v for k,v in asdict(o).items() if k not in {'observed_at','snapshot_digest','input_snapshot'}}
        if identity(fresh) != identity(current):
            deny('source_UNKNOWN')
        if action == 'acknowledge_snapshot' and duplicate_result is not None:
            return duplicate_result, True
        after = deepcopy(ledger)
        if action == 'save_draft':
            after['history'].append(dict(case_id=cid, draft=deepcopy(draft)))
            after['drafts'][cid] = dict(value=proposed, input_digest=digest(proposed), validation=status)
        result = dict(schema=SCHEMA, synthetic_only=True, request_id=claims['request_id'],
            case_id=cid, purpose=claims['purpose'], action=action, input_digest=digest(proposed),
            reviewed_revision=claims['expected_revision'], stored_revision=claims['expected_revision']+1,
            native_authority=False, accounting_permission=False,
            reviewed_input=self.public_input(proposed),
            source_snapshot=asdict(current),
            status='draft_saved' if action == 'save_draft' else 'synthetic_owner_acknowledged')
        after['requests'][claims['request_id']] = dict(fingerprint=fingerprint, result=result)
        from .iphone_sheet_reader import DraftBatch
        self.journal.save(DraftBatch(**state['legacy']['batch']), tag, claims['request_id'], confirmation=after)
        # Exact read-back of acceptance, not inference from a successful write.
        observed = self.read()[2]['requests'].get(claims['request_id'])
        if observed != after['requests'][claims['request_id']]:
            deny('outcome_unknown')
        return deepcopy(result), False

    def saved_mapping(self, cid):
        bundle = json.loads(self.saved.path.read_text(encoding='utf8'))
        if digest(bundle) != self.saved.bundle_digest:
            deny('canonical_bundle_changed')
        return bundle['mappings'][cid]

    def journal_source(self, cid):
        state, _ = self.journal.read_versioned()
        return next(d['identity'] for d in state['batch']['drafts'].values() if d['case_id'] == cid)
