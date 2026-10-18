"""Safety-stop evidence, NOT a completed shared-native composition.

All runtime probes stop before the existing writer. No production transport,
authority update, AI call or source move is available. Configured bridge tests
establish only its local invariant, never coverage of the unbound legacy paths.
"""
import ast
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import inspect
from pathlib import Path
from threading import Barrier, Lock
from uuid import uuid4

import pytest

from app.drive_run_state import StateError
from app.receipt_pipeline import ReceiptPipeline
from app.unified_receipt_write import ConditionalRequests, UnifiedWriteBridge
from test_unified_receipt_write import (
    Memory, FakeNative, active, bridge, payload, medical_native, pdf_native,
)

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def prohibit_live_client_construction(monkeypatch):
    from google.cloud import firestore
    from googleapiclient import discovery
    def forbidden(*args, **kwargs):
        pytest.fail('live client forbidden in safety-stop evidence')
    monkeypatch.setattr(firestore, 'Client', forbidden)
    monkeypatch.setattr(discovery, 'build', forbidden)


class BeforeWriter(Exception):
    """Deliberate stop, never an accounting outcome or execution permission."""


def definition(path, name):
    tree = ast.parse((ROOT / path).read_text('utf-8'))
    found = [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name]
    assert len(found) == 1
    return found[0]


def calls(node):
    return {n.func.attr if isinstance(n.func, ast.Attribute) else n.func.id
            for n in ast.walk(node) if isinstance(n, ast.Call)
            and isinstance(n.func, (ast.Name, ast.Attribute))}


def test_every_current_materializer_and_confirmation_sink_call_is_in_the_inventory():
    # Conservative source census, not proof about external/dynamic composition.
    # New direct calls anywhere in app require another explicit inventory review.
    found = set()
    class Census(ast.NodeVisitor):
        def __init__(self, path):
            self.path, self.scope = path, []
        def scoped(self, node):
            self.scope.append(node.name)
            self.generic_visit(node)
            self.scope.pop()
        visit_ClassDef = visit_FunctionDef = visit_AsyncFunctionDef = scoped
        def visit_Call(self, node):
            if isinstance(node.func, ast.Attribute) and node.func.attr in {'_materialize_result', '_write_accounting_plan'}:
                found.add((self.path, '.'.join(self.scope), node.func.attr))
            self.generic_visit(node)
    for path in (ROOT / 'app').rglob('*.py'):
        Census(path.relative_to(ROOT).as_posix()).visit(ast.parse(path.read_text('utf-8')))
    assert found == {
        ('app/receipt_pipeline.py', 'ReceiptPipeline.process_bytes', '_materialize_result'),
        ('app/receipt_confirmation.py', 'ReceiptConfirmation.apply_confirmations', '_write_accounting_plan'),
        ('app/pdf_intake_runner.py', 'plan', '_materialize_result'),
        ('app/pdf_intake_runner.py', 'PageIntake._post', '_materialize_result'),
        ('app/receipt_plan/posting.py', 'execute', '_materialize_result'),
        ('app/receipt_plan/unit_posting.py', 'execute', '_materialize_result'),
        ('app/receipt_plan/validated.py', 'accounting_plan', '_materialize_result'),
        ('app/unified_receipt_native.py', 'GroupedUnitPort._plan', '_materialize_result'),
        ('app/unified_receipt_native.py', 'GroupedUnitPort.execute', '_materialize_result'),
    }


def test_automatic_pipeline_cannot_supply_required_native_binding():
    parameters = set(inspect.signature(ReceiptPipeline.process_bytes).parameters)
    assert parameters == {'self', 'image_bytes', 'mime_type', 'source_id', 'image_url', 'known_source_classification'}
    assert not parameters & {'source_version', 'receipt_unit_id', 'authority_revision', 'request_id', 'expected_state_revision'}
    body = definition('app/receipt_pipeline.py', 'process_bytes')
    assert {'_materialize_result', 'validate_receipt_result'} <= calls(body)
    assert not {'ConditionalRequests', 'submit', 'claim', 'replace_versioned'} & calls(body)
    materializer = definition('app/receipt_pipeline.py', '_materialize_result')
    assert {'append', 'append_raw'} <= calls(materializer)
    # Evidence of an uncovered path; deliberately NOT an assertion of safety.


@pytest.mark.parametrize('targeted', [False, True])
def test_unbound_legacy_receipt_reaches_writer_boundary_without_shared_claim(monkeypatch, targeted):
    c, b, p, request, service, db, store, fake, io = medical_native()
    del service.request_gate  # Models the actual production constructor, not a bypass of a bound service.
    reached = []
    def stop(key, item):
        reached.append(key)
        assert item['status'] == 'pending'
        raise BeforeWriter()
    monkeypatch.setattr(service, '_write_accounting_plan', stop)
    before = deepcopy({k: db.rows[k] for k in ('レシート', '支出明細', '取込データ')})
    with pytest.raises(BeforeWriter):
        service.apply_confirmations(keys=[p.review_id] if targeted else None)
    assert reached == [p.review_id]
    assert b.requests.read()[0]['operations'] == {} and io.writes == 0
    assert before == {k: db.rows[k] for k in before}
    assert fake.ai_calls == fake.moves == fake.authority_updates == 0
    assert not hasattr(service, 'request_gate')


def test_direct_pdf_manual_reaches_native_post_boundary_without_shared_claim(monkeypatch):
    c, b, p, request, db, runner = pdf_native()
    before = deepcopy(db.data), deepcopy(runner.store.load())
    reached = []
    def stop(key, uid, *args, **kw):
        assert kw['apply'] is True and kw['authorized']
        reached.append((key, uid))
        raise BeforeWriter()
    monkeypatch.setattr(runner, '_post', stop)
    with pytest.raises(BeforeWriter):
        runner.post_manual(p.key, p.uid, p.snapshot_reader(), apply=True)
    assert reached == [(p.key, p.uid)]
    assert b.requests.read()[0]['operations'] == {}
    assert (db.data, runner.store.load()) == before and not db.writes


@pytest.mark.parametrize('path,name,required', [
    ('app/drive_receipts.py', 'process_inbox', {'process_bytes'}),
    ('app/receipt_confirmation_production.py', 'execute', {'ReceiptConfirmation', 'apply_confirmations'}),
    ('app/pdf_intake_production.py', 'process', {'post_manual', 'post_normal'}),
    ('app/pdf_intake_runner.py', '_post', {'save', '_materialize_result'}),
    ('app/receipt_plan/posting.py', 'execute', {'replace', '_materialize_result'}),
    ('app/receipt_plan/unit_posting.py', 'execute', {'replace', '_materialize_result'}),
])
def test_inventory_retains_actual_legacy_to_native_edges(path, name, required):
    assert required <= calls(definition(path, name))


class ExecutionProbe(FakeNative):
    """Non-transferable counter only; no native writer or usable authority."""
    def __init__(self, case):
        super().__init__(case)
        self.lock = Lock()
        self.finished = False
    def execute(self, request):
        with self.lock:
            self.executions += 1
            if self.mode == 'partial':
                raise TimeoutError('synthetic probe incomplete')
            self.finished = True
            if self.mode == 'lost':
                raise TimeoutError('synthetic probe response lost')
    def readback(self, request):
        return self.finished


@pytest.mark.parametrize('count', [1, 2, 16])
@pytest.mark.parametrize('same_request_id', [False, True])
def test_configured_instances_share_one_backend_but_do_not_prove_legacy_coverage(count, same_request_id):
    case = active()
    probe, server = ExecutionProbe(case), Memory()
    instances = [bridge(case, probe, io=server)[0] for _ in range(count)]
    assert len({id(b.requests) for b in instances}) == count
    assert all(b.requests.io is server for b in instances)
    rid = str(uuid4())
    requests = [payload(b, case, rid=rid if same_request_id else str(uuid4())) for b in instances]
    start = Barrier(count)
    def submit(i):
        start.wait(timeout=15)
        try:
            return instances[i].submit(case, requests[i], entry='legacy' if i % 2 else 'unified')
        except StateError:
            return None  # CAS contention fails closed; never retry.
    with ThreadPoolExecutor(max_workers=count) as pool:
        results = list(pool.map(submit, range(count)))
    assert probe.executions == 1 and probe.rows == []
    assert len(instances[0].requests.read()[0]['operations']) == 1
    assert any(r and r['state'] == 'complete' for r in results)
    assert probe.ai_calls == probe.moves == probe.authority_updates == 0
    # A restarted wrapper reads the SAME backend, not another fake store.
    restarted = ConditionalRequests(server, instances[0].requests.store_id)
    assert restarted.read()[0] == instances[0].requests.read()[0]


@pytest.mark.parametrize('mode', ['partial', 'lost'])
def test_configured_unknown_never_redispatches_through_another_entry(mode):
    case = active(); probe = ExecutionProbe(case); probe.mode = mode
    b, _, server = bridge(case, probe); request = payload(b, case)
    with pytest.raises(StateError, match='partial_unknown'):
        b.submit(case, request, entry='legacy')
    restarted, _, _ = bridge(case, probe, io=server)
    retry = {**request, 'request_id': str(uuid4())}
    assert restarted.submit(case, retry)['state'] == 'partial_unknown'
    assert probe.executions == 1
    assert restarted.recover_readback(case, request)['state'] == ('complete' if mode == 'lost' else 'partial_unknown')
    assert probe.executions == 1 and probe.rows == []


def test_configured_lost_claim_response_cannot_be_dispatched_by_another_entry():
    case = active(); probe = ExecutionProbe(case)
    b, _, server = bridge(case, probe); request = payload(b, case)
    server.lost = True
    with pytest.raises(TimeoutError):
        b.submit(case, request, entry='legacy')
    server.lost = False
    restarted, _, _ = bridge(case, probe, io=server)
    assert restarted.submit(case, request)['state'] == 'accepted'
    with pytest.raises(StateError, match='recovery_state_held'):
        # Even a positive probe readback cannot fill in a never-dispatched claim.
        probe.finished = True
        restarted.recover_readback(case, request)
    assert probe.executions == 0


@pytest.mark.parametrize('field,value', [('source_version', 'changed'), ('receipt_unit_id', 'other'),
    ('stable_page_identity', 'a'*64), ('current_snapshot_digest', 'b'*64), ('authority_revision', 2)])
def test_configured_binding_mismatch_never_reaches_probe(field, value):
    case = active(); probe = ExecutionProbe(case)
    b, _, server = bridge(case, probe); request = payload(b, case)
    with pytest.raises(StateError):
        b.submit(case, {**request, field: value})
    assert probe.executions == server.writes == 0


def test_default_off_remains_no_client_no_targets():
    case = active()
    assert UnifiedWriteBridge().prepare(case, 'post', str(uuid4()))['operation_targets'] == []
