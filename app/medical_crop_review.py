"""Exact, human-reviewed crop instructions; no account amount or AI authority.

Review saves coordinates and digests, never source/crop pixels. Actions rebuilds
the pixels independently before the usual ephemeral sender attestation.
"""
from hashlib import sha256
import hmac
import json

from .medical_anonymization import AnonymizationHold, LABELS, VERSION, PaymentCrop, png, validate_png

REVIEW_VERSION='medical-crop-human-review-v1'
STATEMENT='only_payment_label_amount_no_personal_or_medical_information'


def canonical(value):
    return json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=True).encode()


def identity_key(private_key):
    return sha256(b'medical-candidate-evidence\0'+private_key.encode()).digest()


def selected_crop(image,coordinates):
    if (type(coordinates) is not list or len(coordinates)!=4
            or any(type(x) is not int for x in coordinates)):
        raise AnonymizationHold('review_coordinates_invalid')
    l,t,r,b=coordinates
    if not (0<=l<r<=image.width and 0<=t<b<=image.height):
        raise AnonymizationHold('review_coordinates_invalid')
    # Human preview and Actions rebuild use exactly this newly drawn PNG.
    crop=image.crop((l,t,r,b)).convert('L').point(lambda v:0 if v<190 else 255).convert('RGB')
    payload=png(crop);validate_png(payload)
    return payload


def make_review(source,image,coordinates,label,*,confirmed,key):
    if confirmed is not True or label not in LABELS or len(key)<32:
        raise AnonymizationHold('human_crop_confirmation_required')
    payload=selected_crop(image,coordinates)
    record={'schema':REVIEW_VERSION,'source':dict(source),'page':1,'unit':1,
        'rendered_page_size':list(image.size),'source_image_sha256':sha256(png(image)).hexdigest(),
        'coordinates':coordinates,'crop_sha256':sha256(payload).hexdigest(),'label':label,
        'preprocessor':VERSION,'statement':STATEMENT}
    record['tag']=hmac.new(key,b'medical-human-crop\0'+canonical(record),'sha256').hexdigest()
    return record


def reviewed_crop(source,image,record,key):
    try:
        unsigned={k:v for k,v in record.items() if k!='tag'}
        expected=hmac.new(key,b'medical-human-crop\0'+canonical(unsigned),'sha256').hexdigest()
        if not hmac.compare_digest(expected,record.get('tag','')):
            raise AnonymizationHold('human_crop_review_signature_mismatch')
        if record['source']!=source:
            raise AnonymizationHold('human_crop_review_source_mismatch')
        if (record['schema']!=REVIEW_VERSION
                or record['preprocessor']!=VERSION or record['statement']!=STATEMENT
                or record['page']!=1 or record['unit']!=1 or record['label'] not in LABELS
                ):
            raise AnonymizationHold('human_crop_review_contract_mismatch')
        if record['rendered_page_size']!=list(image.size):
            raise AnonymizationHold('human_crop_review_dimensions_mismatch')
        payload=selected_crop(image,record['coordinates'])
        if record['source_image_sha256']!=sha256(png(image)).hexdigest():
            if sha256(payload).hexdigest()!=record['crop_sha256']:
                raise AnonymizationHold('human_crop_review_render_and_png_mismatch')
            raise AnonymizationHold('human_crop_review_render_mismatch')
        if sha256(payload).hexdigest()!=record['crop_sha256']:
            raise AnonymizationHold('human_crop_review_png_mismatch')
    except AnonymizationHold:
        raise
    except Exception:
        raise AnonymizationHold('human_crop_review_changed_or_invalid') from None
    return PaymentCrop(payload,{'source_sha256':source['sha256'],
        'source_image_sha256':record['source_image_sha256'],'page':1,'unit':1,
        'crop_coordinates_original':record['coordinates'],'rotation_clockwise_degrees':0,
        'crop_sha256':record['crop_sha256'],'preprocessor':VERSION,
        'rendered_page_size':record['rendered_page_size'],'metadata_removed':True,
        'validation':'exact_human_reviewed_payment_crop','human_review_digest':sha256(canonical(record)).hexdigest()},
        record['label'])
