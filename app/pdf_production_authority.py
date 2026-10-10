"""Fresh Drive grouping/page-kind authority projected to stable Unit identities.

No local grouping store, UI, AI, accounting or Medical backend exists here.
The original PDF hash/page structure is freshness authority; saved PNG hashes
link historical confirmation only, never current renderer output. New valid v1
confirmations can use the same stable identity projection without fabricating a
page-kind answer for pages already classified normal.
"""
from contextlib import closing
from copy import deepcopy
from dataclasses import dataclass
from hashlib import sha256

import pypdfium2 as pdfium

from .drive_run_state import StateError
from .pdf_grouping_authority import DriveGroupingStore, AUTHORITY_FLAGS
from .pdf_grouping_authority_v2 import DriveGroupingV2Store, FLAGS
from .pdf_page_kind import current_answer
from .pdf_unit_processing import unit_spec, medical_page_spec
from .receipt_pdf_grouping import privacy_for
from .receipt_pdf_units import _digest, MAX_SOURCE_BYTES
from .pdf_page_identity import page_identity


@dataclass(frozen=True)
class AuthoritySnapshot:
    source_file_id: str
    source_content_hash: str
    page_count: int
    status: str
    units: tuple
    medical_pages: tuple
    automatic_classifications: tuple


def _project_legacy(value, record):
    """Identity-only projection of a validated, human-confirmed Drive proposal."""
    p, old = record['proposal'], record['confirmation']
    pages = []
    for page in p['pages']:
        answer = current_answer(value, page)
        automatic = page['classification']
        # An absent human page-kind answer does not authorize sensitive pages.
        # Normal automatic observation + human grouping is already reviewed.
        human = answer['human_classification'] if answer else automatic
        if answer:
            kind = {**FLAGS, 'authority_scope': ['page_kind_only'],
                'source_file_id': p['source_file_id'], 'source_content_hash': p['source_content_hash'],
                'page_number': page['page_number'], 'page_count': p['page_count'],
                'page_identity': page_identity(p['source_content_hash'], page['page_number'], p['page_count']),
                'automatic_classification': automatic, 'human_classification': human,
                'confirmed_at': answer['confirmed_at']}
            kind_digest = _digest(kind)
        else:
            kind_digest = ''
        pages.append({'page_number': page['page_number'],
            'page_identity': page_identity(p['source_content_hash'], page['page_number'], p['page_count']),
            'automatic_classification': automatic, 'human_classification': human,
            'human_confirmation_digest': kind_digest})
    groups = [{'page_numbers': g['page_numbers'],
        'member_page_identities': [pages[n-1]['page_identity'] for n in g['page_numbers']],
        'page_classifications': [privacy_for([pages[n-1]['automatic_classification'],
            pages[n-1]['human_classification']]) for n in g['page_numbers']]} for g in p['groups']]
    projection = {'source_file_id': p['source_file_id'], 'source_content_hash': p['source_content_hash'],
        'page_count': p['page_count'], 'grouping_revision': record['revision'], 'groups': groups, 'pages': pages}
    proposal_digest = _digest(['pdf-grouping-proposal-v2', projection])
    confirmation = {**FLAGS, 'source_file_id': p['source_file_id'],
        'source_content_hash': p['source_content_hash'], 'page_count': p['page_count'],
        'grouping_revision': record['revision'], 'proposal_digest': proposal_digest,
        'confirmed_partition': old['confirmed_partition'], 'confirmed_at': old['confirmed_at']}
    confirmation_digest = _digest(['pdf-grouping-confirmation-v2', confirmation])
    return tuple(unit_spec(p['source_file_id'], p['source_content_hash'], p['page_count'], g['page_numbers'],
        [pages[n-1]['automatic_classification'] for n in g['page_numbers']],
        [pages[n-1]['human_classification'] for n in g['page_numbers']],
        revision=record['revision'], proposal_digest=proposal_digest,
        confirmation_digest=confirmation_digest,
        kind_digests=[pages[n-1]['human_confirmation_digest'] for n in g['page_numbers']]) for g in groups)


class DrivePdfAuthority:
    """Mandatory fresh Drive loads; no repair, new confirmation or state writes."""
    def __init__(self, legacy, load_source, *, migrated=None):
        from .conditional_drive_state_v2 import ConditionalDriveStateTransportV2
        if (not isinstance(legacy, DriveGroupingStore) or not callable(load_source)
                or not isinstance(legacy.transport,ConditionalDriveStateTransportV2)
                or migrated is not None and (not isinstance(migrated, DriveGroupingV2Store)
                    or migrated.legacy_store is not legacy or
                    not isinstance(migrated.transport,ConditionalDriveStateTransportV2))):
            raise StateError('pdf_production_durable_authority_required')
        self.legacy, self.load_source, self.migrated = legacy, load_source, migrated

    def _content(self, source_id):
        content = self.load_source(source_id)
        if not isinstance(content, bytes) or not content or len(content) > MAX_SOURCE_BYTES:
            raise StateError('pdf_production_source_invalid')
        try:
            with closing(pdfium.PdfDocument(content)) as document:
                count = len(document)
                if not 1 <= count <= 50: raise ValueError()
        except Exception: raise StateError('pdf_production_source_invalid') from None
        return content, sha256(content).hexdigest(), count

    def current(self, source_id):
        value = self.legacy.load()
        content, content_hash, count = self._content(source_id)
        record = value['records'].get(_digest(source_id))
        if not record or not record['proposal']:
            return AuthoritySnapshot(source_id, content_hash, count, 'grouping_required', (), (), ())
        p = record['proposal']
        if p['source_content_hash'] != content_hash or p['page_count'] != count:
            raise StateError('pdf_production_source_changed')
        medical = []
        for page in p['pages']:
            answer = current_answer(value, page)
            if answer and answer['human_classification'] == 'medical':
                # Independent of general grouping; preserve the existing legacy
                # manual review identity, linked by its original kind digest.
                medical.append(medical_page_spec(source_id, content_hash, count, page['page_number'],
                    page['classification'], answer['confirmation_digest']))
        units = ()
        if record['status'] == 'grouping_confirmed' and record['confirmation']:
            units = _project_legacy(value, record)
            if self.migrated is not None:
                migrated = self.migrated.load_for(source_id)
                old = migrated['records'].get(_digest(source_id))
                if old:
                    q, a = old['proposal'], old['confirmation']
                    exact = tuple(unit_spec(source_id, q['source_content_hash'], q['page_count'], g['page_numbers'],
                        [q['pages'][n-1]['automatic_classification'] for n in g['page_numbers']],
                        [q['pages'][n-1]['human_classification'] for n in g['page_numbers']],
                        revision=old['revision'], proposal_digest=q['proposal_digest'],
                        confirmation_digest=a['confirmation_digest'],
                        kind_digests=[q['pages'][n-1]['human_confirmation_digest'] for n in g['page_numbers']]) for g in q['groups'])
                    if units != exact: raise StateError('pdf_production_migration_intent_changed')
                    units = exact
        # Re-read source-specific authority across download; unrelated valid
        # proposals cannot revoke this scope. Missing/replaced target intent can.
        latest = self.legacy.load()
        if (latest['records'].get(_digest(source_id)) != record or
                any(current_answer(latest, page) != current_answer(value, page) for page in p['pages'])):
            raise StateError('pdf_production_authority_changed')
        del content
        return AuthoritySnapshot(source_id, content_hash, count, record['status'], units, tuple(medical),
            tuple(page['classification'] for page in p['pages']))

    def verify(self, spec):
        current = self.current(spec['source_file_id'])
        return spec in current.units + current.medical_pages
