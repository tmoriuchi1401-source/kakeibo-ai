"""Plan-only subset of PR #91 (1ac98bd); no live authority/writer transport."""
from .identity import SourceRef,Box,digest,stable_page,page_key,intersection
from ..drive_run_state import StateError
SCHEMA='page-receipt-separation-v1'
def validate(value,binding):
    try:
        if (set(value)!={'schema','binding','generation','pages','accounting_allowed'} or value['schema']!=SCHEMA
                or value['binding']!=binding or type(value['generation']) is not int or value['generation']<0
                or value['accounting_allowed'] is not False or not isinstance(value['pages'],dict)):
            raise ValueError()
        for key,record in value['pages'].items():
            if set(record)!={'source','page_number','stable_page_identity','page_key','segmentation_digest','units'}:raise ValueError()
            source=SourceRef.model_validate(record['source']);number=record['page_number']
            if type(number) is not int or not 1<=number<=source.page_count:raise ValueError()
            if record['stable_page_identity']!=stable_page(source,number):raise ValueError()
            expected=digest(['source-page-unit-v1',source.model_dump(),number,record['stable_page_identity']])
            if key!=expected or record['page_key']!=key:raise ValueError()
            if not isinstance(record['units'],list) or not 1<=len(record['units'])<=20:raise ValueError()
            boxes=[Box.model_validate(u['bbox']) for u in record['units']]
            if record['segmentation_digest']!=digest(['receipt-separation-v1',key,[b.model_dump() for b in boxes]]):raise ValueError()
            for i,(u,box) in enumerate(zip(record['units'],boxes),1):
                if set(u)!={'receipt_unit_id','receipt_index','parent_page_identity','bbox','accounting_status'}:raise ValueError()
                uid='page-receipt-v1:'+digest([source.source_file_id,source.source_content_hash,record['stable_page_identity'],record['segmentation_digest'],i])
                if (u['receipt_unit_id']!=uid or u['receipt_index']!=i or u['parent_page_identity']!=record['stable_page_identity']
                        or u['accounting_status']!='not_written'):raise ValueError()
                if any(intersection(box,other)/min(box.area,other.area)>.05 for other in boxes[i:]):raise ValueError()
        if value['generation']!=len(value['pages']):raise ValueError()
    except Exception:raise StateError('receipt_manifest_invalid') from None
    return value
