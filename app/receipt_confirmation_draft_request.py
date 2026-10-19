"""Explicit, memory-only draft inputs v2. No writer, host or authority issuer.

The immutable legacy document is never used as an editable working document.
Every proposed value is independently derived from versioned trusted readers.
"""
from copy import deepcopy
from hashlib import sha256
from math import isfinite
from types import SimpleNamespace

from .drive_run_state import StateError
from .models import ReceiptResult
from .page_receipt_model import SourceRef, stable_page
from .receipt_confirmation import ReceiptConfirmation, review_id
from .receipt_confirmation_contract import (encoded, load, memory_only, snapshot, token,
    validate_candidate, validate_request as validate_old_request, validate_token)
from .receipt_reimport_production import digest as native_digest
from .receipt_validation import validate_receipt_result
from .unified_receipt_review import digest
from .unified_receipt_write import OLD_P1_SOURCE, OLD_REFERENCES
from .iphone_accounting_ownership import hashed

SCHEMA = 'receipt-confirmation-draft-inputs-v2'
FIELDS = ('date', 'merchant', 'amount', 'category', 'payment', 'memo')
MAX_REQUEST_BYTES = 300000


def fail(code):
    raise StateError('draft_v2_' + code)


def keys(value, names):
    if type(value) is not dict or set(value) != set(names.split()):
        fail('fields')


def text(value, *, required=False, limit=4000):
    if (type(value) is not str or len(value) > limit or required and not value.strip()
            or value.lstrip().startswith(('=', '+', '-', '@'))):
        fail('text')


def integer(value, low, high):
    if type(value) is not int or not low <= value <= high:
        fail('integer')


def strict_tree(value):
    """Reject coercion/non-JSON values before existing typed parsers can coerce."""
    if type(value) is dict:
        if any(type(k) is not str for k in value): fail('json_key')
        for v in value.values(): strict_tree(v)
    elif type(value) is list:
        for v in value: strict_tree(v)
    elif type(value) not in (str, int, float, bool, type(None)):
        fail('json_type')
    elif type(value) is float and not isfinite(value):
        fail('nonfinite')


def decode_request(raw):
    if type(raw) is not bytes or len(raw) > MAX_REQUEST_BYTES: fail('capacity')
    try:
        result = load(raw)
        strict_tree(result)
        return result
    except (ValueError, TypeError, RecursionError) as error:
        raise StateError('draft_v2_json') from error


def validate_shape(c):
    strict_tree(c)
    if len(encoded(c)) > MAX_REQUEST_BYTES: fail('capacity')
    validate_candidate(c)  # Unchanged outer/authority/ownership schema.
    for k in ('issued_at', 'expires_at', 'revision'):
        if type(c['authority'][k]) is not int: fail('authority_type')
    if type(c['authority']['revoked']) is not bool: fail('authority_type')
    i = c['inputs']
    keys(i, 'schema baseline draft source_binding proposed categories derivation')
    if i['schema'] != SCHEMA: fail('schema')
    keys(i['baseline'], 'legacy_digest item candidate_digest native_initial_observation')
    validate_token(i['baseline']['native_initial_observation'])
    keys(i['draft'], 'domain owner case_id generation revision input_digest value observation')
    validate_token(i['draft']['observation'])
    keys(i['source_binding'], 'source privacy_state page_identity scope receipt_unit_id receipt_index observation')
    validate_token(i['source_binding']['observation'])
    keys(i['categories'], 'pairs revision observation')
    validate_token(i['categories']['observation'])
    keys(i['proposed'], 'confirmed parsed lines original_parsed original_plan')
    keys(i['derivation'], 'prepared_at baseline_digest source_digest line_digest before_digest original_plan_digest plan_digest')
    text(i['derivation']['prepared_at'], required=True)
    for field, value in i['derivation'].items():
        if field != 'prepared_at' and not hashed(value): fail('digest')
    for field in ('legacy_digest', 'candidate_digest'):
        if not hashed(i['baseline'][field]): fail('digest')
    d = i['draft']
    keys(d['value'], 'fields items provenance')
    keys(d['value']['fields'], 'date merchant amount category payment memo')
    if (d['domain'] != 'DRAFT_ONLY' or d['owner'] != c['owner'] or d['case_id'] != c['case_id']
            or d['input_digest'] != digest(d['value']) or d['value']['provenance'] != 'human-entered'): fail('draft')
    integer(d['generation'], 1, 2**53-1); integer(d['revision'], 0, 2**53-1)
    for field, value in d['value']['fields'].items():
        if field == 'amount': integer(value, 1, 999999999999)
        else: text(value, required=field in {'date', 'merchant'})
    if type(d['value']['items']) is not list or len(d['value']['items']) > 100: fail('catalog')
    for n, entry in enumerate(d['value']['items'], 1):
        keys(entry, 'baseline_index expense_id name amount category state note')
        integer(entry['baseline_index'], n, n); integer(entry['amount'], -999999999999, 999999999999)
        for field in ('expense_id', 'name', 'category'): text(entry[field], required=True)
        text(entry['note'], limit=8000)
        if entry['state'] != 'entered': fail('line_state')
    proposed = i['proposed']
    if type(proposed['confirmed']) is not list or len(proposed['confirmed']) != 8: fail('confirmed')
    integer(proposed['confirmed'][2], 1, 999999999999)
    for n, value in enumerate(proposed['confirmed']):
        if n != 2: text(value)
    fields = d['value']['fields']
    expected = [fields[k] for k in FIELDS[:5]] + [
        '候補明細で確定' if c['kind'] == 'normal' else '医療費を確定', '', fields['memo']]
    if encoded(proposed['confirmed']) != encoded(expected): fail('confirmed_draft')
    if c['kind'] == 'medical' and d['value']['items']: fail('manual_only')
    master = i['categories']
    text(master['revision'], required=True)
    if type(master['pairs']) is not list or not master['pairs'] or len(master['pairs']) > 1000: fail('master')
    for pair in master['pairs']:
        if type(pair) is not list or len(pair) != 2: fail('master')
        for value in pair: text(value, required=True)
    if type(proposed['lines']) is not list: fail('catalog')
    for n, line in enumerate(proposed['lines'], 1):
        keys(line, 'index expense_id baseline_item_digest'); integer(line['index'], n, n)
        text(line['expense_id'], required=True)
        if not hashed(line['baseline_item_digest']): fail('catalog_digest')
    for parsed in (proposed['parsed'], proposed['original_parsed']):
        if parsed is None:
            if c['kind'] != 'medical' or parsed is proposed['parsed']: fail('parsed')
            continue
        keys(parsed, 'merchant transaction_kind date total payment_method items note')
        if (type(parsed['items']) is not list or not 1 <= len(parsed['items']) <= 100
                or type(parsed['total']) is not int
                or any(type(parsed[k]) is not str for k in ('merchant', 'transaction_kind', 'date', 'payment_method', 'note'))): fail('parsed')
        for item in parsed['items']:
            keys(item, 'name quantity amount major_category minor_category note confidence')
            if (type(item['amount']) is not int or any(type(item[k]) not in (int, float) for k in ('quantity', 'confidence'))):
                fail('parsed_type')
            if any(type(item[k]) is not str for k in ('name', 'major_category', 'minor_category', 'note')): fail('parsed_type')
        # Strict field keys above prevent Pydantic's legacy extra-field ignoring.
        ReceiptResult.model_validate(parsed)
    sb = i['source_binding']
    if (sb['source'] != c['source'] or sb['scope'] != 'whole' or sb['receipt_unit_id'] is not None
            or sb['receipt_index'] is not None): fail('whole_source')
    old = i['baseline']['item']
    if (old['kind'] != c['kind'] or old['status'] != 'waiting' or old.get('require_reconfirm')
            or old.get('error') or old.get('decision_origin') == 'automatic'
            or c['review_id'] != review_id(c['kind'], old['source'])
            or c['ownership']['case_id'] != c['case_id']
            or c['case_id'] in {'G103', 'H301', 'SYN-G103', 'SYN-H301'}
            or c['source']['source_id'] == OLD_P1_SOURCE or c['review_id'] in OLD_REFERENCES
            or c['source']['source_kind'] != 'image' or c['source']['page_count'] != 1):
        fail('held_or_identity')
    ids = [r[0] for _, r in c['plan']]
    if (ids != c['ownership']['ledger_targets'] or len(ids) != len(set(ids))
            or c['plan'][0][0] != 'レシート' or c['plan'][-1][0] != '取込データ'
            or ids[0] != 'R-'+c['source']['source_id'] or ids[-1] != 'receipt:'+c['source']['source_id']):
        fail('plan_identity')
    return deepcopy(c)


class DraftReaders:
    """Injected trusted memory ports only; never constructed from request fields."""
    def __init__(self, *, draft, source, categories, ledger, source_bytes, authority, ownership):
        for port in (draft, source, categories, ledger, source_bytes, authority): memory_only(port)
        self.draft, self.source, self.categories, self.ledger = draft, source, categories, ledger
        self.source_bytes, self.authority, self.ownership = source_bytes, authority, ownership

    def observe(self):
        ports = (self.draft, self.source, self.categories, self.ledger)
        frames = [p.read_versioned() for p in ports]
        values = [load(raw) for raw, _ in frames]
        for raw, tag in frames: token(raw, tag)
        source = values[1]['source']
        raw = self.source_bytes.revision_bytes(source['source_id'], source['drive_revision_id'])
        if type(raw) is not bytes or sha256(raw).hexdigest() != source['sha256']: fail('source_hash')
        if ([p.read_versioned() for p in ports] != frames
                or self.source_bytes.revision_bytes(source['source_id'], source['drive_revision_id']) != raw):
            fail('double_read_changed')
        return values, [token(*f) for f in frames]


class PlanningDB:
    """Read-only evaluated ledger projection. No append/update/materializer."""
    def __init__(self, before, categories): self.before, self.pairs = before, categories
    def get(self, region): return deepcopy(self.before[0][region.split('!')[0].strip("'")])
    def categories(self): return [tuple(p) for p in self.pairs]


def _derive(legacy, kind, case_id, owner, native_observation, prepared_at, values, observations):
    draft, source_frame, master, ledger = deepcopy(values)
    keys(draft, 'domain owner case_id generation revision input_digest value')
    keys(draft['value'], 'fields items provenance')
    keys(source_frame, 'source privacy_state')
    keys(master, 'pairs revision')
    keys(ledger, 'complete snapshot')
    if ledger['complete'] is not True: fail('ledger_incomplete')
    before = snapshot(ledger['snapshot'])
    source = source_frame['source']
    from .iphone_accounting_ownership import source_valid
    source_valid(source)
    if (source['source_kind'] != 'image' or source['page_count'] != 1
            or source_frame['privacy_state'] != ('general-eligible' if kind == 'normal' else 'manual-medical')):
        fail('whole_image_only')
    sid = source['source_id']
    candidates = [v for v in legacy.get('confirmation_items', {}).values()
                  if v['kind'] == kind and v['source']['source_id'] == sid]
    if len(candidates) != 1: fail('baseline_identity')
    old = candidates[0]
    if (old['status'] != 'waiting' or old.get('error') or old.get('require_reconfirm')
            or old.get('decision_origin') == 'automatic' or sid == OLD_P1_SOURCE
            or case_id in {'G103', 'H301', 'SYN-G103', 'SYN-H301'}
            or review_id(kind, old['source']) in OLD_REFERENCES
            or (sid, source['sha256'], source['native_source_version']) != (
                old['source']['source_id'], old['source']['sha256'], old['source']['version'])
            or old['source']['mime_type'] not in {'image/png', 'image/jpeg'}):
        fail('baseline_held_or_changed')
    if any(v['source']['source_id'] == sid and v['status'] in {'pending', 'applied'}
           for v in legacy.get('confirmation_items', {}).values()): fail('legacy_fence')
    if (draft['domain'] != 'DRAFT_ONLY' or draft['owner'] != owner or draft['case_id'] != case_id
            or draft['input_digest'] != digest(draft['value']) or draft['value']['provenance'] != 'human-entered'):
        fail('saved_draft')
    integer(draft['generation'], 1, 2**53-1); integer(draft['revision'], 0, 2**53-1)
    pairs = master['pairs']
    text(master['revision'], required=True)
    if type(pairs) is not list or not pairs or len(pairs) > 1000: fail('master')
    for pair in pairs:
        if type(pair) is not list or len(pair) != 2: fail('master')
        for v in pair: text(v, required=True)
    if len({tuple(p) for p in pairs}) != len(pairs): fail('master_duplicate')
    fields, entries = draft['value']['fields'], draft['value']['items']
    keys(fields, 'date merchant amount category payment memo')
    for k in FIELDS:
        if k != 'amount': text(fields[k], required=k in {'date', 'merchant'})
    integer(fields['amount'], 1, 999999999999)
    if type(entries) is not list or len(entries) > 100: fail('line_capacity')
    confirmed = [fields[k] for k in FIELDS[:5]] + [
        '候補明細で確定' if kind == 'normal' else '医療費を確定', '', fields['memo']]
    execution = deepcopy(old); execution['inputs'] = confirmed
    shadow = object.__new__(ReceiptConfirmation)
    shadow.store = SimpleNamespace(value=deepcopy(legacy))
    shadow.db = PlanningDB(before, pairs)
    lines = []
    original_parsed = None
    if kind == 'normal':
        if fields['category'] != '': fail('receipt_category')
        original = legacy['records'][sid]['parsed']
        original_parsed = ReceiptResult.model_validate(original)
        if len(entries) != len(original['items']) or not entries: fail('fixed_catalog')
        candidate = deepcopy(original)
        candidate.update(date=fields['date'], merchant=fields['merchant'], total=fields['amount'], payment_method=fields['payment'])
        for index, (entry, baseline) in enumerate(zip(entries, original['items']), 1):
            keys(entry, 'baseline_index expense_id name amount category state note')
            integer(entry['baseline_index'], index, index)
            if entry['expense_id'] != f'R-{sid}-{index:02d}' or entry['state'] != 'entered': fail('line_identity')
            text(entry['name'], required=True); text(entry['note'], limit=8000); text(entry['category'], required=True)
            integer(entry['amount'], -999999999999, 999999999999)
            pair = entry['category'].split('｜')
            if pair not in pairs: fail('line_category')
            candidate['items'][index-1] = dict(deepcopy(baseline), name=entry['name'], amount=entry['amount'],
                major_category=pair[0], minor_category=pair[1])
            lines.append(dict(index=index, expense_id=entry['expense_id'], baseline_item_digest=digest(baseline)))
        catalog_ids = {line['expense_id'] for line in lines}
        for row in old['before']['expense_rows']:
            if (len(row) < 13 or row[0] not in catalog_ids or row[8:11] != ['receipt', 'R-'+sid, 'receipt:'+sid]
                    or row[12] != 'active'): fail('existing_expense_identity')
        parsed = ReceiptResult.model_validate(candidate)
    elif kind == 'medical':
        if entries or not fields['category'].strip(): fail('complete_manual_only')
        parsed = shadow._parsed(execution)  # Existing manual parser; no candidate prefill.
        # An incomplete old manual entry has no original plan. It is not filled.
        if all(str(old['inputs'][n]).strip() for n in (0, 1, 2, 3)):
            original_parsed = shadow._parsed(deepcopy(old))
    else: fail('kind')
    for result in (parsed, original_parsed):
        if result is not None and not validate_receipt_result(result, [tuple(p) for p in pairs])[0]: fail('receipt_validator')
    def plan(item, result):
        p = shadow._plan(deepcopy(item), result, '')
        for title, row in p:
            if any(r[0] == row[0] for r in before[0][title]): continue
            if title == 'レシート': row[7] = prepared_at
            if title == '取込データ': row[1] = prepared_at
        return p
    original_plan = plan(old, original_parsed) if original_parsed is not None else None
    proposed_plan = plan(execution, parsed)
    source_binding = dict(source=source, privacy_state=source_frame['privacy_state'],
        page_identity=stable_page(SourceRef(source_file_id=sid, source_content_hash=source['sha256'],
            source_kind='image', page_count=1), 1), scope='whole', receipt_unit_id=None, receipt_index=None,
        observation=observations[1])
    inputs = dict(schema=SCHEMA, baseline=dict(legacy_digest=digest(legacy), item=old,
        candidate_digest=digest(legacy['records'].get(sid)), native_initial_observation=native_observation),
        draft=dict(draft, observation=observations[0]), source_binding=source_binding,
        proposed=dict(confirmed=confirmed, parsed=parsed.model_dump(), lines=lines,
            original_parsed=original_parsed.model_dump() if original_parsed is not None else None, original_plan=original_plan),
        categories=dict(master, observation=observations[2]),
        derivation=dict(prepared_at=prepared_at, baseline_digest=digest(legacy), source_digest=digest(source_binding),
            line_digest=digest(lines), before_digest=digest(before), original_plan_digest=digest(original_plan),
            plan_digest=digest(proposed_plan)))
    return inputs, before, proposed_plan


def prepare_inputs(legacy, kind, case_id, owner, native_observation, prepared_at, readers):
    """Read-only server preparation; returns no authority or financial request."""
    if type(readers) is not DraftReaders: fail('trusted_readers')
    validate_token(native_observation); text(prepared_at, required=True)
    values, observations = readers.observe()
    strict_tree(values)
    result = _derive(legacy, kind, case_id, owner, native_observation, prepared_at, values, observations)
    if readers.observe() != (values, observations): fail('derive_read_changed')
    return deepcopy(result)


def validate_draft_request_v2(c, legacy, readers, *, check_ledger=True):
    """No old-validator fallback. Comparison uses trusted saved values, not digests alone."""
    try:
        validate_shape(c)
        i = c['inputs']
        values, observations = readers.observe()
        original_frames = deepcopy((values, observations))
        # Inspection uses the authenticated frozen before for plan derivation;
        # current ledger is independently classified by the contract, never
        # silently substituted for a mutation precondition.
        if not check_ledger: values[3] = dict(complete=True, snapshot=deepcopy(c['before']))
        inputs, before, plan = _derive(legacy, c['kind'], c['case_id'], c['owner'],
            i['baseline']['native_initial_observation'], i['derivation']['prepared_at'], values, observations)
        if (encoded(i) != encoded(inputs) or encoded(c['before']) != encoded(before) or encoded(c['plan']) != encoded(plan)
                or c['source'] != inputs['source_binding']['source']
                or readers.ownership().value() != c['ownership']): fail('independent_recalculation')
        raw, tag = readers.authority.read_versioned()
        token(raw, tag)
        if load(raw) != c['authority'] or readers.authority.read_versioned() != (raw, tag): fail('authority_changed')
        if readers.observe() != tuple(original_frames): fail('validation_double_read_changed')
        return deepcopy(c)
    except (KeyError, ValueError, TypeError, AttributeError, IndexError) as error:
        raise StateError('draft_v2_invalid') from error


def validate_request(c, *, legacy=None, readers=None):
    if type(c) is bytes: c = decode_request(c)
    if type(c) is not dict or type(c.get('inputs')) is not dict: fail('request')
    if 'schema' not in c['inputs']:
        return validate_old_request(c)
    if c['inputs']['schema'] != SCHEMA: fail('unknown_schema')
    return validate_draft_request_v2(c, legacy, readers)


def pending_projection(c):
    item = deepcopy(c['inputs']['baseline']['item'])
    item.update(inputs=deepcopy(c['inputs']['proposed']['confirmed']), status='pending', plan=deepcopy(c['plan']))
    item['confirmation_hash'] = native_digest(item['inputs'])
    if item['kind'] == 'medical': item['decision_origin'] = 'human'
    return item
