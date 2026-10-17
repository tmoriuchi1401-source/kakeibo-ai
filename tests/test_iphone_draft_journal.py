from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import asdict
import json
from threading import Barrier
import uuid

import pytest

from app.drive_run_state import StateError
from app.iphone_draft_journal import LocalJournal, exclusive, quarantine
from app.iphone_sheet_reader import validate_snapshot, regeneration_preview
from test_iphone_sheet_reader import setup


def fixture(tmp_path):
    raw, contract, sources = setup(); batch = validate_snapshot(contract, raw, sources)
    journal = LocalJournal(tmp_path/'drafts.json', batch.contract_digest)
    _, tag = journal.initialize()
    return journal, batch, tag


def test_persistent_reload_cas_replay_and_readonly_recovery(tmp_path):
    journal, batch, tag = fixture(tmp_path); rid = str(uuid.uuid4())
    saved = journal.save(batch, tag, rid)
    reloaded = LocalJournal(journal.path, batch.contract_digest)
    state, next_tag = reloaded.read_versioned()
    assert state['revision'] == 1 and next_tag != tag
    assert state['batch'] == asdict(batch)
    assert reloaded.save(batch, tag, rid)['replay'] is True
    before = journal.path.read_bytes()
    assert reloaded.recover(rid, batch, tag)['is_current'] is True
    assert journal.path.read_bytes() == before
    assert not saved['authorized'] and not saved['operation_targets']
    assert regeneration_preview(batch, state['batch'])['drafts'] == batch.drafts


@pytest.mark.parametrize('attack', ['stale','weak','missing','replay_payload','request_id','contract','authorized','digest'])
def test_journal_rejects_stale_foreign_or_ambiguous_inputs_without_rewrite(tmp_path, attack):
    journal, batch, tag = fixture(tmp_path); rid = str(uuid.uuid4()); journal.save(batch, tag, rid)
    before = journal.path.read_bytes(); other = deepcopy(batch)
    current = journal.read_versioned()[1]
    if attack == 'stale': args = (batch, tag, str(uuid.uuid4()))
    elif attack == 'weak': args = (batch, 'W/'+current, str(uuid.uuid4()))
    elif attack == 'missing': args = (batch, None, str(uuid.uuid4()))
    elif attack == 'replay_payload': args = (batch, current, rid)
    elif attack == 'request_id': args = (batch, current, 'not-an-id')
    else:
        draft = next(iter(other.drafts.values()))
        if attack == 'contract': draft['identity']['source_version'] = 'new'
        elif attack == 'authorized': draft['authorized'] = True
        else: draft['input_snapshot_digest'] = 'a'*64
        args = (other, current, str(uuid.uuid4()))
    with pytest.raises(StateError): journal.save(*args)
    assert journal.path.read_bytes() == before


def test_two_concurrent_writers_with_same_cas_commit_only_one(tmp_path):
    journal, batch, tag = fixture(tmp_path); barrier = Barrier(2)
    def write(_):
        barrier.wait()
        try:
            journal.save(batch, tag, str(uuid.uuid4())); return 'saved'
        except StateError as error:
            assert str(error) in {'iphone_journal_busy','iphone_journal_cas_conflict'}
            return 'stopped'
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(write, range(2))) == ['saved','stopped']
    assert journal.read_versioned()[0]['revision'] == 1


@pytest.mark.parametrize('when', ['before_replace','after_replace'])
def test_disconnection_unknown_outcome_requires_readonly_lookup(tmp_path, monkeypatch, when):
    journal, batch, tag = fixture(tmp_path); rid = str(uuid.uuid4()); original = journal._replace
    def interrupted(value):
        if when == 'after_replace': original(value)
        raise StateError('simulated_response_lost')
    monkeypatch.setattr(journal, '_replace', interrupted)
    with pytest.raises(StateError): journal.save(batch, tag, rid)
    before = journal.path.read_bytes()
    if when == 'after_replace':
        assert journal.recover(rid, batch, tag)['status'] == 'draft_save_observed'
    else:
        with pytest.raises(StateError, match='recovery_unresolved'): journal.recover(rid, batch, tag)
    assert journal.path.read_bytes() == before


def test_os_lock_is_not_stolen_or_deleted_and_released_lockfile_is_reusable(tmp_path):
    journal, batch, tag = fixture(tmp_path)
    with exclusive(journal.lock_path):
        with pytest.raises(StateError, match='busy'): journal.save(batch, tag, str(uuid.uuid4()))
    assert journal.lock_path.exists()
    assert journal.save(batch, tag, str(uuid.uuid4()))['status'] == 'draft_saved'


@pytest.mark.parametrize('corruption', ['json','contract','revision','schema','batch'])
def test_corruption_never_initializes_or_repairs_existing_journal(tmp_path, corruption):
    journal, batch, tag = fixture(tmp_path)
    state, _ = journal.read_versioned()
    if corruption == 'json': journal.path.write_text('{interrupted', encoding='utf-8')
    else:
        if corruption == 'contract': state['contract_digest'] = 'foreign'
        elif corruption == 'revision': state['revision'] = 5
        elif corruption == 'schema': state['schema'] = 'old'
        else: state['batch'] = {'foreign':'batch'}
        journal.path.write_text(json.dumps(state), encoding='utf-8')
    before = journal.path.read_bytes()
    with pytest.raises(StateError): journal.read_versioned()
    with pytest.raises(StateError): journal.initialize()
    assert journal.path.read_bytes() == before


def test_quarantine_preserves_untrusted_snapshot_without_adopting_journal(tmp_path):
    journal, batch, tag = fixture(tmp_path); before = journal.path.read_bytes()
    raw, _, _ = setup(); raw['sheets'][0]['data'][0]['rowData'].reverse()
    path = quarantine(tmp_path, raw, 'case_order_or_identity_changed')
    record = json.loads(path.read_text())
    assert record['snapshot'] == raw and record['adopted'] is False
    assert journal.path.read_bytes() == before
