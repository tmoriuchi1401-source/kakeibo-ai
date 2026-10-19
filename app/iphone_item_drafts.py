"""General item draft contract; no edit screen, POST or receipt-wide category."""
from copy import deepcopy

from .drive_run_state import StateError
from .receipt_item_review import card, snapshot, check_snapshot, evaluate, validate
from .unified_receipt_review import digest

STATES = {'unentered', 'entered', 'held', 'not_applicable'}


def fail():
    raise StateError('iphone_item_contract_changed')


def image_issue(legacy, kind, source_id):
    """Whole-image baseline; never constructs PDF Unit/index/partition identity."""
    candidates = [i for i in legacy.get('confirmation_items', {}).values()
                  if i['source']['source_id'] == source_id and i['kind'] == kind]
    if (len(candidates) != 1 or candidates[0]['source']['mime_type'] not in {'image/png', 'image/jpeg'}
            or candidates[0]['status'] != 'waiting' or candidates[0].get('error')
            or candidates[0].get('require_reconfirm')):
        fail()
    old = candidates[0]
    fields = dict(zip(('date','merchant','amount','category','payment'), old['inputs'][:5]))
    fields['memo'] = old['inputs'][7]
    entries = []
    if kind == 'normal':
        parsed = legacy['records'][source_id]['parsed']
        fields.update(date=parsed['date'], merchant=parsed['merchant'], amount=parsed['total'],
                      category='', payment=parsed['payment_method'])
        for n,item in enumerate(parsed['items'], 1):
            entries.append(dict(baseline_index=n, expense_id=f'R-{source_id}-{n:02d}', name=item['name'],
                amount=item['amount'], category=item['major_category']+'｜'+item['minor_category'],
                state='unentered', note=''))
    elif kind != 'medical':
        fail()
    # Medical editor starts empty: old parsed/manual values are comparison only.
    return dict(fields=fields, items=entries, provenance='human-entered')


def image_inspect(value, baseline, categories, kind):
    """Fixed catalog and exact values. Partial manual drafts can be retained."""
    from .receipt_confirmation_draft_request import keys, text, integer
    try:
        keys(value, 'fields items provenance')
        keys(value['fields'], 'date merchant amount category payment memo')
        if value['provenance'] != 'human-entered': fail()
        for k,v in value['fields'].items():
            if k == 'amount':
                if v != '': integer(v, 1, 999999999999)
            else: text(v)
        if type(value['items']) is not list or len(value['items']) != len(baseline['items']): fail()
        for e,b in zip(value['items'], baseline['items']):
            keys(e, 'baseline_index expense_id name amount category state note')
            if any(e[k] != b[k] for k in ('baseline_index','expense_id')) or e['state'] not in STATES: fail()
            integer(e['amount'], -999999999999, 999999999999)
            text(e['name']); text(e['note'], limit=8000); text(e['category'])
            if e['category'] and e['category'].split('｜') not in categories: fail()
        if kind == 'normal' and value['fields']['category'] != '': fail()
        if kind == 'medical' and value['fields']['category'] and value['fields']['category'].split('｜') not in categories: fail()
        return deepcopy(value)
    except (KeyError, TypeError, ValueError, AttributeError):
        fail()


def issue(record, saved, categories):
    """Manager-owned native record/snapshot. Existing token/markers are retained."""
    validate(record)
    view = card(record, categories=categories)
    current = check_snapshot(saved, view)
    evaluate(record, current, categories)
    value = dict(schema='iphone-item-draft-v1', record_digest=record['digest'],
                 snapshot_digest=digest(saved), native_snapshot=deepcopy(saved),
                 entries=[dict(item_id=i['item_id'], marker='item:'+i['item_id'],
                     identity=deepcopy(saved['identity']), token=saved['token'],
                     original_link=saved['original_link'], old=deepcopy(i), proposed=deepcopy(i),
                     state='unentered', note='') for i in saved['items']])
    return value


def inspect(value, record, saved, categories):
    """Partial entries remain saveable; only a complete native dry-run can pass.

    not_applicable is an explicit unresolved disposition, never item deletion.
    Native evaluate validates each item/category and the complete receipt sum,
    allocation and structure gates. Amount/name edits follow existing rules.
    """
    try:
        baseline = issue(record, saved, categories)
        if (set(value) != set(baseline) or any(value[k] != baseline[k] for k in baseline if k != 'entries')
                or len(value['entries']) != len(baseline['entries'])):
            fail()
        current = check_snapshot(saved, card(record, categories=categories))
        seen = set()
        for actual, expected in zip(value['entries'], baseline['entries']):
            if (set(actual) != set(expected) or actual['item_id'] in seen
                    or any(actual[k] != expected[k] for k in expected if k not in {'proposed', 'state', 'note'})
                    or actual['state'] not in STATES or not isinstance(actual['note'], str) or len(actual['note']) > 8000):
                fail()
            seen.add(actual['item_id'])
        current['items'] = [deepcopy(e['proposed']) for e in value['entries']]
        current['action'] = '未選択'
        result = evaluate(record, current, categories)
        proposed_snapshot = snapshot(card(record, current, categories=categories), current)
        # Re-run the exact native presentation/token/identity validator too.
        check_snapshot(proposed_snapshot, card(record, categories=categories))
        complete = all(e['state'] == 'entered' for e in value['entries'])
        return dict(status='ready_for_owner_redisplay' if complete and result['status'] == 'ready_to_confirm'
                    else 'partial_or_held', native=result, proposed_snapshot=proposed_snapshot,
                    input_digest=digest(value), owner_reconfirmation_required=True,
                    authorized=False, operation_targets=[])
    except (KeyError, TypeError, ValueError, AttributeError):
        fail()
