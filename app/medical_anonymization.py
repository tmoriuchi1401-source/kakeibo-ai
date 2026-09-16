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

from PIL import Image

VERSION = 'medical-payment-cell-v1'
LABELS = ('領収金額', '領収額', 'お支払金額', 'お支払額', '支払金額', '支払額')
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
                if page.get_width()*page.get_height()*9>MAX_PIXELS:
                    raise AnonymizationHold('image_size_exceeded')
                bitmap=page.render(scale=3)
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


def anchors(observations):
    # Checkpoint's split label reconstruction, bounded to one OCR line.
    groups={}
    for token in observations:groups.setdefault(token['line'],[]).append(token)
    found=[]
    for group in groups.values():
        group.sort(key=lambda t:t['box'][0])
        for start in range(len(group)):
            for count in range(1,min(8,len(group)-start)+1):
                selected=group[start:start+count];text=compact(''.join(t['text'] for t in selected))
                if text not in LABELS or min(t['confidence'] for t in selected)<70:continue
                boxes=[t['box'] for t in selected]
                found.append((text,(min(b[0] for b in boxes),min(b[1] for b in boxes),max(b[2] for b in boxes),max(b[3] for b in boxes))))
    return found


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
    boxes={enclosure(image,box) for _,box in anchors(observations)}
    if len(boxes)!=1:raise AnonymizationHold('payment_region_ambiguous_or_absent')
    box=next(iter(boxes))
    # Validate exactly the pixels that will be sent. Faint marks ignored by the
    # ink test must not survive in a colour/grayscale outbound image.
    crop=image.crop(box).convert('L').point(lambda value:0 if value<190 else 255).convert('RGB')
    label=verify_cell_pixels(crop)
    clean=png(crop);validate_png(clean)
    return PaymentCrop(clean,{'source_sha256':sha256(payload).hexdigest(),
        'source_image_sha256':sha256(png(image)).hexdigest(),'page':1,'unit':1,
        'crop_coordinates_original':list(box),'rotation_clockwise_degrees':0,
        'crop_sha256':sha256(clean).hexdigest(),'preprocessor':VERSION,
        'validation':'closed_payment_cell_positive_glyphs_complete_ink','metadata_removed':True},label)
