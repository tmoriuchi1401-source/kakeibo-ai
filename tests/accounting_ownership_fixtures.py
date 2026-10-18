"""Independent synthetic port frames, existing native Unit IDs and identity."""
from copy import deepcopy
from types import SimpleNamespace

from app.iphone_accounting_ownership import TrustedOwnershipReader, native_identity
from app.unified_receipt_review import digest
from test_pdf_intake_authority import OWNER
from test_unified_receipt_write import pdf_native
from app.pdf_intake_runner import ledger_source_id


class Port:
    synthetic_memory_only = True
    def __init__(self, frame):
        self.frame = deepcopy(frame); self.reads = 0; self.on_read = None
    def read(self, *args):
        self.reads += 1
        if self.on_read: self.on_read(self, *args)
        return deepcopy(self.frame)


class NativePort(Port):
    def read(self, entry, key):
        value = super().read(entry, key)
        return value[(entry, key)]


class SourcePort(Port):
    def __init__(self, frame, raw=b'%PDF-FAKE-UNIT-WRITER'):
        super().__init__(frame); self.raw = raw
    def revision_bytes(self, source_id, revision):
        assert (source_id, revision) == (self.frame['source_id'], self.frame['drive_revision_id'])
        return self.raw


class OwnershipFixture:
    def __init__(self):
        self.c, self.bridge, self.port, self.request, self.db, self.runner = pdf_native(count=2)
        page = self.runner.store.load()['pages'][self.port.key]['page']
        self.source_value = dict(source_id=self.c.source_id, sha256=self.c.identity['source_content_hash'],
            drive_revision_id='drive-revision-A', drive_numeric_version='19',
            native_source_version=self.c.identity['source_version'], source_kind='pdf', page_count=1)
        self.source = SourcePort(dict({k:v for k,v in self.source_value.items() if k != 'native_source_version'}, generation=1))
        manifest = self.runner.store.load()['pages'][self.port.key]['units'][self.port.uid]['candidate']['manifest']
        units = [dict(unit_id=u['receipt_unit_id'], receipt_index=u['receipt_index'], members=[dict(
            page=1, identity=page['stable_page_identity'], box=[u['bbox'][k] for k in ('left','top','right','bottom')])])
            for u in manifest['units']]
        self.partition = Port(dict(source=deepcopy(self.source_value), generation=1, revision=1,
                                   state='confirmed', complete=True, units=units))
        native = {}
        for entry in ('normal_auto', 'buyback_income', 'legacy_confirmation', 'pdf_auto', 'pdf_manual',
                      'grouped_unit', 'fixed_canary', 'explicit_unit', 'future_one_tab'):
            for index in (1, 2):
                whole = entry in {'normal_auto', 'buyback_income', 'legacy_confirmation'}
                namespace = ('receipt_confirmation' if whole else 'pdf_unit_processing' if entry == 'grouped_unit'
                    else 'receipt_unit_posting' if entry in {'fixed_canary','explicit_unit'} else 'pdf_intake')
                unit = None if whole else units[index-1]
                record = dict(generation=1, owner=OWNER, source=deepcopy(self.source_value), case_id=self.c.case_id,
                    purpose='ACCOUNTING_POST', action='buyback_income_post' if entry == 'buyback_income' else 'post',
                    namespace=namespace, native_context='synthetic-native-context',
                    native_request_id='11111111-1111-4111-8111-111111111111' if index == 1 else '22222222-2222-4222-8222-222222222222',
                    native_page=deepcopy(page) if namespace == 'pdf_intake' else None,
                    scope='whole' if whole else 'unit', unit_id=None if whole else unit['unit_id'],
                    receipt_index=None if whole else unit['receipt_index'], input_snapshot=digest(['native-input', entry, index]),
                    authority_revision=1, ledger_targets=['R-'+self.c.source_id] if whole else ['R-'+ledger_source_id(unit['unit_id'])], state='ready')
                record['operation_identity'] = native_identity(record, self.source_value, unit)
                native[(entry, 'u'+str(index))] = record
        # These aliases represent exactly the same native snapshot/operation.
        for key in ('u1', 'u2'):
            native['future_one_tab', key] = deepcopy(native['pdf_manual', key])
            native['pdf_auto', key]['input_snapshot'] = native['pdf_manual', key]['input_snapshot']
        self.native = NativePort(native)
        self.ledger = Port(dict(generation=1, complete=True, records=[]))
        self.pending = Port(dict(generation=1, complete=True, records=[]))
        self.reader = TrustedOwnershipReader(self.source, self.partition, self.native, self.ledger, self.pending)

    def observe(self, entry='pdf_manual', key='u1'):
        return self.reader.observe(entry, key)
