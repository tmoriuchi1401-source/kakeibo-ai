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
    data=pytesseract.image_to_data(image,lang='jpn+eng',config=f'--psm {psm}',output_type=pytesseract.Output.DICT)
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


def anchors(observations):
    # Do not discard uncertain money cues before the independent pixel checks.
    found={}
    def add(selected):
        text=compact(''.join(t['text'] for t in selected))
        if not money_label_cue(text):return
        boxes=[t['box'] for t in selected]
        box=(min(b[0] for b in boxes),min(b[1] for b in boxes),max(b[2] for b in boxes),max(b[3] for b in boxes))
        found[(text,box)]=(text,box)
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
    return list(found.values())


def enclosure(image, box):
    """Find a closed ruled cell around the label; never use a source template."""
    import numpy as np
    ink=np.asarray(image.convert('L'))<170
    x1,y1,x2,y2=box; h=max(1,y2-y1)
    # The cell may contain the label and amount, but no other table row.
    top=[y for y in range(max(0,y1-3*h),y1) if ink[y,x1:x2].mean()>.94]
    bottom=[y for y in range(y2,min(image.height,y2+3*h)) if ink[y,x1:x2].mean()>.94]
    if not top or not bottom:raise AnonymizationHold('payment_cell_boundary_unknown')
    top=max(top);bottom=min(bottom)
    if bottom-top>5*h:raise AnonymizationHold('payment_cell_boundary_unknown')
    left=[x for x in range(max(0,x1-4*h),x1) if ink[top:bottom+1,x].mean()>.94]
    right=[x for x in range(x2,min(image.width,x2+24*h)) if ink[top:bottom+1,x].mean()>.94]
    if not left or not right:raise AnonymizationHold('payment_cell_boundary_unknown')
    left=max(left);right=min(right)
    if right-left<2*h or not all(ink[y,left:right+1].mean()>.94 for y in (top,bottom)):
        raise AnonymizationHold('payment_cell_boundary_unknown')
    return left+2,top+2,right-1,bottom-1


def verify_cell_pixels(image, *, observations=None, glyphs=None):
    """Positive content grammar plus complete ink coverage, not a PII blacklist.

    Does not accept/return an OCR amount. Digit identity may be wrong: the AI
    reads the original retained pixels. Non-digit/extra/unknown ink is held.
    """
    import numpy as np
    import pytesseract
    observations=tokens(image,7) if observations is None else observations
    if not observations or min(t['confidence'] for t in observations)<65:
        raise AnonymizationHold('cell_content_not_verified')
    ordered=sorted(observations,key=lambda t:t['box'][0])
    if sum(bool(re.search(r'\d',compact(t['text']))) for t in ordered)!=1:
        raise AnonymizationHold('multiple_numeric_regions')
    text=compact(''.join(t['text'] for t in ordered))
    if not PAYMENT.fullmatch(text):raise AnonymizationHold('cell_content_not_allowed')
    if glyphs is None:
        raw=pytesseract.image_to_boxes(image,lang='jpn+eng',config='--psm 7')
        glyphs=[]
        for row in raw.splitlines():
            char,left,bottom,right,top,_=row.split()
            glyphs.append((char,(int(left),image.height-int(top),int(right),image.height-int(bottom))))
    observed=compact(''.join(char for char,_ in glyphs))
    if not PAYMENT.fullmatch(observed):raise AnonymizationHold('glyph_content_not_allowed')
    label=next(label for label in LABELS if observed.startswith(label))
    if not text.startswith(label):raise AnonymizationHold('payment_label_conflict')
    number_boxes=[box for char,box in glyphs if char in '0123456789,，']
    for left,right in zip(number_boxes,number_boxes[1:]):
        height=max(left[3]-left[1],right[3]-right[1])
        if right[0]-left[2]>.65*height or abs((left[1]+left[3]-right[1]-right[3])/2)>.5*height:
            raise AnonymizationHold('multiple_numeric_regions')
    covered=np.zeros((image.height,image.width),dtype=bool)
    for char,(l,t,r,b) in glyphs:
        if not (0<=l<r<=image.width and 0<=t<b<=image.height) or r-l>2*(b-t):
            raise AnonymizationHold('glyph_geometry_unknown')
        if covered[t:b,l:r].mean()>.1:raise AnonymizationHold('glyph_geometry_unknown')
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


def prepare_payment_crop(payload, mime, *, image=None, observations=None):
    image=render_single_page(payload,mime) if image is None else image
    observations=tokens(image) if observations is None else observations
    boxes=set();failures=[];verified=[]
    for _,anchor in anchors(observations):
        try:boxes.add(enclosure(image,anchor))
        except AnonymizationHold as error:failures.append(str(error))
    for box in sorted(boxes):
        # Inspect every distinct cell, including after an unresolved enclosure.
        # Validate exactly the pixels that would be sent.
        crop=image.crop(box).convert('L').point(lambda value:0 if value<190 else 255).convert('RGB')
        try:verified.append((box,crop,verify_cell_pixels(crop)))
        except AnonymizationHold as error:failures.append(str(error))
    # Never discard an unresolved candidate and promote the survivor to truth.
    if failures:raise AnonymizationHold(failures[0])
    if len(verified)!=1:raise AnonymizationHold('payment_region_ambiguous_or_absent')
    box,crop,label=verified[0]
    clean=png(crop);validate_png(clean)
    return PaymentCrop(clean,{'source_sha256':sha256(payload).hexdigest(),
        'source_image_sha256':sha256(png(image)).hexdigest(),'page':1,'unit':1,
        'crop_coordinates_original':list(box),'rotation_clockwise_degrees':0,
        'crop_sha256':sha256(clean).hexdigest(),'preprocessor':VERSION,
        'rendered_page_size':list(image.size),
        'validation':'closed_payment_cell_positive_glyphs_complete_ink','metadata_removed':True},label)
