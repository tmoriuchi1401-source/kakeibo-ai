"""Non-AI payment-cell isolation. Originals and OCR never leave this process.

Reuses the checkpoint's label-anchor / smallest enclosure / fresh PNG approach.
Unlike its ten-document coordinate plan, every page supplies its own enclosure.
OCR absence is not clearance: retained ink must be accounted for by positively
recognized allowed glyphs and a closed payment cell. Anything else is held.
"""
from dataclasses import dataclass, field
from hashlib import sha256
from io import BytesIO
import re
import struct
import unicodedata
import math

from PIL import Image

VERSION = 'medical-payment-cell-v4'
LABELS = ('領収金額', '領収額', 'お支払金額', 'お支払額', '支払金額', '支払額', '今回入金額')
# These positively verified cells are classified locally, never sent or treated
# as a current payment. Generic totals/charges remain unresolved candidates.
NONPAYMENT_LABELS=('前回入金額','累計入金額','未収金額','未収額','預り金','お預り金','釣銭','お釣り')
# Discovery cues are deliberately broader than final outbound content checks.
# Checkpoint 6d31751 used partial, near and split labels, including generic totals.
MONEY_CUES = ('領収', '入金', '支払', '請求', '合計', '金額', '料金', '会計', '精算',
              '負担', '未収', '預り', '預かり', '釣銭', '返金', '残額')
NEAR_LABELS = ('領収金額', '請求額', '支払額')
PAYMENT = re.compile(r'('+'|'.join(LABELS)+r')[¥￥]?[0-9][0-9,，]{0,10}円?')
MAX_PIXELS = 20_000_000


class AnonymizationHold(ValueError):
    """Only fixed reason codes, never rejected text or image metadata."""


def compact(text):
    return ''.join(unicodedata.normalize('NFKC', text).split())


def png(image):
    # A newly allocated canvas strips EXIF, profiles, comments and PDF layers.
    clean = Image.new('RGB', image.size, 'white')
    clean.paste(image.convert('RGB'))
    stream = BytesIO(); clean.save(stream, format='PNG')
    return stream.getvalue()


def validate_png(payload):
    if not isinstance(payload, bytes) or len(payload)>5_000_000 or payload[:8]!=b'\x89PNG\r\n\x1a\n':
        raise AnonymizationHold('derived_png_invalid')
    pos=8; kinds=[]
    while pos<len(payload):
        if pos+12>len(payload):raise AnonymizationHold('derived_png_invalid')
        length=struct.unpack('>I',payload[pos:pos+4])[0]
        kind=payload[pos+4:pos+8];kinds.append(kind);pos+=12+length
        if kind not in {b'IHDR',b'IDAT',b'IEND'} or pos>len(payload):
            raise AnonymizationHold('derived_metadata_forbidden')
    if pos!=len(payload) or not kinds or kinds[0]!=b'IHDR' or kinds[-1]!=b'IEND':
        raise AnonymizationHold('derived_png_invalid')
    with Image.open(BytesIO(payload)) as image:
        image.load()
        if image.mode!='RGB' or image.width*image.height>MAX_PIXELS or image.info:
            raise AnonymizationHold('derived_png_invalid')
        return image.copy()


def render_single_page(payload, mime):
    if mime=='application/pdf':
        from pypdf import PdfReader
        from .receipt_text_extraction import _suppress_pypdf_output
        with _suppress_pypdf_output():
            reader=PdfReader(BytesIO(payload))
            if reader.is_encrypted or len(reader.pages)!=1:
                raise AnonymizationHold('page_mapping_requires_review')
            if int(reader.pages[0].get('/Rotate',0))%360:
                raise AnonymizationHold('rotation_requires_review')
        import pypdfium2 as pdfium
        document=pdfium.PdfDocument(payload)
        try:
            page=document[0]
            try:
                area=page.get_width()*page.get_height()
                if not math.isfinite(area) or area<=0:
                    raise AnonymizationHold('image_size_exceeded')
                # Large scanner page units do not imply multiple receipts.
                # Retain the same bounded pixel budget at an adaptive scale.
                scale=min(3,math.sqrt(MAX_PIXELS*.98/area))
                bitmap=page.render(scale=scale)
                try:image=bitmap.to_pil().convert('RGB')
                finally:bitmap.close()
            finally:page.close()
        finally:document.close()
    elif mime in {'image/png','image/jpeg','image/jpg'}:
        with Image.open(BytesIO(payload)) as opened:
            if getattr(opened,'n_frames',1)!=1:
                raise AnonymizationHold('page_mapping_requires_review')
            if opened.getexif().get(274,1)!=1:
                raise AnonymizationHold('rotation_requires_review')
            if opened.width*opened.height>MAX_PIXELS:
                raise AnonymizationHold('image_size_exceeded')
            image=opened.convert('RGB')
    else:raise AnonymizationHold('mime_not_supported')
    return image


def tokens(image, psm=6):
    import pytesseract
    data=pytesseract.image_to_data(image,lang='jpn+eng',config=f'--psm {psm}',timeout=30,output_type=pytesseract.Output.DICT)
    return [dict(text=str(t),box=(int(data['left'][i]),int(data['top'][i]),
        int(data['left'][i]+data['width'][i]),int(data['top'][i]+data['height'][i])),
        confidence=float(data['conf'][i]),line=(data['block_num'][i],data['par_num'][i],data['line_num'][i]))
        for i,t in enumerate(data['text']) if str(t).strip()]


def _edit_distance(left,right):
    # Pure label comparison recovered from the checkpoint's candidate discovery.
    row=list(range(len(right)+1))
    for i,a in enumerate(left,1):
        next_row=[i]
        for j,b in enumerate(right,1):
            next_row.append(min(next_row[-1]+1,row[j]+1,row[j-1]+(a!=b)))
        row=next_row
    return row[-1]


def money_label_cue(value):
    """Local candidate only. This never approves pixels, payment or transmission."""
    value=''.join(c for c in compact(value) if c not in ':：()（）[]【】')
    if any(cue in value for cue in MONEY_CUES):return True
    for label in NEAR_LABELS:
        width=len(label)
        if len(value)>=width-1 and any(_edit_distance(value[i:i+width],label)<=1
                for i in range(max(1,len(value)-width+1))):return True
    return False


def _fragment_follows(left,right):
    # Bounded horizontal/vertical reconstruction, not general text guessing.
    a,b=left['box'],right['box']
    ah,bh=a[3]-a[1],b[3]-b[1];aw,bw=a[2]-a[0],b[2]-b[0]
    row_overlap=max(0,min(a[3],b[3])-max(a[1],b[1]))
    col_overlap=max(0,min(a[2],b[2])-max(a[0],b[0]))
    return ((a[0]<=b[0] and row_overlap>=.25*min(ah,bh) and b[0]-a[2]<=8*max(ah,bh))
        or (a[1]<=b[1] and col_overlap>=.2*min(aw,bw) and b[1]-a[3]<=5*max(ah,bh)))


def anchors(observations, *, details=None):
    # Do not discard uncertain money cues before the independent pixel checks.
    found={}
    def add(selected):
        text=compact(''.join(t['text'] for t in selected))
        if not money_label_cue(text):return
        boxes=[t['box'] for t in selected]
        box=(min(b[0] for b in boxes),min(b[1] for b in boxes),max(b[2] for b in boxes),max(b[3] for b in boxes))
        found[(text,box)]=(text,box)
        if details is not None:
            details[(text,box)]=tuple(observations.index(t) for t in selected)
    for token in observations:add([token])
    fragments=[t for t in observations if 0<len(compact(t['text']))<=4 and not re.search(r'\d',t['text'])]
    for i,first in enumerate(fragments):
        for j in range(i+1,len(fragments)):
            second=fragments[j]
            if not _fragment_follows(first,second):continue
            # If either fragment already identifies a money label, keep its
            # smaller box instead of annexing unrelated adjacent text.
            if money_label_cue(first['text']) or money_label_cue(second['text']):
                if compact(first['text']+second['text']) in LABELS+MONEY_CUES:add([first,second])
                continue
            add([first,second])
            for third in fragments[j+1:]:
                if (not money_label_cue(third['text']) and _fragment_follows(second,third)
                        and sum(len(compact(t['text'])) for t in (first,second,third))<=8):
                    add([first,second,third])
    # A money cue in the middle token must not truncate a split full label
    # (e.g. 今回 / 入金 / 額). Only complete lexical labels extend this pass;
    # uncertain/partial cues from above are retained as candidates too.
    vocabulary=LABELS+NONPAYMENT_LABELS+MONEY_CUES
    groups={}
    for token in observations:groups.setdefault(tuple(token['line']),[]).append(token)
    for group in groups.values():
        group.sort(key=lambda t:t['box'][0])
        for start in range(len(group)):
            for stop in range(start+2,min(len(group),start+6)+1):
                selected=group[start:stop]
                if (compact(''.join(t['text'] for t in selected)) in vocabulary
                        and all(_fragment_follows(a,b) for a,b in zip(selected,selected[1:]))):
                    add(selected)
    return list(found.values())


def enclosure(image, box):
    """Find a closed ruled cell around the label; never use a source template."""
    import numpy as np
    ink=np.asarray(image.convert('L'))<170
    x1,y1,x2,y2=box; h=max(1,y2-y1)
    # The cell may contain the label and amount, but no other table row.
    top=[y for y in range(max(0,y1-3*h),y1) if ink[y,x1:x2].mean()>.94]
    bottom=[y for y in range(y2,min(image.height,y2+3*h)) if ink[y,x1:x2].mean()>.94]
    def reject(detail):
        error=AnonymizationHold('payment_cell_boundary_unknown');error.boundary_reason=detail;raise error
    if not top or not bottom:reject('top_and_bottom_missing' if not top and not bottom else 'top_missing' if not top else 'bottom_missing')
    top=max(top);bottom=min(bottom)
    if bottom-top>5*h:reject('row_span_exceeded')
    left=[x for x in range(max(0,x1-4*h),x1) if ink[top:bottom+1,x].mean()>.94]
    right=[x for x in range(x2,min(image.width,x2+24*h)) if ink[top:bottom+1,x].mean()>.94]
    if not left or not right:reject('left_and_right_missing' if not left and not right else 'left_missing' if not left else 'right_missing')
    left=max(left);right=min(right)
    if right-left<2*h or not all(ink[y,left:right+1].mean()>.94 for y in (top,bottom)):
        reject('horizontal_continuity_or_width')
    return left+2,top+2,right-1,bottom-1


def verify_cell_pixels(image, *, observations=None, glyphs=None, classify_nonpayment=False, psm=7):
    """Positive content grammar plus complete ink coverage, not a PII blacklist.

    Does not accept/return an OCR amount. Digit identity may be wrong: the AI
    reads the original retained pixels. Non-digit/extra/unknown ink is held.
    """
    import numpy as np
    import pytesseract
    observations=tokens(image,psm) if observations is None else observations
    if not observations:
        raise AnonymizationHold('cell_content_not_verified')
    ordered=sorted(observations,key=(lambda t:(t['line'],t['box'][0])) if psm==6 else lambda t:t['box'][0])
    numeric_tokens=sum(bool(re.search(r'\d',compact(t['text']))) for t in ordered)
    if not numeric_tokens:raise AnonymizationHold('numeric_region_not_verified')
    if numeric_tokens!=1:
        raise AnonymizationHold('multiple_numeric_regions')
    text=compact(''.join(t['text'] for t in ordered))
    labels=LABELS+NONPAYMENT_LABELS if classify_nonpayment else LABELS
    grammar=re.compile(r'('+'|'.join(labels)+r')[¥￥]?[0-9][0-9,，]{0,10}円?')
    if not grammar.fullmatch(text):raise AnonymizationHold('cell_content_not_allowed')
    observed_label=next(label for label in labels if text.startswith(label))
    pos=0
    for token in ordered:
        # Exact label recognition is necessary. Low confidence about which
        # digit is printed does not pre-empt Gemini's reading of the pixels.
        if pos<len(observed_label) and token['confidence']<65:
            raise AnonymizationHold('payment_label_not_verified')
        pos+=len(compact(token['text']))
    if glyphs is None:
        raw=pytesseract.image_to_boxes(image,lang='jpn+eng',config=f'--psm {psm}',timeout=30)
        glyphs=[]
        for row in raw.splitlines():
            char,left,bottom,right,top,_=row.split()
            glyphs.append((char,(int(left),image.height-int(top),int(right),image.height-int(bottom))))
    observed=compact(''.join(char for char,_ in glyphs))
    if not grammar.fullmatch(observed):raise AnonymizationHold('glyph_content_not_allowed')
    label=next(label for label in labels if observed.startswith(label))
    if not text.startswith(label):raise AnonymizationHold('payment_label_conflict')
    number_boxes=[box for char,box in glyphs if char in '0123456789,，']
    for left,right in zip(number_boxes,number_boxes[1:]):
        height=max(left[3]-left[1],right[3]-right[1])
        if right[0]-left[2]>.65*height or abs((left[1]+left[3]-right[1]-right[3])/2)>.5*height:
            raise AnonymizationHold('multiple_numeric_regions')
    covered=np.zeros((image.height,image.width),dtype=bool);label_irregular=False
    for index,(char,(l,t,r,b)) in enumerate(glyphs):
        if not (0<=l<r<=image.width and 0<=t<b<=image.height):
            raise AnonymizationHold('glyph_geometry_unknown')
        irregular=r-l>2*(b-t) or covered[t:b,l:r].mean()>.1
        if irregular and index>=len(label):raise AnonymizationHold('glyph_geometry_unknown')
        label_irregular=label_irregular or irregular
        covered[max(0,t-1):min(image.height,b+1),max(0,l-1):min(image.width,r+1)]=True
    # Japanese OCR can split a printed label's strokes between adjacent glyph
    # boxes. Cover only the compact, twice-recognized exact allowed label span;
    # never bridge the whitespace between a label and a number/other field.
    label_boxes=[box for _,box in glyphs[:len(label)]]
    for left,right in zip(label_boxes,label_boxes[1:]):
        if right[0]-left[2]>.8*max(left[3]-left[1],right[3]-right[1]):
            raise AnonymizationHold('label_geometry_unknown')
    l=min(b[0] for b in label_boxes);t=min(b[1] for b in label_boxes)
    r=max(b[2] for b in label_boxes);b=max(b[3] for b in label_boxes)
    if label_irregular:
        # Japanese OCR may assign overlapping/wide character boxes within an
        # otherwise exact label. Validate that word as its own bounded region;
        # do not relax numeric geometry or let it cover another field.
        height=max(box[3]-box[1] for box in label_boxes)
        if (r-l>(len(label)+1)*height or b-t>1.6*height
                or any(right[0]<left[0] for left,right in zip(label_boxes,label_boxes[1:]))
                or any(max(l,a)<min(r,c) and max(t,d)<min(b,e) for _,(a,d,c,e) in glyphs[len(label):])):
            raise AnonymizationHold('label_geometry_unknown')
        pad=max(8,round(height*.25));word=Image.new('RGB',(r-l+2*pad,b-t+2*pad),'white')
        word.paste(image.crop((l,t,r,b)),(pad,pad))
        checked=tokens(word,7)
        if (not checked or min(x['confidence'] for x in checked)<65
                or compact(''.join(x['text'] for x in checked))!=label):
            raise AnonymizationHold('label_geometry_unknown')
    covered[max(0,t-1):min(image.height,b+1),max(0,l-1):min(image.width,r+1)]=True
    # Stray text, QR/barcodes, signatures and unrecognized pixels outside the
    # positively classified glyph extents are not silently dropped from a crop.
    ink=np.asarray(image.convert('L'))<190
    if (ink & ~covered).any():raise AnonymizationHold('unaccounted_cell_ink')
    return label


@dataclass(frozen=True)
class PaymentCrop:
    payload: bytes = field(repr=False)
    mapping: dict = field(repr=False)
    label: str
    accounting_evaluation: dict | None = field(default=None,repr=False)


def prepare_payment_crop(payload, mime, *, image=None, observations=None, automatic=False):
    image=render_single_page(payload,mime) if image is None else image
    observations=tokens(image) if observations is None else observations
    boxes=set();failures=[];verified=[];excluded=[]
    discovered=anchors(observations)
    if automatic:
        return _automatic_crop(payload,image,observations,discovered)
    for _,anchor in discovered:
        try:boxes.add(enclosure(image,anchor))
        except AnonymizationHold as error:failures.append(str(error))
    for box in sorted(boxes):
        # Inspect every distinct cell, including after an unresolved enclosure.
        # Validate exactly the pixels that would be sent.
        crop=image.crop(box).convert('L').point(lambda value:0 if value<190 else 255).convert('RGB')
        try:
            label=verify_cell_pixels(crop,classify_nonpayment=True) if automatic else verify_cell_pixels(crop)
            if label in NONPAYMENT_LABELS:excluded.append(label)
            else:verified.append((box,crop,label))
        except AnonymizationHold as error:failures.append(str(error))
    # Never discard an unresolved candidate and promote the survivor to truth.
    if failures or len(verified)!=1:
        error=AnonymizationHold(failures[0] if failures else 'payment_region_ambiguous_or_absent')
        error.candidate_checks={'discovered':len(discovered),'enclosed':len(boxes),'verified':len(verified),
            'excluded_nonpayment':len(excluded),'unresolved_reasons':failures,'multiple_verified':len(verified)>1}
        raise error
    box,crop,label=verified[0]
    clean=png(crop);validate_png(clean)
    return PaymentCrop(clean,{'source_sha256':sha256(payload).hexdigest(),
        'source_image_sha256':sha256(png(image)).hexdigest(),'page':1,'unit':1,
        'crop_coordinates_original':list(box),'rotation_clockwise_degrees':0,
        'crop_sha256':sha256(clean).hexdigest(),'preprocessor':VERSION,
        'rendered_page_size':list(image.size),
        'validation':'closed_payment_cell_positive_glyphs_complete_ink','metadata_removed':True,
        **({'excluded_nonpayment_cells':len(excluded)} if automatic else {})},label)


def _automatic_crop(payload,image,observations,discovered):
    from .medical_text_regions import text_regions,ruled_regions,adjacent_ruled_regions,observed_row_regions,separated_row_regions
    # Detection-only contrast helps split light table text. Outbound pixels
    # always come from the original render, never this detection image.
    detector=image.convert('L').point(lambda value:0 if value<170 else 255).convert('RGB')
    detection_tokens=tokens(detector,11);extra=anchors(detection_tokens)
    cues=list(dict.fromkeys((text,tuple(box)) for text,box in discovered+extra))
    proposals={};retained={};by_anchor=[];boundary_failures=0;boundary_reasons={}
    for _,anchor in cues:
        candidate_boxes=[]
        try:
            box=enclosure(image,anchor);proposals[box]='closed_payment_cell_positive_glyphs_complete_ink'
            candidate_boxes.append(box)
        except AnonymizationHold as error:
            boundary_failures+=1;reason=getattr(error,'boundary_reason','unknown')
            boundary_reasons[reason]=boundary_reasons.get(reason,0)+1
        try:
            for box in ruled_regions(image,anchor):
                proposals.setdefault(box,'tolerant_ruled_cell_positive_glyphs_complete_ink');candidate_boxes.append(box)
        except AnonymizationHold:pass
        try:
            segments=adjacent_ruled_regions(image,anchor)
            box=(segments[0][0],min(b[1] for b in segments),segments[1][2],max(b[3] for b in segments))
            proposals[box]='adjacent_ruled_cells_positive_glyphs_complete_ink';retained[box]=segments
            candidate_boxes.append(box)
        except AnonymizationHold:pass
        for box in text_regions(image,anchor):
            proposals.setdefault(box,'whitespace_text_region_positive_glyphs_complete_ink')
            candidate_boxes.append(box)
        for box in text_regions(image,anchor,below=True):
            proposals.setdefault(box,'whitespace_text_region_positive_glyphs_complete_ink')
            candidate_boxes.append(box)
        for box in observed_row_regions(image,anchor,observations+detection_tokens):
            proposals.setdefault(box,'whitespace_text_region_positive_glyphs_complete_ink');candidate_boxes.append(box)
        for segments in separated_row_regions(image,anchor,observations+detection_tokens):
            box=(segments[0][0],min(b[1] for b in segments),segments[1][2],max(b[3] for b in segments))
            proposals[box]='ruled_separator_text_fields_positive_glyphs_complete_ink';retained[box]=segments
            candidate_boxes.append(box)
        by_anchor.append((anchor,candidate_boxes))
    if len(proposals)>256:
        error=AnonymizationHold('candidate_geometry_limit');error.candidate_checks={'proposed_regions':len(proposals),'verified':0};raise error
    checked={};verified={};excluded={}
    for box,validation in sorted(proposals.items()):
        if box in retained:
            crop=Image.new('RGB',(box[2]-box[0],box[3]-box[1]),'white')
            for segment in retained[box]:crop.paste(image.crop(segment),(segment[0]-box[0],segment[1]-box[1]))
        else:crop=image.crop(box)
        crop=crop.convert('L').point(lambda value:0 if value<190 else 255).convert('RGB')
        padding=max(8,round(crop.height*.25))
        canvas=Image.new('RGB',(crop.width+2*padding,crop.height+2*padding),'white')
        canvas.paste(crop,(padding,padding));crop=canvas
        clean=png(crop)
        try:
            # Reopen and check the actual final bytes; never regenerate them.
            final_image=validate_png(clean)
            try:label=verify_cell_pixels(final_image,classify_nonpayment=True)
            except AnonymizationHold:
                label=verify_cell_pixels(final_image,classify_nonpayment=True,psm=6)
            target=excluded if label in NONPAYMENT_LABELS else verified
            target[box]=(clean,label,validation);checked[box]='verified'
        except AnonymizationHold as error:checked[box]=str(error)
    # Nested proposals of the same retained ink represent one physical field.
    unique=[]
    def ink_digest(payload):
        import numpy as np
        im=validate_png(payload);ys,xs=np.where(np.asarray(im.convert('L'))<190)
        return sha256(png(im.crop((int(xs.min()),int(ys.min()),int(xs.max())+1,int(ys.max())+1)))).digest()
    for box,result in sorted(verified.items(),key=lambda pair:-(pair[0][2]-pair[0][0])*(pair[0][3]-pair[0][1])):
        # Keep the maximal verified field, including its optional currency
        # suffix, instead of counting a nested shorter proposal a second time.
        if any(result[1]==old[1][1] and (_contains(old[0],box) or
                (_overlapping(box,old[0]) and ink_digest(result[0])==ink_digest(old[1][0]))) for old in unique):continue
        unique.append((box,result))
    resolved=list(verified)+list(excluded)
    unresolved=[]
    for anchor,attempts in by_anchor:
        if any(_contains(box,anchor) for box in resolved):continue
        if any(_overlapping(anchor,other) for other,_ in unresolved):continue
        reasons=[checked[b] for b in attempts if b in checked]
        unresolved.append((anchor,reasons[0] if reasons else 'text_region_boundary_unknown'))
    checks={'discovered':len(discovered),'additional_cues':len(extra),'distinct_cues':len(cues),
        'proposed_regions':len(proposals),'closed_boundary_failures':boundary_failures,'boundary_reasons':boundary_reasons,
        'proposals_by_geometry':{kind:sum(v==kind for v in proposals.values()) for kind in sorted(set(proposals.values()))},
        'verified':len(unique),'excluded_nonpayment':len(excluded),
        'unresolved_reasons':[reason for _,reason in unresolved],'multiple_verified':len(unique)>1}
    if not unique:
        error=AnonymizationHold(checks['unresolved_reasons'][0] if unresolved else 'payment_region_ambiguous_or_absent')
        error.candidate_checks=checks;raise error
    # This selects an image to read, not the document's accounting answer.
    # Cutting a field and deciding its accounting role are separate checks.
    box,(clean,label,validation)=unique[0]
    mapping={'source_sha256':sha256(payload).hexdigest(),
        'source_image_sha256':sha256(png(image)).hexdigest(),'page':1,'unit':1,
        'crop_coordinates_original':list(box),'rotation_clockwise_degrees':0,
        'crop_sha256':sha256(clean).hexdigest(),'preprocessor':VERSION,
        'rendered_page_size':list(image.size),'validation':validation,'metadata_removed':True,
        'unresolved_candidates':len(unresolved),'verified_payment_cells':len(unique),
        'candidate_checks':checks,'geometry_policy':'bounded-text-regions-v1',
        'retained_regions_original':[list(region) for region in retained.get(box,(box,))],
        'derived_padding_pixels':max(8,round((box[3]-box[1])*.25))}
    from .medical_accounting_roles import evaluate_roles
    evaluation=evaluate_roles((observations,detection_tokens),cues,unresolved,mapping,label,image=image)
    return PaymentCrop(clean,mapping,label,evaluation)


def _contains(outer,inner):
    return outer[0]<=inner[0] and outer[1]<=inner[1] and outer[2]>=inner[2] and outer[3]>=inner[3]


def _overlapping(a,b):
    intersection=max(0,min(a[2],b[2])-max(a[0],b[0]))*max(0,min(a[3],b[3])-max(a[1],b[1]))
    return intersection>=.8*min((a[2]-a[0])*(a[3]-a[1]),(b[2]-b[0])*(b[3]-b[1]))
