"""Medical accounting fields from local OCR, with a second digit reader.

No original, crop, OCR text or patient field leaves this process. This path does
not grant permission to send an image and is independent of the cloud crop gate.
"""
import re
from copy import deepcopy
from .medical_anonymization import compact
from .models import ReceiptResult,ReceiptItem

POLICY='medical-local-consensus-v1'
LABELS=('領収金額','領収額','お支払金額','お支払額','支払金額','支払額','今回入金額')
NUMBER=re.compile(r'(?:[¥￥])?(?P<amount>(?:[0-9]{1,3}(?:,[0-9]{3})+|[0-9]+))(?:円)?')

def label_text(value):
    # The multilingual recognizer sometimes emits the equivalent Chinese
    # glyph. Normalize only a complete allowlisted payment label, not names.
    value=compact(value).translate(str.maketrans({'额':'額','收':'収'}))
    return value if value in LABELS else ''

def amount_text(value):
    value=compact(value)
    if value.endswith('-') and value.startswith(('¥','￥')):value=value[:-1]
    match=NUMBER.fullmatch(value)
    return int(match['amount'].replace(',','')) if match else None

def candidate_pairs(observations):
    labels=[t for t in observations if label_text(t['text'])]
    pairs=[]
    for label in labels:
        if label['confidence']<85:return [],'payment_label_uncertain'
        a=label['box'];candidates=[]
        for token in observations:
            amount=amount_text(token['text'])
            if amount is None:continue
            b=token['box'];h=max(a[3]-a[1],b[3]-b[1])
            right=(0<=b[0]-a[2]<=12*h and abs((a[1]+a[3]-b[1]-b[3])/2)<=.65*h)
            below=(0<=b[1]-a[3]<=1.5*h and max(a[0]-b[2],b[0]-a[2])<=h
                   and abs((a[0]+a[2]-b[0]-b[2])/2)<=3*h)
            if not (right or below):continue
            # Do not jump across another text field between label and amount.
            if right and any(t is not label and t is not token and
                a[2]<t['box'][0]<b[0] and
                abs((t['box'][1]+t['box'][3]-a[1]-a[3])/2)<=.4*h for t in observations):continue
            if below and any(t is not label and t is not token and
                a[3]<t['box'][1]<b[1] and max(a[0],t['box'][0])<min(a[2],t['box'][2]) for t in observations):continue
            candidates.append((token,amount))
        if len(candidates)!=1:return [],'payment_pair_ambiguous'
        token,amount=candidates[0]
        if token['confidence']<90 or amount<=0:return [],'payment_digits_uncertain'
        pairs.append((label,token,amount))
    if not pairs:return [],'payment_label_missing'
    if len({amount for _,_,amount in pairs})!=1:return [],'payment_amount_conflict'
    return pairs,''

def confirm_digits(image,box,expected):
    import pytesseract
    from PIL import Image,ImageOps
    l,t,r,b=box
    patch=ImageOps.grayscale(image.crop((l,t,r,b)))
    # Normalize text height: very large scanned PDF glyphs degrade Tesseract.
    patch=patch.resize((max(1,round(patch.width*64/patch.height)),64))
    canvas=Image.new('L',(patch.width+32,patch.height+32),255);canvas.paste(patch,(16,16))
    # The full numeric field is read; digit repair, rounding, dropping an
    # internal dot, and borrowing another amount are all forbidden.
    text=pytesseract.image_to_string(canvas,lang='jpn+eng',config='--psm 7',timeout=20)
    value=compact(text).replace('Y','¥') if compact(text).startswith('Y') else compact(text)
    # English OCR may omit the Japanese currency suffix, but must preserve
    # every digit and separator. No general removal of nonnumeric content.
    return amount_text(value)==expected

def read_local_payment(image,observations,fields,provenance):
    if not all(fields.get(k) for k in ('date','issuer','category')):return None,'local_fields_incomplete'
    if not provenance.get('date_evidence_verified') or provenance.get('date_candidates')!=1:return None,'local_date_unverified'
    if provenance.get('issuer_status')!='SELECTED_ISSUER':return None,'local_issuer_unverified'
    texts=[compact(t['text']) for t in observations]
    headings=[t for t in texts if re.fullmatch(r'(?:(?:診療費|医療費|調剤|薬剤費|請求書兼))*領収[書証]',t)]
    numbered=[t for t in texts if re.fullmatch(r'領収[書証](?:No\.?|NO\.?|番号)\d*',t)]
    if len(headings)>1 or (not headings and len(numbered)!=1):return None,'paid_receipt_missing'
    if any(any(word in t for word in ('取消','無効','返金','未払い','未払','請求書のみ','一部入金','分割','部分入金','前受金')) for t in texts):
        return None,'payment_qualifier_present'
    pairs,reason=candidate_pairs(observations)
    if reason:return None,reason
    try:
        if not all(confirm_digits(image,token['box'],amount) for _,token,amount in pairs):return None,'local_digits_disagree'
    except (RuntimeError,TimeoutError):return None,'local_digits_unavailable'
    amount=pairs[0][2];category=fields['category'].split('｜')
    if len(category)!=2 or category[0]!='医療・保険':return None,'local_category_invalid'
    parsed=ReceiptResult(date=fields['date'],merchant=fields['issuer'],total=amount,payment_method='',
        items=[ReceiptItem(name='調剤薬代' if category[1]=='薬' else '医療費',amount=amount,
            major_category=category[0],minor_category=category[1],note='原本をローカルOCRで照合')])
    return parsed,''

def apply_local(review,source,folder,parsed,provenance):
    """Immediate, source-checked local decision; never replay a saved reading."""
    from .receipt_confirmation import review_id
    from .medical_auto_posting import owner_blocked,possible_duplicate
    from .receipt_validation import validate_receipt_result
    key=review_id('medical',source);old=review.items[key]
    if old['status']!='waiting' or old.get('error') or owner_blocked(source,review.store.value):return False
    binding=provenance.get('document_binding',{})
    if binding.get('source_sha256')!=source['sha256']:return False
    if not validate_receipt_result(parsed,review.db.categories())[0]:return False
    live=review.ui_rows().get(key)
    if live is None or any(live[1][7:15]):return False
    if possible_duplicate(parsed,review.tables()):return False
    review.verify_source(source,folder)
    try:plan=review._plan(old,parsed,'',automatic=True)
    except ValueError:return False
    # Correct the origin text from the older cloud-amount policy.
    for _,row in plan:
        for i,value in enumerate(row):
            if isinstance(value,str):row[i]=value.replace('medical-auto-v1',POLICY)
    item=deepcopy(old);item.update(status='pending',plan=plan,decision_origin='automatic',
        local_decision={'policy':POLICY,'source':deepcopy(source),'parsed':parsed.model_dump(),
                        'evidence':provenance,'external_requests':0})
    review.save_item(key,item)
    from .drive_run_state import StateError
    try:review.verify_source(source,folder)
    except StateError as error:
        if str(error)!='confirmation_source_changed':raise
        item.update(status='waiting',automatic_hold='source_changed',aborted_before_accounting=True)
        item.pop('plan',None);review.save_item(key,item);return False
    current=review.ui_rows().get(key)
    if current is None or any(current[1][7:15]) or current!=live:
        item.update(status='waiting',aborted_before_accounting=True);item.pop('plan',None)
        review.save_item(key,item);return False
    review._write_accounting_plan(key,item)
    return True


def archive_local(review,source,folder,processed_folder,drive):
    """Move only a read-back-complete local decision, preserving metadata."""
    from .receipt_confirmation import review_id
    from .drive_run_state import StateError
    from datetime import datetime,timezone
    item=review.items[review_id('medical',source)]
    if item['status']!='applied' or not item.get('local_decision'):return
    if not review._complete(item['plan']):raise StateError('confirmation_readback_mismatch')
    meta=drive.files().get(fileId=source['source_id'],fields='parents,version,mimeType,trashed,appProperties',supportsAllDrives=True).execute(num_retries=0)
    if meta.get('trashed') or meta.get('parents')!=[folder] or meta.get('version')!=source['version'] or meta.get('mimeType')!=source['mime_type']:
        raise StateError('confirmation_source_changed')
    drive.files().update(fileId=source['source_id'],addParents=processed_folder,removeParents=folder,
        body={'appProperties':{**meta.get('appProperties',{}),'kakeiboReceiptClass':'medical',
            'kakeiboProcessedAt':datetime.now(timezone.utc).isoformat()}},fields='id,parents',supportsAllDrives=True).execute(num_retries=0)
