"""Synthetic Drive authority fixture; no UI, Medical store or live clients."""
from types import SimpleNamespace
from uuid import UUID
from app import pdf_grouping_authority as authority, receipt_pdf_units as pdf
from test_receipt_pdf_units import synthetic_pdf
from test_conditional_drive_state_v2 import FakeV2, adapter

BINDING='b'*64
NOW='2026-10-04T12:00:00+00:00'


def context(kinds=('normal','normal')):
    drive=FakeV2();drive.payload=authority.encoded(authority.empty_state(BINDING))
    content=synthetic_pdf(kinds)
    live=SimpleNamespace(observations=pdf.observe_pdf(content,'drive-source-id'),content=content)
    store=authority.DriveGroupingStore(adapter(drive),BINDING,preflight=lambda:None)
    g=authority.DurablePdfGrouping(store,lambda sid,old:live.observations,clock=lambda:NOW)
    return g,live,drive


def request(view,*,operation='confirm',partition=None,number=1):
    p=view['proposal']
    return {'request_id':str(UUID(int=number)),'source_file_id':p['source_file_id'],
        'source_content_hash':p['source_content_hash'],'proposal_digest':p['proposal_digest'],
        'grouping_revision':p['grouping_version'],'operation':operation,'partition':partition}
