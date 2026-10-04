from dataclasses import replace
from unittest.mock import Mock

import pytest
from app.pdf_terminal_plan import PageUnit, TerminalOutcome, completion_plan
from app.pdf_page_identity import page_identity
from app.drive_run_state import StateError


def context():
    source=dict(source_file_id='synthetic-source',source_content_hash='a'*64,page_count=14)
    specs=[PageUnit(str(n),source['source_file_id'],source['source_content_hash'],(n,),
        (page_identity(source['source_content_hash'],n,14),),
        'normal' if n not in {1,4,10,14} else 'sensitive_unknown','medical' if n==1 else 'normal') for n in range(1,15)]
    outcomes={s.unit_id:TerminalOutcome(s.unit_id,s.source_file_id,s.source_content_hash,s.page_numbers,
        s.member_page_identities,'medical_manual_imported' if s.unit_id=='1' else
        'manual_imported' if s.unit_id in {'4','10','14'} else 'imported','c'*64) for s in specs}
    return source,specs,outcomes,Mock(return_value=True),Mock(return_value=True)


def run(values):
    source,specs,outcomes,readback,fresh=values
    return completion_plan(source,specs,lambda:outcomes,readback,fresh)


def test_only_all_fourteen_terminal_can_be_archive_candidate_still_no_permission():
    args=context();plan=run(args)
    assert plan['all_units_terminal'] and plan['archive_candidate']
    assert plan['pending_unit_count']==0 and plan['archive_allowed'] is False and plan['source_moves']==0
    assert args[-1].call_count==2 and args[-2].call_count==14


@pytest.mark.parametrize('n',[1,3,4,10,14])
def test_one_pending_page_always_prevents_parent_move(n):
    args=context();args[2].pop(str(n));plan=run(args)
    assert not plan['all_units_terminal'] and not plan['archive_candidate'] and plan['pending_unit_count']==1


@pytest.mark.parametrize('status',['grouping_required','privacy_blocked','medical_pending','pending','write_unknown'])
def test_unfinished_states_never_count_as_terminal(status):
    args=context();args[2]['4']=replace(args[2]['4'],status=status)
    assert not run(args)['all_units_terminal']


def test_duplicate_or_explicit_skip_needs_durable_intent_verifier():
    args=context();args[2]['3']=replace(args[2]['3'],status='duplicate_confirmed')
    assert run(args)['all_units_terminal']
    args[-2].return_value=False
    with pytest.raises(StateError,match='readback_or_intent'):run(args)


@pytest.mark.parametrize('kind',['normal','medical','payroll','sensitive_unknown'])
def test_non_normal_automatic_provenance_never_counts_as_auto_import(kind):
    args=context();args[1][3]=replace(args[1][3],automatic_classification=kind)
    args[2]['4']=replace(args[2]['4'],status='imported')
    if kind=='normal':assert run(args)['all_units_terminal']
    else:
        with pytest.raises(StateError,match='route_conflict'):run(args)


@pytest.mark.parametrize('change',[dict(source_content_hash='d'*64),dict(member_page_identities=('d'*64,)),
    dict(page_numbers=(5,)),dict(confirmation_digest='invalid')])
def test_wrong_outcome_identity_is_held(change):
    args=context();args[2]['4']=replace(args[2]['4'],**change)
    with pytest.raises(StateError,match='identity_changed'):run(args)


def test_source_change_or_accounting_readback_failure_holds_plan():
    args=context();args[-1].side_effect=[True,False]
    with pytest.raises(StateError,match='source_changed'):run(args)
    args=context();args[-2].return_value=False
    with pytest.raises(StateError,match='readback_or_intent'):run(args)


def test_missing_or_overlapping_partition_and_unknown_unit_are_rejected():
    args=context();args[1].pop()
    with pytest.raises(StateError,match='partition_incomplete'):run(args)
    args=context();args[1][3]=replace(args[1][3],page_numbers=(3,))
    with pytest.raises(StateError,match='partition_invalid'):run(args)
    args=context();args[2]['another']=args[2]['3']
    with pytest.raises(StateError,match='unknown_unit'):run(args)
