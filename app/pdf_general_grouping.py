"""Transaction-only scope; human normal never rewrites privacy observations."""
from dataclasses import replace

from .drive_run_state import StateError
from .pdf_page_kind import current_answer
from .receipt_pdf_grouping import AdjacentPageGrouping, PageEvidence

SCOPE_FIELDS = ('grouping_page_numbers', 'page_kind_digests')


def scope_snapshot(snapshot, value):
    proofs, numbers = [], []
    for page in snapshot['pages']:
        answer = current_answer(value, page)
        proofs.append(answer['confirmation_digest'] if answer else '')
        routing = answer['human_classification'] if answer else page['classification']
        if routing == 'normal' and page['classification'] not in {'medical', 'payroll'}:
            numbers.append(page['page_number'])
    if not numbers:
        raise StateError('grouping_general_pages_required')
    return {**snapshot, 'grouping_page_numbers': numbers, 'page_kind_digests': proofs}


def scope_current(value, proposal):
    if not any(k in proposal for k in SCOPE_FIELDS):
        return True  # legacy whole-document grouping
    try:
        current = scope_snapshot(proposal, value)
        return all(current[k] == proposal[k] for k in SCOPE_FIELDS)
    except Exception:
        return False


def general_candidates(observations, scoped, evidence):
    numbers = scoped['grouping_page_numbers']
    if set(evidence) != set(numbers) or any(not isinstance(evidence[n], PageEvidence) for n in numbers):
        raise StateError('grouping_evidence_incomplete')
    # This local copy is a proposer input only. The persisted pages, privacy,
    # extraction states and payload gates are always the original snapshot.
    selected = replace(observations, pages=tuple(replace(p, classification='normal', _payload=None)
                       for p in observations.pages if p.page_number in numbers))
    return AdjacentPageGrouping(lambda obs: tuple(evidence[p.page_number] for p in obs.pages)).propose(selected)
