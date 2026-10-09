"""Plan-only subset of PR #91 (1ac98bd); no live authority/writer transport."""
from hashlib import sha256
import json,re
from typing import Literal
from pydantic import BaseModel,ConfigDict,Field,model_validator
from ..drive_run_state import StateError
from .models import ReceiptResult
def page_identity(source_hash, number, count):
    if (not isinstance(source_hash,str) or not re.fullmatch('[0-9a-f]{64}',source_hash)
            or type(number) is not int or type(count) is not int or not 1<=number<=count<=50):
        raise StateError('pdf_page_identity_invalid')
    return sha256(json.dumps(['pdf-page-v1',source_hash,number,count],
        ensure_ascii=True,separators=(',',':')).encode()).hexdigest()
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

def intersection(a,b):
    return max(0,min(a.right,b.right)-max(a.left,b.left))*max(0,min(a.bottom,b.bottom)-max(a.top,b.top))
