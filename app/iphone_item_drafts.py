"""General item draft contract; no edit screen, POST or receipt-wide category."""
from copy import deepcopy

from .drive_run_state import StateError
from .receipt_item_review import card, snapshot, check_snapshot, evaluate, validate
from .unified_receipt_review import digest

STATES = {'unentered', 'entered', 'held', 'not_applicable'}


def fail():
    raise StateError('iphone_item_contract_changed')


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
