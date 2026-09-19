"""AI-key-free local preparation; only minimal fields/crops cross its boundary."""
from collections import defaultdict
from hashlib import sha256
import hmac
import json
import os
import re

from .medical_anonymization import AnonymizationHold, compact, png, render_single_page, tokens, prepare_payment_crop
from .medical_image_candidate import seal_crop
from .medical_issuer_selector_shadow import IssuerBinding, OcrFacilityRegion, OcrPageForIssuerSelection, select_medical_issuer_shadow
from .medical_transaction_combined_shadow import MedicalDocumentBinding


def _canonical(value):
    return json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=True).encode()


def local_fields(observations, binding, size):
    groups=defaultdict(list)
    for token in observations:groups[token['line']].append(token)
    from .medical_local_date import receipt_date
    day,date_provenance=receipt_date(observations)
    regions=[]
    for group in groups.values():
        group.sort(key=lambda t:t['box'][0]);boxes=[t['box'] for t in group]
        text=' '.join(t['text'] for t in group)
        regions.append(OcrFacilityRegion(ordinal=len(regions),reading_order=len(regions),raw_text=text,
            confidence=max(0,min(1,min(t['confidence'] for t in group)/100)),
            bbox_xywh=(float(min(b[0] for b in boxes)),float(min(b[1] for b in boxes)),
                float(max(b[2] for b in boxes)-min(b[0] for b in boxes)),float(max(b[3] for b in boxes)-min(b[1] for b in boxes)))))
    fields={'date':day, 'issuer':'','category':''}
    if not regions:return fields,{**date_provenance,'issuer_status':'missing'}
    page=OcrPageForIssuerSelection(binding=IssuerBinding(source_sha256=binding.source_sha256,
        image_sha256=binding.source_image_sha256,unit=binding.unit,page=binding.page),
        width=size[0],height=size[1],regions=tuple(regions))
    selected=select_medical_issuer_shadow(page,expected_binding=page.binding)
    name=selected.issuer_facility_name or ''
    # An OCR line containing an issuer plus other personal content is not a
    # facility-name field. Keep it local and leave the candidate blank.
    isolated_name=(len(name)<=80 and not re.search(r'\d|[〒@/\\]|様|患者|氏名|生年|住所|電話|保険|診療内容|担当',name))
    if selected.verdict=='SELECTED_ISSUER' and isolated_name:
        fields['issuer']=compact(selected.issuer_facility_name)
        fields['category']='医療・保険｜'+('薬' if selected.issuer_facility_type=='pharmacy' else '病院')
    # Full OCR page/reference providers are neither persisted nor emitted.
    return fields,{**date_provenance,'issuer_status':selected.verdict,
        'paid_receipt_evidence':any(any(label in compact(r.raw_text) for label in ('領収書','領収証')) for r in regions),
        'issuer_selector':selected.selector_version,'document_binding':binding.model_dump()}


def reread_local_fields(image, observations, binding):
    """Separate layouts, same full original geometry; no human field defaults.

    Each reading must independently establish a field. Conflicting dates or
    providers are never resolved by majority voting or by a merchant template.
    Raw OCR stays transient and is not added to the signed candidate packet.
    """
    readings=[local_fields(observations,binding,image.size)]
    for psm in (4,11):
        try:extra=tokens(image,psm)
        except (RuntimeError,TimeoutError):
            return readings[0]  # No promotion based on an incomplete reread.
        if not extra:return readings[0]
        readings.append(local_fields(extra,binding,image.size))
    fields=dict(readings[0][0]);provenance=dict(readings[0][1])
    days={f['date'] for f,p in readings if f['date']}
    if len(days)==1 and all(p.get('date_candidates',0)<=1 for f,p in readings):
        selected=next((f,p) for f,p in readings if f['date'])
        fields['date']=selected[0]['date']
        provenance.update({k:v for k,v in selected[1].items() if k.startswith('date_')})
    else:
        fields['date']='';provenance.update(date_candidates=len(days),date_evidence_verified=False)
    names={(compact(f['issuer']),f['category']) for f,p in readings if f['issuer']}
    if len(names)==1:
        fields['issuer'],fields['category']=next(iter(names));provenance['issuer_status']='SELECTED_ISSUER'
    else:
        fields['issuer']=fields['category']='';provenance['issuer_status']='REQUIRES_HUMAN_REVIEW'
    provenance['paid_receipt_evidence']=any(p.get('paid_receipt_evidence') for f,p in readings)
    provenance['field_readings']=len(readings)
    return fields,provenance


def prepare(source, payload, key, *, crop_review=None, review_key=None, automatic=False):
    if os.environ.get('GEMINI_API_KEY'):raise ValueError('medical_preprocessor_received_ai_key')
    if sha256(payload).hexdigest()!=source['sha256']:raise ValueError('medical_source_content_changed')
    try:
        from .receipt_local_ocr import enabled,read_tokens,VERSION as OCR_VERSION
        image=render_single_page(payload,source['mime_type'])
        neural=automatic and enabled()
        observations=read_tokens(image) if neural else tokens(image)
        binding=MedicalDocumentBinding(source_sha256=source['sha256'],source_image_sha256=sha256(png(image)).hexdigest(),unit=1,page=1)
        fields,local_provenance=reread_local_fields(image,observations,binding) if automatic and not neural else local_fields(observations,binding,image.size)
        if neural:local_provenance['ocr_engine']=OCR_VERSION
    except AnonymizationHold as error:
        return {'status':'held','reason':str(error),'source':source,'fields':{}},None
    packet={'source':source,'fields':fields,'local_provenance':local_provenance}
    if neural:
        from .medical_local_reading import read_local_payment
        parsed,reason=read_local_payment(image,observations,fields,local_provenance)
        if parsed is None:packet.update(status='held',reason=reason)
        else:packet.update(status='local_ready',local_parsed=parsed.model_dump())
        # This result is consumed immediately in the key-free intake process.
        # It neither grants crop permission nor enters the cloud sender.
        return packet,None
    try:
        if crop_review is not None and not automatic:
            from .medical_crop_review import reviewed_crop
            crop=reviewed_crop(source,image,crop_review,review_key)
        else:
            crop=prepare_payment_crop(payload,source['mime_type'],image=image,observations=observations,automatic=automatic)
    except AnonymizationHold as error:
        packet.update(status='held',reason=str(error))
        if automatic:packet['candidate_checks']=getattr(error,'candidate_checks',{})
        return packet,None
    if any(crop.mapping[name]!=getattr(binding,name) for name in ('source_sha256','source_image_sha256','unit','page')):
        raise ValueError('medical_evidence_binding_mismatch')
    if automatic:
        from .medical_auto_posting import POLICY
        # The automatic branch never reads or re-signs a human crop record.
        crop.mapping.update(automatic_policy=POLICY,payment_label=crop.label)
        if crop.accounting_evaluation is not None:
            packet['accounting_evaluation']=crop.accounting_evaluation
    packet.update(status='prepared',mapping=crop.mapping,proof=seal_crop(crop.payload,crop.label,key))
    packet['preparation_tag']=hmac.new(key,b'medical-preparation\0'+_canonical(packet),'sha256').hexdigest()
    return packet,crop.payload


def verify_preparation(packet,payload,key):
    from .medical_image_candidate import verify_crop
    if packet.get('status')!='prepared':raise ValueError('medical_preparation_not_ready')
    unsigned={k:v for k,v in packet.items() if k!='preparation_tag'}
    expected=hmac.new(key,b'medical-preparation\0'+_canonical(unsigned),'sha256').hexdigest()
    if not hmac.compare_digest(expected,packet.get('preparation_tag','')):
        raise ValueError('medical_preparation_changed')
    verify_crop(payload,packet['proof'],key)
    if packet['mapping']['crop_sha256']!=sha256(payload).hexdigest():
        raise ValueError('medical_preparation_changed')
