"""The completion reader must preserve the existing page manual identity."""
from app.pdf_page_medical import manual_source
from app.pdf_page_kind import current_answer
from app.receipt_confirmation import review_id
from app.receipt_pdf_units import _digest
from test_pdf_manual_terminal import medical
from test_receipt_pdf_units import local_ocr


def test_terminal_reference_uses_existing_manual_source_and_review_identity(local_ocr):
    reader,spec,record,db,drive,live,store,t,key=medical()
    value=reader.authority.legacy.load()
    page=value['records'][_digest(spec['source_file_id'])]['proposal']['pages'][0]
    existing_source=manual_source(page,current_answer(value,page))
    assert store.value['confirmation_items'][key]['source']==existing_source
    assert key==review_id('medical',existing_source)
    before=(store.payload,t.writes,drive.payload,len(drive.updates),db.writes)
    assert reader.verify(spec,record['intent_digest']) is True
    assert before==(store.payload,t.writes,drive.payload,len(drive.updates),db.writes)
