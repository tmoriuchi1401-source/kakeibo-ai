from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
from threading import Barrier
import base64
import json
from pathlib import Path
import subprocess
import sys
import uuid

import pytest

from app.drive_run_state import StateError
from app.iphone_persistent_drafts import PersistentJournal
from app.unified_receipt_review import digest
from iphone_persistent_fixtures import provision, repository


def edited(batch, note='未送信・途中入力'):
    drafts = deepcopy(batch.drafts)
    draft = next(d for d in drafts.values() if d['case_id'] == 'SYN-M102')
    draft['cells'][12]['userEnteredValue'] = {'stringValue':note}
    draft['input_snapshot_digest'] = digest(draft['cells'])
    return replace(batch, drafts=drafts, snapshot_digest=digest(drafts))


def test_migrate_encrypted_history_and_instance_reopen(tmp_path):
    r, doc, key, _, batch, local = provision(tmp_path)
    old = local.path.read_bytes(); _, tag = r.read_versioned()
    changed = edited(batch); rid = str(uuid.uuid4())
    result = r.save(changed, tag, rid)
    second = repository(doc, key, batch)
    assert second.recover(rid, changed, tag)['is_current']
    state, _, _ = second._state()
    assert state['history'][0]['batch'] == r._validate(json.loads(old))['batch']
    assert local.path.read_bytes() == old
    assert result['authorized'] is False and result['operation_targets'] == [] and doc.writes == 1
    assert b'SYN-M102' not in doc.payload and '未送信'.encode() not in doc.payload


@pytest.mark.parametrize('instances', [2, 8, 16])
def test_atomic_shared_backend_only_one_winner(tmp_path, instances):
    r, doc, key, _, batch, _ = provision(tmp_path); _, tag = r.read_versioned()
    barrier = Barrier(instances)
    def save(n):
        other = repository(doc, key, batch)
        barrier.wait()
        try:
            return other.save(edited(batch, str(n)), tag, str(uuid.uuid4()))
        except StateError:
            return None
    with ThreadPoolExecutor(instances) as pool:
        results = list(pool.map(save, range(instances)))
    assert sum(x is not None for x in results) == doc.writes == 1
    assert r.read_versioned()[0]['revision'] == 2


def test_same_request_concurrent_and_changed_payload_refused(tmp_path):
    r, doc, key, _, batch, _ = provision(tmp_path); _, tag = r.read_versioned()
    rid = str(uuid.uuid4()); changed = edited(batch)
    def send(_):
        try:
            return repository(doc, key, batch).save(changed, tag, rid)
        except StateError:
            return None  # a racing replay may need explicit read-back
    with ThreadPoolExecutor(8) as pool:
        list(pool.map(send, range(8)))
    assert r.recover(rid, changed, tag)['is_current'] and doc.writes == 1
    with pytest.raises(StateError, match='replay_payload_changed'):
        r.save(edited(batch, '別内容'), tag, rid)
    assert doc.writes == 1


@pytest.mark.parametrize('fault', ['before', 'after', 'partial', 'read'])
def test_unknown_partial_or_disconnected_never_retries_or_loses_client_draft(tmp_path, fault):
    r, doc, key, _, batch, _ = provision(tmp_path); _, tag = r.read_versioned()
    changed = edited(batch); copy = deepcopy(changed); rid = str(uuid.uuid4())
    doc.fault = fault
    with pytest.raises(StateError):
        r.save(changed, tag, rid)
    assert changed == copy and doc.writes <= 1
    doc.fault = None
    restarted = repository(doc, key, batch)
    if fault == 'after':
        assert restarted.recover(rid, changed, tag)['is_current']
    else:
        with pytest.raises(StateError):
            restarted.recover(rid, changed, tag)
    assert doc.writes == (1 if fault in {'after', 'partial'} else 0)
    assert restarted.recovery_export()[0] == doc.payload


@pytest.mark.parametrize('attack', ['owner', 'generation', 'key', 'corrupt', 'health', 'history'])
def test_foreign_or_corrupt_state_stays_quarantined_after_reopen(tmp_path, attack):
    from cryptography.fernet import Fernet
    r, doc, key, _, batch, _ = provision(tmp_path)
    if attack in {'health', 'history'}:
        value, _, _ = r._state()
        if attack == 'health': value['health'] = 'quarantined'
        else: value['migration']['legacy_digest'] = 'f'*64
        doc.payload = r.transport.encode(value)
    if attack == 'corrupt': doc.payload = b'corrupt'
    foreign = repository(doc, Fernet.generate_key() if attack == 'key' else key, batch,
        owner='f'*64 if attack == 'owner' else None, generation=2 if attack == 'generation' else 1)
    for _ in range(2):
        with pytest.raises(StateError): foreign.read_versioned()
    assert doc.writes == 0


@pytest.mark.parametrize('tag', ['*', 'W/"weak"', '', '"stale"'])
def test_stale_and_non_strong_cas_refused(tmp_path, tag):
    r, doc, _, _, batch, _ = provision(tmp_path)
    with pytest.raises(StateError): r.save(edited(batch), tag, str(uuid.uuid4()))
    assert doc.writes == 0


def test_old_receipt_is_not_reported_as_current_and_request_ids_retained(tmp_path):
    r, doc, _, _, batch, _ = provision(tmp_path); _, tag = r.read_versioned()
    first = edited(batch, 'first'); rid = str(uuid.uuid4())
    r.save(first, tag, rid)
    _, second_tag = r.read_versioned()
    r.save(edited(batch, 'second'), second_tag, str(uuid.uuid4()))
    assert not r.recover(rid, first, tag)['is_current']
    with pytest.raises(StateError): r.recover(rid, edited(batch, 'changed'), tag)
    assert doc.writes == 2


def test_actual_process_restart_reconstructs_adapter_from_saved_fake_backend(tmp_path):
    r, doc, key, _, batch, _ = provision(tmp_path)
    _, tag = r.read_versioned(); changed = edited(batch); rid = str(uuid.uuid4())
    r.save(changed, tag, rid)
    fixture = tmp_path/'synthetic-restart.json'
    fixture.write_text(json.dumps(dict(payload=base64.b64encode(doc.payload).decode(), key=key.decode(),
                                      scope=r.scope, revision=doc.version)), encoding='utf8')
    code = '''import base64,json,sys
from app.iphone_persistent_drafts import PersistentJournal
from types import SimpleNamespace
v=json.load(open(sys.argv[1])); s=v['scope']
io=SimpleNamespace(read_versioned=lambda:(base64.b64decode(v['payload']), '"persisted-fake-version"'))
r=PersistentJournal(io,v['key'].encode(),owner=s['owner'],contract_digest=s['contract'],generation=s['generation'],initial_snapshot=s['initial_snapshot'])
state,tag=r.read_versioned()
assert state['revision']==2 and len(state['requests'])==2
print('process-restart-readback-ok')
'''
    result = subprocess.run([sys.executable, '-c', code, str(fixture)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == 'process-restart-readback-ok'


def test_capacity_is_hold_and_never_prunes_inputs(tmp_path, monkeypatch):
    r, doc, _, _, batch, _ = provision(tmp_path); before = doc.payload; _, tag = r.read_versioned()
    monkeypatch.setattr('app.iphone_persistent_drafts.LIMIT', len(before))
    with pytest.raises(StateError, match='capacity'): r.save(edited(batch), tag, str(uuid.uuid4()))
    assert doc.payload == before and doc.writes == 0


def test_item_partial_draft_and_observation_are_in_same_atomic_cas(tmp_path):
    r, doc, key, _, batch, _ = provision(tmp_path)
    details, tag = r.read_details(); original = deepcopy(details)
    details['SYN-G101']['item_draft']['entries'][0].update(state='held', note='明細だけ途中保存')
    rid = str(uuid.uuid4())
    r.save(batch, tag, rid, details=details)
    other = repository(doc, key, batch)
    assert other.recover(rid, batch, tag, details=details)['is_current']
    assert other.read_details()[0] == details
    assert other._state()[0]['detail_history'][0] == original
    with pytest.raises(StateError, match='replay_payload_changed'):
        other.save(batch, tag, rid, details=original)
    assert doc.writes == 1


def test_migrated_legacy_request_retains_exact_idempotency(tmp_path):
    r, doc, _, _, batch, local = provision(tmp_path)
    old, _ = local.read_versioned(); rid, receipt = next(iter(old['requests'].items()))
    # Existing initialize() returns the pre-save v1 tag, not the post-save tag.
    empty = dict(schema=old['schema'], contract_digest=old['contract_digest'], revision=0, batch=None, requests={})
    old_tag = '"'+digest(empty)+'"'
    assert r.recover(rid, batch, old_tag)['is_current']
    assert r.save(batch, old_tag, rid)['is_current'] and doc.writes == 0
    with pytest.raises(StateError): r.save(edited(batch), old_tag, rid)


def test_explicit_private_export_then_reviewed_new_generation_restore(tmp_path):
    r, doc, key, _, batch, _ = provision(tmp_path)
    state, _, tag = r._state(); private_export = r.recovery_export()
    doc.payload = b'damaged'
    with pytest.raises(StateError): r.read_versioned()
    from iphone_persistent_fixtures import AtomicDocument
    restored_doc = AtomicDocument()
    restored = repository(restored_doc, key, batch, generation=2)
    restored_doc.payload = restored.migration_payload(state['legacy'], tag, details=state['details'])
    assert restored.read_versioned()[0] == state['legacy']
    assert restored.read_details()[0] == state['details']
    assert doc.payload == b'damaged' and private_export[0] != doc.payload
    assert doc.writes == restored_doc.writes == 0  # operator-only fake provisioning


def test_stale_source_identity_requires_new_reviewed_generation(tmp_path):
    r, doc, _, _, batch, _ = provision(tmp_path); _, tag = r.read_versioned()
    drafts = deepcopy(batch.drafts)
    key = next(k for k,d in drafts.items() if d['case_id'] == 'SYN-G101')
    draft = drafts.pop(key); draft['identity']['source_version'] = 'changed-original-revision'
    key = digest(dict(case_id=draft['case_id'], kind=draft['kind'], identity=draft['identity']))
    drafts[key] = draft
    changed = replace(batch, drafts=drafts, snapshot_digest=digest(drafts))
    with pytest.raises(StateError, match='snapshot_identity_changed'):
        r.save(changed, tag, str(uuid.uuid4()))
    assert doc.writes == 0


@pytest.mark.parametrize('attack', ['corrupt_legacy', 'weak_tag', 'wrong_snapshot'])
def test_migration_never_adopts_damaged_or_unpinned_legacy(tmp_path, attack):
    r, doc, _, _, batch, local = provision(tmp_path); before = doc.payload
    state, tag = local.read_versioned()
    if attack == 'corrupt_legacy': state['revision'] += 1
    elif attack == 'weak_tag': tag = '*'
    else: state['batch']['snapshot_digest'] = 'f'*64
    with pytest.raises(StateError): r.migration_payload(state, tag)
    assert doc.payload == before and doc.writes == 0
