"""Candidate persistence in the existing confirmation JSON, never native state."""
from copy import deepcopy

from .drive_run_state import StateError
from .receipt_reimport_production import digest


class MedicalCandidateState:
    def __init__(self,store):self.store=store

    def install_crop_review(self,record,image,key):
        """Operator publishes a real UI decision under the existing maintenance lock."""
        from .medical_crop_review import reviewed_crop
        from .receipt_confirmation import review_id
        source=record['source'];rid=review_id('medical',source)
        item=self.store.value['confirmation_items'][rid]
        if item['kind']!='medical' or item['status']!='waiting' or item['source']!=source:
            raise StateError('medical_crop_review_source_changed')
        reviewed_crop(source,image,record,key)
        value=deepcopy(self.store.value)
        if value.get('medical_crop_reviews',{}).get(rid)==record:return False
        value.setdefault('medical_crop_reviews',{})[rid]=deepcopy(record)
        self.store.save(value)
        return True

    def get(self,analysis_id):
        return deepcopy(self.store.value.get('medical_image_analyses',{}).get(analysis_id))

    def send_review_allowed(self,source,mapping):
        """A service plan alone cannot authorize another source or crop."""
        from hashlib import sha256
        from .medical_crop_review import canonical
        from .receipt_confirmation import review_id
        record=self.store.value.get('medical_crop_reviews',{}).get(review_id('medical',source))
        if not record or record.get('source')!=source:return False
        review_digest=sha256(canonical(record)).hexdigest()
        return (review_digest in self.store.value.get('medical_image_send_reviews',[])
            and mapping.get('validation')=='exact_human_reviewed_payment_crop'
            and mapping.get('human_review_digest')==review_digest
            and mapping.get('crop_sha256')==record.get('crop_sha256')
            and mapping.get('preprocessor')==record.get('preprocessor'))

    def put(self,analysis_id,record):
        value=deepcopy(self.store.value)
        value.setdefault('medical_image_analyses',{})[analysis_id]=deepcopy(record)
        self.store.save(value)

    def begin(self,analysis_id,*,review_id,source,mapping,model,prompt):
        old=self.get(analysis_id)
        if old is not None:
            if old['source']!=source or old['mapping']!=mapping or old['model']!=model or old['prompt']!=prompt:
                raise StateError('medical_analysis_binding_changed')
            if old['phase']=='complete':return False
            raise StateError('medical_analysis_reconciliation_required')
        item=self.store.value['confirmation_items'][review_id]
        if item['kind']!='medical' or item['source']!=source:
            raise StateError('medical_analysis_source_changed')
        self.put(analysis_id,dict(phase='intent',review_id=review_id,source=source,
            mapping=mapping,model=model,prompt=prompt))
        return True

    def complete(self,analysis_id,result,integrity_tag=None):
        old=self.get(analysis_id)
        if old is None or old['phase']!='intent':raise StateError('medical_analysis_intent_required')
        old.update(phase='complete',result=deepcopy(result),integrity_tag=integrity_tag);self.put(analysis_id,old)

    def publish(self,review_id,source,fields):
        """Only candidate/display state; user's eight input fields stay untouched."""
        value=deepcopy(self.store.value);item=value['confirmation_items'][review_id]
        if item['kind']!='medical' or item['source']!=source:
            raise StateError('medical_candidate_source_changed')
        if set(fields)-{'date','issuer','amount_yen','category','review_message','provenance'}:
            raise StateError('medical_candidate_fields_invalid')
        candidate=dict(fields,source=deepcopy(source));candidate['candidate_id']=digest(candidate)
        if item.get('medical_candidates')==candidate:return False
        if item['status']!='waiting':
            # A fresh AI reading never rewrites an already confirmed record.
            item['medical_candidate_comparison']=candidate
        else:
            item['medical_candidates']=candidate
            if str(item['inputs'][5]) not in {'','保留'}:
                item['require_reconfirm']=True
                item['error']='候補が変更されました。判断を保留に戻し、候補を再確認してください'
        self.store.save(value)
        return True
