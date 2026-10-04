"""Read-only parent completion plan. No mover, writer, AI or state mutations.

The caller loads specs/outcomes from validated durable stores and supplies their
independent source and accounting read-back verifiers. Spreadsheet statuses and
an import ID alone are insufficient. Actual archive needs a separate canary.
"""
from dataclasses import dataclass
import re

from .drive_run_state import StateError
from .pdf_page_identity import page_identity

TERMINAL={'imported','duplicate_confirmed','intentionally_skipped',
          'manual_imported','medical_manual_imported'}


@dataclass(frozen=True)
class PageUnit:
    unit_id: str
    source_file_id: str
    source_content_hash: str
    page_numbers: tuple
    member_page_identities: tuple
    automatic_classification: str
    human_classification: str


@dataclass(frozen=True)
class TerminalOutcome:
    unit_id: str
    source_file_id: str
    source_content_hash: str
    page_numbers: tuple
    member_page_identities: tuple
    status: str
    confirmation_digest: str


def completion_plan(source, specs, load_outcomes, verify_outcome, verify_source):
    """Proof callbacks must re-read durable intent/source/rows, never UI cells."""
    sid=source['source_file_id'];content_hash=source['source_content_hash'];count=source['page_count']
    if (not re.fullmatch('[A-Za-z0-9_-]{1,150}',sid) or not re.fullmatch('[0-9a-f]{64}',content_hash)
            or type(count) is not int or not 1<=count<=50):
        raise StateError('pdf_terminal_source_invalid')
    if verify_source(source) is not True:raise StateError('pdf_terminal_source_changed')
    seen=set();ids=set()
    for unit in specs:
        if (not isinstance(unit,PageUnit) or unit.source_file_id!=sid or unit.source_content_hash!=content_hash
                or not unit.unit_id or unit.unit_id in ids or not unit.page_numbers
                or len(unit.page_numbers)!=len(unit.member_page_identities)
                or unit.page_numbers!=tuple(sorted(set(unit.page_numbers)))
                or any(type(n) is not int or not 1<=n<=count or n in seen for n in unit.page_numbers)
                or any(not re.fullmatch('[0-9a-f]{64}',h) for h in unit.member_page_identities)
                or unit.member_page_identities!=tuple(page_identity(content_hash,n,count) for n in unit.page_numbers)
                or unit.automatic_classification not in {'normal','medical','payroll','sensitive_unknown'}
                or unit.human_classification not in {'normal','medical','payroll','sensitive_unknown'}):
            raise StateError('pdf_terminal_partition_invalid')
        ids.add(unit.unit_id);seen.update(unit.page_numbers)
    if seen!=set(range(1,count+1)):raise StateError('pdf_terminal_partition_incomplete')
    outcomes=load_outcomes()
    if not isinstance(outcomes,dict) or not set(outcomes)<=ids:
        raise StateError('pdf_terminal_unknown_unit')
    rows=[]
    for unit in specs:
        outcome=outcomes.get(unit.unit_id)
        state='pending'
        if outcome is not None:
            if (not isinstance(outcome,TerminalOutcome) or outcome.unit_id!=unit.unit_id
                    or any(getattr(outcome,k)!=getattr(unit,k) for k in
                        ('source_file_id','source_content_hash','page_numbers','member_page_identities'))
                    or not re.fullmatch('[0-9a-f]{64}',outcome.confirmation_digest)):
                raise StateError('pdf_terminal_outcome_identity_changed')
            state=outcome.status
            if state in TERMINAL:
                if (state=='imported' and (unit.automatic_classification!='normal' or unit.human_classification!='normal')
                        or state=='manual_imported' and unit.human_classification!='normal'
                        or state=='medical_manual_imported' and unit.human_classification!='medical'):
                    raise StateError('pdf_terminal_route_conflict')
                if verify_outcome(unit,outcome) is not True:
                    raise StateError('pdf_terminal_readback_or_intent_mismatch')
        rows.append({'unit_id':unit.unit_id,'page_numbers':list(unit.page_numbers),'status':state,'terminal':state in TERMINAL})
    # A plan is never permission to move, even after all pages complete.
    if verify_source(source) is not True:raise StateError('pdf_terminal_source_changed')
    ready=all(r['terminal'] for r in rows)
    return {'source_file_id':sid,'source_content_hash':content_hash,'page_count':count,
        'units':rows,'all_units_terminal':ready,'archive_candidate':ready,'archive_allowed':False,
        'pending_unit_count':sum(not r['terminal'] for r in rows),'source_moves':0}
