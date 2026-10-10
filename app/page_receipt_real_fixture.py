"""Ephemeral real-image fixture: two already-normal source pages, never Medical.

Only the manual read-only runner calls this module. No Drive image copy,
authority grant, ledger, writer or image artifact. Provenance is diagnostic.
"""
from contextlib import closing
from hashlib import sha256
from io import BytesIO
import pypdfium2 as pdfium
from PIL import Image
from .drive_run_state import StateError
from .page_receipt_model import SourceRef,PageUnit,stable_page,digest
from .human_general_authority import binding_fields,review_identity
from .page_receipt_ai import authorize_payload
from .receipt_pdf_units import _render_png
from .pdf_bounded_rendering import render_scale

PARENTS=(11,12)
SIZE=(2400,1800)

def real_image_fixture(current,source,expected):
    pages=[current(expected['source_file_id'],n) for n in PARENTS]
    if any(p.automatic_classification!='normal' or p.human_page_kind!='general_receipt'
           or p.clearly_sensitive or not p.observation_complete or p.extraction_status!='extracted' for p in pages):
        raise StateError('real_fixture_normal_parents_required')
    initial=[binding_fields(p) for p in pages]
    def check():
        if [binding_fields(current(expected['source_file_id'],n)) for n in PARENTS]!=initial:
            raise StateError('real_fixture_parent_changed')
        raw=source(expected['source_file_id'])
        if type(raw) is not bytes or sha256(raw).hexdigest()!=expected['source_content_hash']:
            raise StateError('real_fixture_source_changed')
        return raw
    raw=check()
    with Image.new('RGB',SIZE,'white') as canvas, closing(pdfium.PdfDocument(raw)) as document:
        if len(document)!=expected['page_count']:raise StateError('real_fixture_page_structure_changed')
        for index,parent in enumerate(pages):
            with closing(document[parent.page_number-1]) as selected:
                scale,_=render_scale(*selected.get_size(),2_000_000)
                png=_render_png(selected,scale,2_000_000)
            authorize_payload(parent,png,current_page=current,load_source=source,load_grant=lambda _:None)
            with Image.open(BytesIO(png)) as image:
                image.thumbnail((SIZE[0]//2-80,SIZE[1]-80))
                canvas.paste(image,(index*(SIZE[0]//2)+40+(SIZE[0]//2-80-image.width)//2,
                    40+(SIZE[1]-80-image.height)//2))
            png=None
        output=BytesIO();canvas.save(output,format='PNG');fixture=output.getvalue()
    ref=SourceRef(source_file_id='readonly-real-image-fixture',source_content_hash=sha256(fixture).hexdigest(),
        page_count=1,source_kind='image')
    page=PageUnit(source=ref,page_number=1,stable_page_identity=stable_page(ref,1),
        automatic_classification='normal',automatic_reason='normal_receipt_evidence',
        observation_complete=True,extraction_status='extracted',observation_render_hash=sha256(fixture).hexdigest(),
        human_page_kind='general_receipt',review_identity='0'*64,authority_revision=1)
    page=page.model_copy(update={'review_identity':review_identity(page)})
    def current_fixture(sid,n):
        if sid!=ref.source_file_id or n!=1:raise StateError('real_fixture_target_invalid')
        check();return page
    def load_fixture(sid):
        if sid!=ref.source_file_id:raise StateError('real_fixture_target_invalid')
        check();return fixture
    provenance={'kind':'derived_real_receipt_images','parent_source_hash':expected['source_content_hash'],
        'parent_pages':list(PARENTS),'parent_page_identities':[p.stable_page_identity for p in pages],
        'fixture_source':ref.model_dump(),'fixture_digest':digest([ref.model_dump(),initial]),
        'persisted_images':0,'p1_rendered':0,'accounting_authority':False}
    return page,current_fixture,load_fixture,provenance

def verify_real_replay(first,replay):
    if first.get('status') not in {'would_import','would_need_review'} or replay.get('status') not in {'would_import','would_need_review'}:
        raise StateError('real_fixture_segmentation_not_proven')
    a,b=first.get('units',[]),replay.get('units',[])
    if len(a)<2 or len(a)!=len(b):raise StateError('real_fixture_receipt_count_changed')
    if (first.get('segmentation_digest')!=replay.get('segmentation_digest') or
            [u['receipt_unit_id'] for u in a]!=[u['receipt_unit_id'] for u in b] or
            len({u['receipt_unit_id'] for u in a})!=len(a)):
        raise StateError('real_fixture_receipt_identity_changed')
    # Boundaries alone are insufficient: each spatial receipt must retain its
    # own transaction values and item ownership across both independent runs.
    for one,two in zip(a,b):
        fields=('date','total','merchant','transaction_kind')
        if any(one['parsed'][k]!=two['parsed'][k] for k in fields):
            raise StateError('real_fixture_receipt_values_changed')
        signature=lambda u:sorted((x['name'],x['amount']) for x in u['parsed']['items'])
        if signature(one)!=signature(two):raise StateError('real_fixture_receipt_items_changed')
    return True
