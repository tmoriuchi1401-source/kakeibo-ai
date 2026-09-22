"""Local document identity evidence, not authority to post or send an image.

Only keyed references leave the reader. Patient names, patient numbers and
unlabelled numbers are never document identifiers. A caller must still bind
the evidence to current source bytes, accounting rows and owner decisions.
"""
import hmac
import json
import re
import unicodedata

from .medical_anonymization import compact

POLICY='medical-document-identity-v1'
ROLE=r'(?:領収[書証](?:no[.。]?|番号)|請求書番号)[:：]?'
FULL=re.compile(ROLE+r'([0-9]{1,16})',re.IGNORECASE)
LABEL=re.compile(ROLE,re.IGNORECASE)


def _number_text(text):
    normalized=unicodedata.normalize('NFKC',str(text))
    if re.search(r'[0-9]\s+[0-9]',normalized):return ''
    return compact(normalized)


def number_region(observations):
    candidates=[]
    for token in observations:
        text=_number_text(token['text']);match=FULL.fullmatch(text)
        if match:
            if token['confidence']<90:return None
            candidates.append((match[1],tuple(token['box'])))
        elif LABEL.fullmatch(text):
            if token['confidence']<90:return None
            a=token['box'];height=a[3]-a[1]
            nearby=[]
            for other in observations:
                if not re.fullmatch(r'[0-9]{1,16}',_number_text(other['text'])):continue
                b=other['box']
                # Small detector-box overlap is allowed, crossing another
                # field is not. A label cannot claim a patient number at left.
                if (b[0]>=a[2]-.25*height and b[0]-a[2]<=4*height
                        and abs((b[1]+b[3]-a[1]-a[3])/2)<=.6*height):
                    nearby.append(other)
            if len(nearby)!=1 or nearby[0]['confidence']<90:return None
            other=nearby[0];b=other['box']
            if any(t is not token and t is not other and a[2]<t['box'][0]<b[0]
                   and abs((t['box'][1]+t['box'][3]-a[1]-a[3])/2)<=.6*height
                   for t in observations):return None
            candidates.append((_number_text(other['text']),
                               (min(a[0],b[0]),min(a[1],b[1]),max(a[2],b[2]),max(a[3],b[3]))))
    return candidates[0] if len(candidates)==1 else None


def _digits(text):
    text=_number_text(text)
    # Read the whole labelled region, with exactly one uninterrupted digit
    # sequence. Preserve leading zeros; never join split/ambiguous sequences.
    if any(c in text for c in ('-', '−', '△', '▲')):return None
    match=re.fullmatch(r'[^0-9]*([0-9]{1,16})[.,、。]?',text)
    return match[1] if match else None


def confirm_number(image,box,expected):
    from PIL import Image,ImageOps
    import pytesseract
    original=ImageOps.grayscale(image.crop(box))
    patch=original.resize((max(1,round(original.width*64/original.height)),64))
    patch=ImageOps.expand(patch,border=16,fill=255)
    text=pytesseract.image_to_string(patch,lang='jpn+eng',config='--psm 7',timeout=20)
    if any(c in compact(text) for c in ('-', '−', '△', '▲')):return False
    actual=_digits(text)
    if actual is not None:return actual==expected
    import cv2
    import numpy as np
    patch=original.resize((max(1,round(original.width*48/original.height)),48))
    _,binary=cv2.threshold(np.asarray(patch),0,255,cv2.THRESH_BINARY+cv2.THRESH_OTSU)
    patch=ImageOps.expand(Image.fromarray(binary),border=16,fill=255)
    actual=_digits(pytesseract.image_to_string(patch,lang='eng',config='--psm 7',timeout=20))
    return actual==expected


def read_identity(image,observations,parsed,provenance,key):
    """Return opaque evidence only after independent local number agreement."""
    if len(key)<32:raise ValueError('medical_identity_key_required')
    binding=provenance.get('document_binding',{})
    if (not re.fullmatch('[a-f0-9]{64}',binding.get('source_sha256',''))
            or not re.fullmatch('[a-f0-9]{64}',binding.get('source_image_sha256',''))
            or binding.get('page')!=1 or binding.get('unit')!=1
            or provenance.get('issuer_status')!='SELECTED_ISSUER'
            or provenance.get('date_evidence_verified') is not True
            or provenance.get('date_candidates')!=1):return None
    if any('再発行' in compact(t['text']) and '再発行しません' not in compact(t['text'])
           for t in observations):return None
    region=number_region(observations)
    if region is None:return None
    number,box=region
    try:
        if not confirm_number(image,box,number):return None
    except (RuntimeError,TimeoutError):return None
    def reference(kind,value):
        return hmac.new(key,(POLICY+'\0'+kind+'\0'+value).encode(),'sha256').hexdigest()
    # Normalize typographic middle-dot variants only, not name characters,
    # abbreviations, corporate prefixes or approximate merchant similarities.
    issuer=compact(parsed.merchant).replace('·','・')
    from .medical_issuer_contact import read_contacts
    contacts=read_contacts(image,observations,parsed.merchant,reference)
    from .medical_issuer_locality import read_locality
    locality=read_locality(image,observations,parsed.merchant,reference)
    return {'policy':POLICY,'source_sha256':binding['source_sha256'],
            'source_image_sha256':binding['source_image_sha256'],
            'key_ref':reference('key','domain'),
            'issuer_ref':reference('issuer',issuer),
            'document_ref':reference('document',json.dumps([issuer,parsed.date,number],ensure_ascii=True)),
            'serial_ref':reference('serial',json.dumps([parsed.date,number],ensure_ascii=True)),
            'issuer_contacts':contacts,
            'issuer_locality':locality,
            'date':parsed.date,'amount':parsed.total,'number_readers':2}


def compare_identities(left,right):
    """Three-way evidence comparison; never use amount/date alone for equality."""
    if not left or not right:return 'unknown'
    if any(x.get('policy')!=POLICY or x.get('number_readers')!=2 for x in (left,right)):return 'unknown'
    for x in (left,right):
        if any(not re.fullmatch('[a-f0-9]{64}',x.get(k,'')) for k in
               ('source_sha256','source_image_sha256','issuer_ref','document_ref','key_ref')):return 'unknown'
    if left['key_ref']!=right['key_ref']:return 'unknown'
    if left['document_ref']==right['document_ref']:
        return 'same' if (left['date'],left['amount'],left['issuer_ref'])==(right['date'],right['amount'],right['issuer_ref']) else 'unknown'
    if left['source_sha256']==right['source_sha256'] or left['source_image_sha256']==right['source_image_sha256']:
        return 'unknown'  # Same pixels with inconsistent identities cannot post.
    # Name variation alone cannot prove two providers differ. Document numbers
    # are scoped to the same verified issuer; other cases need more evidence.
    if left['issuer_ref']==right['issuer_ref']:return 'different'
    # A spelling difference must not become a general provider alias. When
    # independently read TEL AND FAX both agree, different verified bill
    # serials can distinguish bills despite that spelling. Never use contact
    # agreement to link two documents or guess/correct a provider's name.
    serials=[x.get('serial_ref','') for x in (left,right)]
    contacts=[x.get('issuer_contacts',{}) for x in (left,right)]
    if any(not re.fullmatch('[a-f0-9]{64}',s) for s in serials) or serials[0]==serials[1]:return 'unknown'
    from .medical_issuer_locality import distinct_locations
    if distinct_locations(left,right):return 'different'
    if any(not isinstance(c,dict) or set(c)!={'telephone','fax'}
           or any(not re.fullmatch('[a-f0-9]{64}',str(v)) for v in c.values()) for c in contacts):return 'unknown'
    if contacts[0]==contacts[1] and contacts[0]['telephone']!=contacts[0]['fax']:return 'different'
    return 'unknown'
