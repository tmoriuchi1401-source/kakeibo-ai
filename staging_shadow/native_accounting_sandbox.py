"""Private native-module composition, default OFF, no production registration.

Intercepts accounting methods BEFORE their bodies. Trusted read-only native
preflights run through the shared gate; only an inert execution probe follows.
Actual native execute/authority/writer connection belongs to the next phase.
Production constructors/runners are unchanged and denied in this composition.
"""
import ast
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass
from functools import lru_cache
import importlib
import inspect
from pathlib import Path
import sys
from types import ModuleType
from uuid import uuid4

from app.drive_run_state import StateError
from app.iphone_accounting_gate import ENTRIES, SyntheticAccountingGate
from app.unified_receipt_review import digest

PATHS = {
    'normal_auto': 'receipt_pipeline.ReceiptPipeline.process_bytes',
    'buyback_income': 'receipt_pipeline.ReceiptPipeline.process_bytes',
    'legacy_confirmation': 'receipt_confirmation.ReceiptConfirmation.apply_confirmations',
    'pdf_auto': 'pdf_intake_runner.PageIntake.post_normal',
    'pdf_manual': 'pdf_intake_runner.PageIntake.post_manual',
    'grouped_unit': 'unified_receipt_native.GroupedUnitPort.execute',
    'fixed_canary': 'receipt_plan.posting.execute',
    'explicit_unit': 'receipt_plan.unit_posting.execute',
    'future_one_tab': 'future_accounting.request',
}
DENIED = (
    'receipt_reimport_production.ReimportStore.isolated_v2',
    'receipt_confirmation_contract.IsolatedConfirmationContract.accept_fixture',
    'receipt_confirmation_contract.IsolatedConfirmationContract.consume',
    'receipt_confirmation_contract.IsolatedConfirmationContract.start',
    'receipt_confirmation_contract.IsolatedConfirmationContract.complete_fixture',
    'receipt_confirmation_contract.IsolatedReimportStore._commit',
    'receipt_pipeline.ReceiptPipeline._materialize_result',
    'receipt_confirmation.ReceiptConfirmation._write_accounting_plan',
    'pdf_intake_runner.PageIntake._post',
    'pdf_intake_runner.PageIntake.recover',
    'pdf_intake_runner.ScopedWriter.append',
    'receipt_plan.posting.ScopedMaterializerDB.append',
    'unified_receipt_native.ReceiptConfirmationPort.execute',
    'unified_receipt_native.PDFManualUnitPort.execute',
    'unified_receipt_native.ExplicitUnitPostingPort.execute',
    'unified_receipt_write.UnifiedWriteBridge.submit',
    'sheets.SheetsDB.__init__', 'sheets.SheetsDB.append', 'sheets.SheetsDB.append_raw', 'sheets.SheetsDB.update_row_raw',
    'cli.main', 'cli.make', 'cli.make_receipt_pipeline',
    'drive_receipts.process_inbox', 'receipt_confirmation_production.execute',
    'pdf_intake_production.process', 'pdf_intake_production.archive_terminal',
    'receipt_plan.posting_live.main', 'receipt_plan.unit_posting_live.main',
)
DISABLED_RUNNERS = ('cli', 'drive_receipts', 'production_flow', 'production_run',
                    'receipt_confirmation_production', 'pdf_intake_production',
                    'receipt_plan.posting_live', 'receipt_plan.unit_posting_live')
SINK_CALLERS = {
    ('receipt_pipeline.py', 'ReceiptPipeline.process_bytes'),
    ('receipt_confirmation.py', 'ReceiptConfirmation.apply_confirmations'),
    ('pdf_intake_runner.py', 'plan'), ('pdf_intake_runner.py', 'PageIntake._post'),
    ('receipt_plan/posting.py', 'execute'), ('receipt_plan/unit_posting.py', 'execute'),
    ('receipt_plan/validated.py', 'accounting_plan'),
    ('unified_receipt_native.py', 'GroupedUnitPort._plan'),
    ('unified_receipt_native.py', 'GroupedUnitPort.execute'),
}
ISOLATED_SINK_CALLERS = {
    ('synthetic_auto_writer.py', 'existing_plan'),
    ('synthetic_auto_writer.py', 'SyntheticReceiptWriter.execute'),
    ('synthetic_legacy_writer.py', '_GuardedConfirmation._delegate'),
}


def verify_isolated_sink_census(root):
    """Review the synthetic delegates as well as all existing app call sites."""
    # Independent adapter instances share CODE, not execution authority. Cache
    # the AST walk only while the complete source inventory/stat tuple agrees.
    paths = sorted(list((root / 'app').rglob('*.py')) + list((root / 'staging_shadow').rglob('*.py')))
    signature = tuple((str(p), p.stat().st_size, p.stat().st_mtime_ns) for p in paths)
    _verify_isolated_sink_census(root, signature)


@lru_cache(maxsize=4)
def _verify_isolated_sink_census(root, signature):
    verify_sink_census(root / 'app')
    found = set()
    class Census(ast.NodeVisitor):
        def __init__(self, path): self.path, self.scope = path, []
        def scoped(self, node):
            self.scope.append(node.name); self.generic_visit(node); self.scope.pop()
        visit_ClassDef = visit_FunctionDef = visit_AsyncFunctionDef = scoped
        def visit_Call(self, node):
            if isinstance(node.func, ast.Attribute) and node.func.attr in {'_materialize_result', '_write_accounting_plan'}:
                found.add((self.path, '.'.join(self.scope)))
            self.generic_visit(node)
    for path in (root / 'staging_shadow').rglob('*.py'):
        Census(path.relative_to(root / 'staging_shadow').as_posix()).visit(ast.parse(path.read_text('utf-8')))
    if found != ISOLATED_SINK_CALLERS:
        raise StateError('synthetic_native_unreviewed_isolated_sink')


def verify_sink_census(root):
    """An added direct native sink call requires review, not silent activation."""
    found = set()
    class Census(ast.NodeVisitor):
        def __init__(self, path): self.path, self.scope = path, []
        def scoped(self, node):
            self.scope.append(node.name); self.generic_visit(node); self.scope.pop()
        visit_ClassDef = visit_FunctionDef = visit_AsyncFunctionDef = scoped
        def visit_Call(self, node):
            if isinstance(node.func, ast.Attribute) and node.func.attr in {'_materialize_result', '_write_accounting_plan'}:
                found.add((self.path, '.'.join(self.scope)))
            self.generic_visit(node)
    for path in root.rglob('*.py'):
        Census(path.relative_to(root).as_posix()).visit(ast.parse(path.read_text('utf-8')))
    if found != SINK_CALLERS:
        raise StateError('synthetic_native_unreviewed_sink')


def argument_pin(value):
    if value is None or type(value) in (str, int, float, bool):
        return value
    if type(value) in (list, tuple):
        return [argument_pin(x) for x in value]
    if type(value) is dict:
        if any(type(k) is not str for k in value):
            raise StateError('synthetic_native_argument_keys_invalid')
        return {k: argument_pin(v) for k, v in value.items()}
    if hasattr(value, 'model_dump'):
        return value.model_dump()
    # Trusted handles are in-process only, never persisted as operation identity.
    return {'trusted_handle': id(value)}


@dataclass(frozen=True)
class NativeInvocation:
    entry: str
    key: str
    args: tuple
    kwargs: dict


@dataclass(frozen=True)
class _InertHandle:
    """A call-binding placeholder, with NO underlying service/writer reference."""
    label: str
    nonce: str


def inert_arguments(value):
    if value is None or type(value) in (str, int, float, bool):
        return value
    if type(value) in (list, tuple):
        return type(value)(inert_arguments(x) for x in value)
    if type(value) is dict:
        if any(type(k) is not str for k in value):
            raise StateError('synthetic_native_argument_keys_invalid')
        return {k: inert_arguments(v) for k,v in value.items()}
    if hasattr(value, 'model_dump'):
        return deepcopy(value.model_dump())
    return _InertHandle(type(value).__name__, uuid4().hex)


class NativeAccountingSandbox:
    def __init__(self, gate, invocations, *, enabled=False, isolated_synthetic=False):
        if (enabled is not True or isolated_synthetic is not True
                or type(gate) is not SyntheticAccountingGate):
            raise StateError('synthetic_native_composition_disabled')
        if (set(i.entry for i in invocations) != ENTRIES
                or len({(i.entry, i.key) for i in invocations}) != len(invocations)):
            raise StateError('synthetic_native_incomplete_entry_inventory')
        self.gate = gate
        self.closed = False
        # Financial front doors never receive the native handles used by the
        # trusted read-only preflight adapter. Otherwise an instance method on
        # such a handle would be a second, unguarded entry point.
        inert = [NativeInvocation(i.entry,i.key,inert_arguments(i.args),inert_arguments(i.kwargs)) for i in invocations]
        self.bindings = {(i.entry, i.key): (i, digest(argument_pin([i.args, i.kwargs]))) for i in inert}
        self._command = ContextVar('isolated_native_command', default=None)
        self._root = Path(__file__).resolve().parents[1] / 'app'
        verify_sink_census(self._root)
        self.package = '_kakeibo_accounting_sandbox_' + uuid4().hex
        package = ModuleType(self.package); package.__path__ = [str(self._root)]
        sys.modules[self.package] = package
        # Only the error type is shared. Native modules/classes have private
        # globals; no monkeypatch of the production app package takes place.
        sys.modules[self.package + '.drive_run_state'] = importlib.import_module('app.drive_run_state')
        future = ModuleType(self.package + '.future_accounting')
        future.request = self._interceptor('future_accounting.request')
        sys.modules[future.__name__] = future
        # Load all modules first so imported aliases refer to the same private
        # class objects; then interpose front doors and hard-deny writer bodies.
        for path in set(PATHS.values()) | set(DENIED):
            self._resolve(path)
        for path in set(PATHS.values()):
            parent, name = self._resolve(path); setattr(parent, name, self._interceptor(path))
        for path in DENIED:
            parent, name = self._resolve(path); setattr(parent, name, self._deny)
        for module_name in DISABLED_RUNNERS:
            module = self.module(module_name)
            for name, value in list(vars(module).items()):
                if ((inspect.isfunction(value) or inspect.isclass(value))
                        and getattr(value, '__module__', '').startswith(self.package)):
                    setattr(module, name, self._deny)
        # No private runtime can construct Google clients through its helpers.
        clients = self.module('google_clients')
        for name in ('credentials', 'drive_service', 'sheets_service', 'read_only_drive_service',
                     'read_only_sheets_service', 'download_drive_file'):
            if hasattr(clients, name): setattr(clients, name, self._deny)

    def module(self, name):
        return importlib.import_module(self.package + '.' + name)

    def _resolve(self, path):
        parts = path.split('.')
        # The only nested Python package here is receipt_plan.
        length = 2 if parts[0] == 'receipt_plan' else 1
        parent = self.module('.'.join(parts[:length]))
        for part in parts[length:-1]: parent = getattr(parent, part)
        if not hasattr(parent, parts[-1]):
            raise StateError('synthetic_native_inventory_changed')
        return parent, parts[-1]

    @staticmethod
    def _deny(*args, **kwargs):
        raise StateError('synthetic_native_direct_path_disabled')

    def _interceptor(self, path):
        # Intentionally no __wrapped__/saved executable writer reference.
        def guarded(*args, **kwargs):
            if self.closed or type(self.gate) is not SyntheticAccountingGate:
                raise StateError('synthetic_shared_gate_required')
            command = self._command.get()
            if command is None:
                raise StateError('synthetic_shared_gate_required')
            invocation, pin, rid, revision, context = command
            if (PATHS[invocation.entry] != path or digest(argument_pin([args, kwargs])) != pin):
                raise StateError('synthetic_native_invocation_changed')
            self._command.set(None)  # No nested invocation can reuse this command.
            return self.gate.submit(invocation.entry, invocation.key, rid, revision, context)
        return guarded

    @contextmanager
    def command(self, entry, key, request_id, expected_revision, context):
        if self._command.get() is not None or (entry, key) not in self.bindings:
            raise StateError('synthetic_native_command_invalid')
        invocation, pin = self.bindings[(entry, key)]
        token = self._command.set((invocation, pin, request_id, expected_revision, context))
        try: yield
        finally: self._command.reset(token)

    def call(self, entry, key, request_id, expected_revision, context):
        if self.closed:
            raise StateError('synthetic_native_composition_disabled')
        with self.command(entry, key, request_id, expected_revision, context):
            invocation, _ = self.bindings[(entry, key)]
            parent, name = self._resolve(PATHS[entry])
            return getattr(parent, name)(*invocation.args, **invocation.kwargs)

    def close(self):
        self.closed = True
        for name in list(sys.modules):
            if name == self.package or name.startswith(self.package + '.'):
                sys.modules.pop(name, None)
