"""Existing item proof contract and scoped independent-read evidence."""
from copy import deepcopy
import unicodedata
from .drive_run_state import StateError
from .page_receipt_model import digest
from .receipt_plan.proof import ConfirmedItems, covers, RESOLVABLE, CHOICES, binding

def annotated(record, readings, categories, *, adjustment_targets=None):
    """Trusted typed readings of the same frozen unit; no global gate removal."""
    from .receipt_item_review import validate
    validate(record)
    if len(readings)<2:raise StateError('item_evidence_independent_readings_required')
    result=deepcopy(record);evidence=[]
    base=record['legacy']['parsed']['items']
    for reading in readings:
        if (len(reading.items)!=len(base) or reading.transaction_kind!='purchase' or
                reading.total!=record['legacy']['parsed']['total']):
            raise StateError('item_evidence_unit_unstable')
    for index,(item,original) in enumerate(zip(result['items'],base)):
        samples=[r.items[index] for r in readings]
        norm=lambda v:unicodedata.normalize('NFKC',v)
        mapped=all(norm(s.name)==norm(original['name']) and s.quantity==original['quantity']
                   and s.amount==original['amount'] for s in samples)
        same_shape=all((s.name,s.quantity,s.amount)==(original['name'],original['quantity'],original['amount']) for s in samples)
        pairs={(s.major_category,s.minor_category) for s in samples}
        stable=mapped and len(pairs)==1 and next(iter(pairs)) in set(categories) and all(s.confidence>=.8 for s in samples)
        # Only the two narrow reread gates may coexist with local prefill.
        safe=not set(record['legacy']['hard_issues'])-RESOLVABLE
        item['category']='｜'.join(next(iter(pairs))) if stable and safe and item['kind']=='product' else ''
        evidence.append({'item_id':item['item_id'],'mapped':mapped,'shape_agrees':same_shape,
            'category_agrees':stable,'category':item['category'],
            'readings':[{'name':s.name,'quantity':s.quantity,'amount':s.amount,
                         'category':'｜'.join((s.major_category,s.minor_category))} for s in samples]})
    if adjustment_targets is not None:result['adjustment_targets']=deepcopy(adjustment_targets)
    result['review_evidence']={'schema':1,'items':evidence,'full_structure_confirmation_required':
                              bool(set(record['legacy']['hard_issues'])&RESOLVABLE)}
    result.pop('digest');result['digest']=digest(result)
    return validate(result)
