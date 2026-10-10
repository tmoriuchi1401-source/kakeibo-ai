"""Source -> Page -> independently validated Receipt Units, no write capability."""
from copy import deepcopy
from hashlib import sha256
import json, math, re
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator
from .receipt_plan.models import ReceiptResult
from .pdf_page_identity import page_identity
from .pdf_receipt_policy import apply_receipt_policy

def digest(value):
    return sha256(json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=True,allow_nan=False).encode()).hexdigest()

class SourceRef(BaseModel):
    model_config=ConfigDict(frozen=True,extra='forbid',hide_input_in_errors=True)
    source_file_id:str=Field(pattern=r'^[A-Za-z0-9_-]{1,150}$')
    source_content_hash:str=Field(pattern=r'^[0-9a-f]{64}$')
    page_count:int=Field(strict=True,ge=1,le=50)
    source_kind:Literal['pdf','image']='pdf'
    @model_validator(mode='after')
    def image_is_one_page(self):
        if self.source_kind=='image' and self.page_count!=1:raise ValueError('image_page_count_invalid')
        return self

class PageUnit(BaseModel):
    model_config=ConfigDict(frozen=True,extra='forbid',hide_input_in_errors=True)
    source:SourceRef
    page_number:int=Field(strict=True,ge=1,le=50)
    stable_page_identity:str=Field(pattern=r'^[0-9a-f]{64}$')
    automatic_classification:Literal['normal','medical','payroll','sensitive_unknown','unknown']
    automatic_reason:str
    observation_complete:bool=Field(strict=True)
    extraction_status:str
    observation_render_hash:str=Field(pattern=r'^[0-9a-f]{64}$')
    human_page_kind:Literal['general_receipt','medical','payroll','unknown']|None=None
    review_identity:str=Field(pattern=r'^[0-9a-f]{64}$')
    authority_revision:int=Field(strict=True,ge=1)
    clearly_sensitive:bool=Field(default=False,strict=True)
    processing_status:str='observed'
    @model_validator(mode='after')
    def validate_page(self):
        if self.page_number>self.source.page_count or self.stable_page_identity!=stable_page(self.source,self.page_number):
            raise ValueError('page_identity_invalid')
        return self

def stable_page(source,number):
    if source.source_kind=='pdf':return page_identity(source.source_content_hash,number,source.page_count)
    return digest(['image-page-v1',source.source_content_hash,number,source.page_count])

def page_key(page):
    return digest(['source-page-unit-v1',page.source.model_dump(),page.page_number,page.stable_page_identity])

class Box(BaseModel):
    model_config=ConfigDict(frozen=True,extra='forbid',hide_input_in_errors=True)
    left:float=Field(ge=0,le=1,allow_inf_nan=False)
    top:float=Field(ge=0,le=1,allow_inf_nan=False)
    right:float=Field(ge=0,le=1,allow_inf_nan=False)
    bottom:float=Field(ge=0,le=1,allow_inf_nan=False)
    @model_validator(mode='after')
    def positive_area(self):
        if self.right<=self.left or self.bottom<=self.top:raise ValueError('bbox_invalid')
        return self
    @property
    def area(self):return (self.right-self.left)*(self.bottom-self.top)

class LocatedReceipt(BaseModel):
    model_config=ConfigDict(extra='forbid',hide_input_in_errors=True)
    bbox:Box
    receipt:ReceiptResult
    item_boxes:list[Box]=Field(default_factory=list,max_length=300)
    @model_validator(mode='after')
    def bounded_result(self):
        r=self.receipt
        if (len(r.items)>300 or len(r.merchant)>100 or len(r.payment_method)>50
                or len(r.date)>32 or len(r.note)>300
                or any(len(x.name)>200 or len(x.note)>300 or len(x.major_category)>100
                       or len(x.minor_category)>100 for x in r.items)):
            raise ValueError('receipt_result_resource_limit')
        return self

class PageReceiptExtraction(BaseModel):
    model_config=ConfigDict(extra='forbid',hide_input_in_errors=True)
    receipts:list[LocatedReceipt]=Field(min_length=1,max_length=20)
    separation_complete:bool=Field(strict=True)
    mixed_page_kind_suspected:bool=Field(strict=True)
    cross_page_continuation_suspected:bool=Field(strict=True)

def intersection(a,b):
    return max(0,min(a.right,b.right)-max(a.left,b.left))*max(0,min(a.bottom,b.bottom)-max(a.top,b.top))
def iou(a,b):
    common=intersection(a,b)
    return common/(a.area+b.area-common)
def ordered(reading):
    # Sort by spatial row band, then left, then exact top. Never SDK array order.
    return sorted(reading.receipts,key=lambda r:(math.floor(r.bbox.top/.03),r.bbox.left,r.bbox.top))

def segmentation_issues(first,second):
    reasons=[]
    if any(not r.separation_complete for r in (first,second)):reasons.append('separation_incomplete')
    if any(r.mixed_page_kind_suspected for r in (first,second)):reasons.append('mixed_page_kind')
    if any(r.cross_page_continuation_suspected for r in (first,second)):reasons.append('cross_page_continuation')
    a,b=ordered(first),ordered(second)
    if len(a)!=len(b):return list(dict.fromkeys(reasons+['receipt_count_changed']))
    for reading in (a,b):
        for i,r in enumerate(reading):
            if len(reading)>1 and len(r.item_boxes)!=len(r.receipt.items):reasons.append('item_positions_missing')
            if any(intersection(r.bbox,box)/box.area<.98 for box in r.item_boxes):reasons.append('item_outside_receipt')
            for other in reading[i+1:]:
                if intersection(r.bbox,other.bbox)/min(r.bbox.area,other.bbox.area)>.05:reasons.append('receipt_bbox_overlap')
                if any(iou(x,y)>.5 for x in r.item_boxes for y in other.item_boxes):reasons.append('item_shared_between_receipts')
    if any(iou(x.bbox,y.bbox)<.8 for x,y in zip(a,b)):reasons.append('receipt_positions_changed')
    fingerprints=[digest([r.receipt.date,r.receipt.merchant,r.receipt.total,
        [x.model_dump() for x in r.receipt.items]]) for r in b]
    if len(set(fingerprints))!=len(fingerprints):reasons.append('duplicate_receipt_result')
    # A changing assignment of the same item between spatial receipts is a
    # segmentation problem, not an invitation to post the apparently good half.
    def ownership(reading):
        out={}
        for i,r in enumerate(reading):
            for x in r.receipt.items:out.setdefault((x.name.strip(),x.amount),set()).add(i)
        return out
    left,right=ownership(a),ownership(b)
    if any(left[k]!=right[k] for k in left.keys() & right.keys()):reasons.append('item_ownership_changed')
    return list(dict.fromkeys(reasons))

TERMINAL={'imported','manual_imported','medical_manual_imported','duplicate_confirmed','intentionally_skipped'}

def build_receipt_units(page,first,second,categories,*,previous=None,unit_texts=None,unit_gates=None):
    """Two independent spatial readings; old manifest must match before reuse.

    Freeze the first corroborated geometry once. Replays reuse its IDs rather
    than deriving new IDs from model jitter, amounts or merchants.
    """
    reasons=segmentation_issues(first,second)
    a,b=ordered(first),ordered(second)
    if previous:
        if (previous.get('page_key')!=page_key(page) or len(previous.get('units',[]))!=len(a)):
            reasons.append('saved_segmentation_changed')
        else:
            boxes=[Box.model_validate(x['bbox']) for x in previous['units']]
            if any(iou(box,r.bbox)<.8 for box,r in zip(boxes,a)):reasons.append('saved_segmentation_changed')
            expected=digest(['receipt-separation-v1',page_key(page),[box.model_dump() for box in boxes]])
            if previous.get('segmentation_digest')!=expected:raise ValueError('saved_segmentation_invalid')
            if len({u['receipt_unit_id'] for u in previous['units']})!=len(boxes):raise ValueError('saved_receipt_identity_invalid')
    if reasons:return {'status':'receipt_segmentation_review','reason_codes':sorted(set(reasons)),
                       'page_key':page_key(page),'units':[],'terminal':False,
                       'candidate_regions':[[r.bbox.model_dump() for r in pass_] for pass_ in (a,b)],
                       'geometry_diagnostic':[[{'receipt_index':index,'bbox':r.bbox.model_dump(),
                           'item_count':len(r.receipt.items),'item_box_count':len(r.item_boxes),
                           'item_boxes':[box.model_dump() for box in r.item_boxes],
                           'outside_item_indices':[i for i,box in enumerate(r.item_boxes,1)
                               if intersection(r.bbox,box)/box.area<.98]}
                           for index,r in enumerate(pass_,1)] for pass_ in (a,b)]}
    manifest=digest(['receipt-separation-v1',page_key(page),[r.bbox.model_dump() for r in a]])
    if previous:manifest=previous['segmentation_digest']
    units=[]
    for index,(one,two) in enumerate(zip(a,b),1):
        text=(unit_texts or {}).get(index,'')
        result=deepcopy(two.receipt)
        issues,checks=apply_receipt_policy(result,categories,text=text,gate=(unit_gates or {}).get(index),
                                          readings=[one.receipt,two.receipt])
        uid='page-receipt-v1:'+digest([page.source.source_file_id,page.source.source_content_hash,
            page.stable_page_identity,manifest,index])
        old=previous['units'][index-1] if previous else None
        if old and old['receipt_unit_id']!=uid:raise ValueError('saved_receipt_identity_invalid')
        if old and old.get('accounting_status') in TERMINAL:
            if old.get('parsed')!=result.model_dump():
                return {'status':'receipt_segmentation_review','reason_codes':['posted_result_changed'],
                        'page_key':page_key(page),'units':[],'terminal':False}
        units.append({'receipt_unit_id':uid,'parent_page_identity':page.stable_page_identity,
            'receipt_index':index,'bbox':old['bbox'] if old else one.bbox.model_dump(),
            'parsed':result.model_dump(),'validation_issues':issues,'validation':checks,
            'analysis_status':'would_need_review' if issues else 'would_import',
            'accounting_status':old['accounting_status'] if old else 'not_written'})
    return {'status':'receipts_observed','page_key':page_key(page),'segmentation_digest':manifest,
            'units':units,'terminal':bool(units) and all(u['accounting_status'] in TERMINAL for u in units)}

def source_terminal(source,pages,verify_readback):
    """No move authority: only a fresh, fully enumerated source is eligible."""
    if len(pages)!=source.page_count or sorted(p['page_number'] for p in pages)!=list(range(1,source.page_count+1)):
        return False
    seen=set()
    for p in pages:
        if p['source']!=source.model_dump() or not p.get('units'):return False
        identity=stable_page(source,p['page_number'])
        if p.get('stable_page_identity')!=identity or p.get('authority_current') is not True:return False
        for u in p['units']:
            if (u.get('receipt_unit_id') in seen or u.get('parent_page_identity')!=identity
                    or u.get('accounting_status') not in TERMINAL or not verify_readback(u)):return False
            seen.add(u['receipt_unit_id'])
    return True
