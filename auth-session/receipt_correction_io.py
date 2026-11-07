"""Read-only source adapter and result-only private CAS journal.

Ports reuse existing Drive metadata/bytes, Sheets raw reads and private journal
read_versioned/replace_versioned. No credentials, resource creation or live IDs.
The logical one-tab mapping and protected case anchors are NOT live-installed.
"""
from copy import deepcopy
from hashlib import sha256
import re

from app.drive_run_state import StateError
from app.conditional_drive_state_v2 import strong_etag
from app.human_general_auth_transport import AuthRequestStore, ISSUER
from app.page_receipt_model import digest

RESULT_SCOPE = 'receipt-correction-results-only-v1'
STATES = {'受付済み', '検証中', '修正が必要', '検証済み・未記帳', '結果不明・照合待ち'}
FINAL = {'修正が必要', '検証済み・未記帳'}


class IndependentReceiptReader:
    def __init__(self, *, metadata, source_bytes, sheet_values, private_cases,
                 spreadsheet_id, private_id, source_folder, owner_email, service_email,
                 parse_imports, parse_rules):
        self.metadata, self.source_bytes = metadata, source_bytes
        self.sheet_values, self.private_cases = sheet_values, private_cases
        self.sid, self.pid, self.folder = spreadsheet_id, private_id, source_folder
        self.owner, self.service = owner_email, service_email
        self.parse_imports, self.parse_rules = parse_imports, parse_rules

    def checked_metadata(self, fid):
        m = deepcopy(self.metadata(fid))
        grants = {(p.get('type'), p.get('role'), p.get('emailAddress'))
                  for p in m.get('permissions', []) if not p.get('deleted')}
        if (m.get('id') != fid or m.get('trashed') is not False
                or [o.get('emailAddress') for o in m.get('owners', [])] != [self.owner]
                or grants != {('user', 'owner', self.owner), ('user', 'writer', self.service)}
                or not isinstance(m.get('version'), str) or not m['version']):
            raise StateError('receipt_reader_acl_or_identity_changed')
        return m

    def __call__(self, case_id):
        try:
            before = {fid:self.checked_metadata(fid) for fid in (self.sid, self.pid)}
            if (before[self.sid]['mimeType'] != 'application/vnd.google-apps.spreadsheet'
                    or before[self.pid]['mimeType'] != 'application/json'):raise ValueError()
            catalog, tag = self.private_cases()
            catalog = deepcopy(catalog)
            if not strong_etag(tag):raise ValueError()
            anchor = catalog['cases'][case_id]
            source = anchor['source']
            sid = source['source_id']
            original = self.checked_metadata(sid)
            if (original['version'] != source['version'] or original['mimeType'] != source['mime_type']
                    or original.get('parents') != [self.folder]):raise ValueError()
            sheets = deepcopy(self.sheet_values())
            if sheets['sheet'] != '確認':raise ValueError()
            rows = [r for r in sheets['cases'] if r['case_id'] == case_id]
            txs = [t for t in self.parse_imports(sheets['imports']) if t.import_id == case_id]
            if len(rows) != 1 or len(txs) != 1 or rows[0]['source'] != source:raise ValueError()
            selected = [r for _,r in sheets['candidates'] if len(r)>1 and r[1] == case_id]
            if (digest(selected) != anchor['candidate_digest']
                    or digest(txs[0].row) != anchor['import_digest']):raise ValueError()
            raw = self.source_bytes(sid)
            if type(raw) is not bytes or sha256(raw).hexdigest() != source['sha256']:raise ValueError()
            # No source or UI cache: reject changes during the entire read.
            if (self.checked_metadata(sid) != original or self.sheet_values() != sheets
                    or self.private_cases() != (catalog, tag)
                    or any(self.checked_metadata(fid) != m for fid,m in before.items())):raise ValueError()
            return dict(tx=txs[0], indexed_rows=deepcopy(sheets['candidates']), source=deepcopy(source),
                        source_bytes=raw, operations=deepcopy(rows[0]['operations']),
                        categories=[tuple(c) for c in sheets['categories']],
                        approved_product_rules=self.parse_rules(sheets['rules']),
                        total_confirmation=deepcopy(anchor['total_confirmation']))
        except StateError:raise
        except Exception:raise StateError('receipt_reader_changed_or_unavailable') from None


def empty_results():
    return dict(schema=RESULT_SCOPE, binding=RESULT_SCOPE, generation=0, results={})


def validate_results(value, scope):
    try:
        if (scope != RESULT_SCOPE or set(value) != {'schema','binding','generation','results'}
                or value['schema'] != scope or value['binding'] != scope
                or type(value['generation']) is not int or value['generation'] < 0
                or not isinstance(value['results'], dict) or len(value['results']) > 1000):raise ValueError()
        for rid,r in value['results'].items():
            if (set(r) != {'request_id','actor_id','binding','binding_digest','state','validation','registration_digest'}
                    or rid != r['request_id'] or r['binding']['request_id'] != rid
                    or r['binding']['requested_action'] != 'register_receipt_item_correction_validation_v1'
                    or r['binding']['snapshot']['case_id'] != r['binding']['case_id']
                    or r['binding']['snapshot']['source'] != r['binding']['source']
                    or r['binding_digest'] != digest(r['binding']) or r['state'] not in STATES
                    or not re.fullmatch('[a-f0-9]{64}',r['actor_id'])):raise ValueError()
            if r['state'] in FINAL:
                v = r['validation']
                if (v['state'] != r['state'] or v['posting_authority'] is not False
                        or v['financial_write_count'] != 0
                        or v['snapshot_digest'] != r['binding']['snapshot']['digest']
                        or r['registration_digest'] != digest([r['binding']['requested_action'],
                            r['binding_digest'],r['actor_id'],v])):raise ValueError()
            elif r['validation'] is not None or r['registration_digest'] is not None:raise ValueError()
    except Exception:raise StateError('receipt_result_journal_invalid') from None
    return value


class ResultJournal:
    """Existing AuthRequestStore CAS/read-back over a dedicated non-TTL port.

    Never point this at authority, bank, a Sheet cell or a 600-second auth store.
    The caller must supply the existing private ACL preflight on its transport.
    """
    def __init__(self, transport, *, preflight, clock):
        self.store = AuthRequestStore(transport, RESULT_SCOPE, preflight=preflight, validator=validate_results)
        self.clock, self.uncertain = clock, False

    def transition(self, record, state, validation=None):
        rid = record['request_id']
        value = self.store.load()
        old = value['results'].get(rid)
        base = dict(request_id=rid, actor_id=record['actor']['actor_id'],
                    binding=deepcopy(record['binding']), binding_digest=record['digest'])
        if old and any(old[k] != v for k,v in base.items()):
            raise StateError('receipt_result_binding_conflict')
        if state == '受付済み' and old:
            raise StateError('receipt_result_request_already_accepted')
        if old and old['state'] in FINAL:
            if state not in FINAL or old['validation'] == validation:return
            raise StateError('receipt_result_terminal_conflict')
        if old and old['state'] == state and old['validation'] == validation:return
        allowed = {'受付済み':{'検証中','結果不明・照合待ち'},
                   '検証中':FINAL | {'結果不明・照合待ち'}, '結果不明・照合待ち':set()}
        if old and state not in allowed[old['state']]:raise StateError('receipt_result_transition_forbidden')
        receipt = None if validation is None else digest([base['binding']['requested_action'],
                    base['binding_digest'],base['actor_id'],validation])
        value['results'][rid] = dict(**base, state=state, validation=deepcopy(validation), registration_digest=receipt)
        value['generation'] += 1
        try:self.store.save(value)
        except Exception:
            self.uncertain = True
            raise  # No retry or unconditional replacement of uncertain state.

    def failed(self, record):
        if not self.uncertain:
            self.transition(record, '結果不明・照合待ち')

    def query(self, rid, *, logins, cookie):
        # Query authentication is fresh and independent of old request expiry.
        current = logins.current(cookie)
        if current is None:raise StateError('receipt_result_login_required')
        actor = digest([ISSUER,current['subject']])
        result = self.store.load()['results'].get(rid)
        if result is None or result['actor_id'] != actor:raise StateError('receipt_result_access_denied')
        result = deepcopy(result)
        if result['state'] in {'受付済み','検証中'} and self.clock() >= result['binding']['expires_at']:
            result['state'] = '結果不明・照合待ち'
        return result


def project_result(rows, result):
    """Pure one-tab result projection. Never updates live Sheets or input cells."""
    case_id, source = result['binding']['case_id'], result['binding']['source']
    found = [i for i,r in enumerate(rows) if r['case_id'] == case_id]
    if len(found) != 1:raise StateError('receipt_result_case_missing_or_duplicate')
    i = found[0]
    row = rows[i]
    if (row['source'] != source or row['submission'] != {
            'request_id':result['request_id'], 'snapshot_digest':result['binding']['snapshot']['digest']}):
        raise StateError('receipt_result_projection_stale')
    output = deepcopy(rows)
    output[i]['result'] = dict(request_id=result['request_id'], state=result['state'],
                              snapshot_digest=result['binding']['snapshot']['digest'],
                              draft_notice='登録時の明細と一致' if row['operations'] ==
                              result['binding']['snapshot']['operations'] else '未登録の変更あり')
    return output
