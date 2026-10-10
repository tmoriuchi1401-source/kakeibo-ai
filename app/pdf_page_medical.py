"""PDF page adapter to the existing COMPLETE MANUAL confirmation/writer.

No Medical extraction, candidate, admission, HMAC, AI or new writer. Parent PDF
freshness and page-kind proofs are supplied by the independent review service.
"""
from copy import deepcopy
from types import SimpleNamespace

from .drive_run_state import StateError
from .receipt_confirmation import ReceiptConfirmation, review_id, medical_categories
from .receipt_pdf_units import DocumentUnit, PageObservation

CATEGORY_LABELS = {'医療費': '医療・保険｜病院', '薬代': '医療・保険｜薬',
                   '医療費（その他）': '医療・保険｜その他'}
MANUAL_MESSAGE = '原本を見て完全手入力。OCR・AI候補は使用しません。'


def category_choices(categories):
    valid = {'｜'.join(c) for c in medical_categories(categories)}
    return {label: value for label, value in CATEGORY_LABELS.items() if value in valid}


def manual_source(page, kind):
    if (not kind or kind['human_classification'] != 'medical' or
            any(kind[k] != page[k] for k in ('source_file_id', 'source_content_hash', 'page_number', 'page_hash'))):
        raise StateError('pdf_medical_kind_required')
    unit = DocumentUnit(PageObservation(**page))
    # Reuse the established PDF page Unit identity and existing review_id.
    # The PNG hash is the review version; original bytes remain its sha256.
    return {'source_id': unit.source_id, 'version': page['page_hash'],
            'sha256': page['source_content_hash'], 'mime_type': 'application/pdf',
            'pdf_page': {'original_file_id': page['source_file_id'], 'page_number': page['page_number'],
                         'page_hash': page['page_hash'], 'page_kind_confirmation_digest': kind['confirmation_digest']}}


def legacy_row(source, values, status='waiting'):
    key = review_id('medical', source)
    p = source['pdf_page']
    link = 'https://drive.google.com/file/d/' + p['original_file_id'] + '/view#page=' + str(p['page_number'])
    return [key, 'medical', status, link, 'PDF p'+str(p['page_number'])+'・本人確認済み医療',
            '完全手入力・原本を保持', MANUAL_MESSAGE, *values, '']


def manual_values(fields, categories):
    category = category_choices(categories).get(str(fields.get('category', '')), '')
    return [fields.get('date', ''), fields.get('facility', ''), fields.get('amount', ''), category,
            fields.get('payment', ''), fields.get('medical_action', ''),
            fields.get('duplicate_target', ''), fields.get('memo', '')]


def validate_manual_values(values,categories):
    context=SimpleNamespace(db=SimpleNamespace(categories=lambda:categories))
    return ReceiptConfirmation._parsed(context,{'kind':'medical','inputs':values})


class PdfManualConfirmation(ReceiptConfirmation):
    def __init__(self, store, db, source, folder, verify_page, read_owner_inputs):
        self.source, self.folder, self.read_owner_inputs = source, folder, read_owner_inputs
        self.key = review_id('medical', source)
        # verify_page MUST re-read original/page identity and durable human kind.
        super().__init__(store, db, verify_page)

    @property
    def items(self):
        all_items = self.store.value.get('confirmation_items', {})
        return {self.key: all_items[self.key]} if self.key in all_items else {}

    def ui_rows(self):
        values = self.read_owner_inputs()  # re-read UI + reconstruct identity from Drive
        if not isinstance(values, list) or len(values) != 8:
            raise StateError('pdf_medical_input_snapshot_invalid')
        return {self.key: (2, legacy_row(self.source, values))}

    def render(self):
        raise StateError('pdf_medical_use_shared_page_ui')  # never create another UI/tab

    def prepare(self):
        self.verify_source(self.source, self.folder)
        from .receipt_reimport_production import target_snapshot
        parent=target_snapshot(self.tables(),self.source['pdf_page']['original_file_id'])
        if any(parent.values()):
            raise StateError('pdf_medical_parent_accounting_requires_review')
        self.observe_medical(self.source, self.folder)
        item = deepcopy(self.items[self.key])
        expected = legacy_row(self.source, [''] * 8)
        presentation = expected[:2] + expected[3:7]
        if item.get('presentation') is None:
            item['presentation'] = presentation
            self.save_item(self.key, item)
        elif item['presentation'] != presentation:
            raise StateError('pdf_medical_review_identity_changed')

    def _write_accounting_plan(self,key,item):
        self.verify_source(item['source'],item['folder_id'])
        if self.ui_rows()[key][1][7:15]!=item['inputs']:
            raise StateError('pdf_medical_owner_inputs_changed')
        return super()._write_accounting_plan(key,item)

    def confirm(self, *, submitted_inputs=None):
        """Explicit owner action only; common duplicate/intent/readback/replay."""
        self.prepare()
        item = self.items[self.key]
        if item['status'] == 'applied':
            if submitted_inputs is not None and submitted_inputs != item['inputs']:
                raise StateError('pdf_medical_owner_inputs_changed')
            if not self._complete(item['plan']):
                raise StateError('confirmation_readback_mismatch')
            return '医療確定済み'
        self.capture_inputs()
        self.apply_confirmations()
        item = self.items[self.key]
        if item['status'] == 'applied' and self._complete(item['plan']):
            return '医療確定済み'
        return item.get('error', '医療入力待ち')
