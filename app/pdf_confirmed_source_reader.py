"""Manual confirmation freshness for an already migrated durable source.

Original SHA/page count/ordinal identity and both Drive authorities are checked.
Legacy review IDs and historical render fingerprints remain unchanged. This is
not observation, an AI gate, a source of new proposals, or accounting authority.
No page is rendered and no OCR is invoked, including a manually confirmed
Medical page. The caller still checks owner input and the existing writer.
"""
from contextlib import closing
from copy import deepcopy
from hashlib import sha256

import pypdfium2 as pdfium

from .drive_run_state import StateError
from .pdf_grouping_authority_v2 import DriveGroupingV2Store, page_identity
from .receipt_pdf_units import _digest, MAX_SOURCE_BYTES, PageObservation, PdfObservations


def confirmed_legacy_observations(store, load_source, source_id, previous, *, numbers=None):
    if not isinstance(store, DriveGroupingV2Store) or previous is None:
        raise StateError('page_kind_durable_identity_required')
    value=store.load();v2=deepcopy(value)
    before=(store.payload,store.tag,store.legacy_store.payload,store.legacy_store.tag)
    record=value['records'].get(_digest(source_id))
    old=value['migration']['legacy_authority_snapshot']['records'].get(_digest(source_id))
    if (not record or not old or old['proposal']!=previous
            or record['status']!='grouping_confirmed' or old['status']!='grouping_confirmed'):
        raise StateError('page_kind_source_changed')
    p=record['proposal'];count=p['page_count']
    if numbers is not None and (not isinstance(numbers,list) or not numbers or numbers!=sorted(set(numbers))
            or any(type(n) is not int or not 1<=n<=count for n in numbers)):
        raise StateError('page_kind_page_changed')
    content=load_source(source_id)
    if (not isinstance(content,bytes) or not content or len(content)>MAX_SOURCE_BYTES
            or sha256(content).hexdigest()!=p['source_content_hash']):
        raise StateError('page_kind_source_changed')
    try:
        with closing(pdfium.PdfDocument(content)) as document:
            if len(document)!=count:raise StateError('page_kind_source_changed')
    except StateError:raise
    except Exception:raise StateError('page_kind_source_invalid') from None
    for n,page in enumerate(p['pages'],1):
        if (page['page_number']!=n or page['page_identity']!=page_identity(p['source_content_hash'],n,count)
                or page['observation_render_hash']!=previous['pages'][n-1]['page_hash']):
            raise StateError('page_kind_page_changed')
    # The saved render hash only links legacy review intent to migration. No
    # current PNG is compared with it, and no privacy restriction is rewritten.
    if store.load()!=v2 or (store.payload,store.tag,store.legacy_store.payload,store.legacy_store.tag)!=before:
        raise StateError('page_kind_source_changed')
    return PdfObservations(source_id,p['source_content_hash'],
        tuple(PageObservation(**page) for page in previous['pages']))
