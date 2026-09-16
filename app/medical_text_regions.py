"""Whitespace-bounded candidate geometry, independent of OCR amount accuracy.

These boxes are proposals only. Complete positive glyph/ink checks on the final
PNG remain mandatory. No coordinates, layouts or amounts identify a source.
"""
import numpy as np


def runs(mask):
    edges=np.flatnonzero(np.diff(np.r_[False,mask,False].astype(np.int8)))
    return [(int(a),int(b)) for a,b in zip(edges[::2],edges[1::2])]


def _bridge(mask):
    mask=mask.copy()
    for a,b in runs(~mask):
        if a>0 and b<len(mask) and b-a<=2:mask[a:b]=True
    return mask


def ruled_regions(image,anchor):
    """Nearest connected four-sided cell with two-pixel line tolerance.

    The horizontal runs bound the side search, instead of assuming that a
    right-aligned label lies within four glyph heights of the left border.
    Dilation/bridging is detection-only. No altered pixels enter a PNG.
    """
    from .medical_anonymization import AnonymizationHold
    x1,y1,x2,y2=anchor;h=y2-y1
    if h<5:raise AnonymizationHold('ruled_cell_not_connected')
    ink=np.asarray(image.convert('L'))<190
    def horizontal(y):return _bridge(ink[max(0,y-2):min(image.height,y+3)].any(axis=0))
    start=max(0,y1-3*h)
    tops=[start+b-1 for a,b in runs(np.array([horizontal(y)[x1:x2].all() for y in range(start,y1)]))]
    bottoms=[y2+a for a,b in runs(np.array([horizontal(y)[x1:x2].all() for y in range(y2,min(image.height,y2+3*h))]))]
    if len(tops)>8 or len(bottoms)>8:raise AnonymizationHold('ruled_cell_line_limit')
    found=set()
    # The nearest short rule may stop at a neighbouring merged cell. Try the
    # other connected row borders within the same bounded neighbourhood too.
    for top in tops:
        for bottom in bottoms:
            if bottom-top>5*h:continue
            spans=[(a,b) for a,b in runs(horizontal(top)&horizontal(bottom)) if a<=x1 and b>=x2]
            if len(spans)!=1:continue
            a,b=spans[0]
            def vertical(x):return _bridge(ink[top:bottom+1,max(0,x-2):min(image.width,x+3)].any(axis=1)).all()
            left=next((x for x in range(x1-1,a-1,-1) if vertical(x)),None)
            right=next((x for x in range(x2,b) if vertical(x)),None)
            if left is None or right is None or not 2*h<=right-left<=40*h:continue
            box=(left+3,top+3,right-2,bottom-2)
            if box[0]<=x1<x2<=box[2] and box[1]<=y1<y2<=box[3]:found.add(box)
    if not found:raise AnonymizationHold('ruled_cell_not_connected')
    return sorted(found,key=lambda b:(b[2]-b[0])*(b[3]-b[1]))


def ruled_region(image,anchor):
    return ruled_regions(image,anchor)[0]


def adjacent_ruled_regions(image,anchor):
    """Label and value may occupy two cells sharing the same row borders."""
    from .medical_anonymization import AnonymizationHold
    first=ruled_region(image,anchor);h=anchor[3]-anchor[1]
    x=first[2]+max(10,h//2)
    if x+h>=image.width:raise AnonymizationHold('adjacent_cell_not_connected')
    second=ruled_region(image,(x,anchor[1],x+h,anchor[3]))
    if (not 2<=second[0]-first[2]<=12 or abs(second[1]-first[1])>4
            or abs(second[3]-first[3])>4):
        raise AnonymizationHold('adjacent_cell_not_connected')
    return first,second


def observed_row_regions(image,anchor,observations):
    """Use local glyph extents when a larger same-row font changes the band.

    Text/numeric identity is irrelevant here. Each proposal still needs blank
    edges and independent positive content checks on its final pixels.
    """
    x1,y1,x2,y2=anchor;h=y2-y1
    if h<5 or h>image.height/8:return []
    aligned=[t['box'] for t in observations if 0<t['box'][3]-t['box'][1]<=2*h
        and abs((t['box'][1]+t['box'][3]-y1-y2)/2)<=.5*h
        and x1<=t['box'][0]<x2+24*h]
    candidates=set()
    for end in aligned:
        if end[2]<=x2:continue
        # A different OCR pass can return a box spanning several table rows.
        # Do not let that unrelated box enlarge/veto every candidate. The
        # original pixels between these two endpoints are still all inspected.
        row=[anchor,end]
        raw=(x1,min(a[1] for a in row),end[2],max(a[3] for a in row))
        if raw[2]-raw[0]>30*h or raw[3]-raw[1]>2*h:continue
        # Locate blank edges close to the observed extents, without cutting
        # neighbouring ink off merely because OCR omitted it.
        for margin in range(3,max(4,round(.4*h)),2):
            box=(max(0,raw[0]-margin),max(0,raw[1]-margin),min(image.width,raw[2]+margin),min(image.height,raw[3]+margin))
            ink=np.asarray(image.crop(box).convert('L'))<190
            if not (ink[:2].any() or ink[-2:].any() or ink[:,:2].any() or ink[:,-2:].any()):
                candidates.add(box);break
    return sorted(candidates)


def separated_row_regions(image,anchor,observations):
    """Retain two adjacent text fields separated only by a verified rule.

    An open top/bottom border is allowed. Removed columns must be a thin,
    continuous vertical rule extending well beyond the text row. No unknown
    text or other ink can be removed between the retained fields.
    """
    x1,y1,x2,y2=anchor;h=y2-y1
    if h<5 or h>image.height/8:return []
    ink=np.asarray(image.convert('L'))<190
    ends=[t['box'] for t in observations if x2<t['box'][0]<x2+24*h
          and 0<t['box'][3]-t['box'][1]<=2*h
          and abs((t['box'][1]+t['box'][3]-y1-y2)/2)<.5*h]
    results=[]
    for end in ends:
        l=max(0,x1-4);r=min(image.width,end[2]+4)
        top=max(0,min(y1,end[1])-4);bottom=min(image.height,max(y2,end[3])+4)
        local=ink[top:bottom,l:r].copy()
        # Require the separator to extend half a label height beyond *both*
        # observed text bounds. A digit stroke ends inside those bounds.
        context=ink[max(0,top-round(.5*h)):min(image.height,bottom+round(.5*h)),l:r]
        # The scan's thin vertical rule can drift by a few pixels. Detect its
        # continuous corridor, then include the entire isolated rule width;
        # a single rigid column need not be dark on every row.
        corridor=context.copy()
        for shift in (1,2):
            corridor[:,shift:]|=context[:,:-shift];corridor[:,:-shift]|=context[:,shift:]
        centres=corridor.mean(axis=0)>.98
        rule=centres.copy()
        for shift in (1,2):rule[shift:]|=centres[:-shift];rule[:-shift]|=centres[shift:]
        # Tolerance searches for the line; blank corridor margins are not part
        # of its measured width or a reason to reject a genuinely thin rule.
        rule &= context.any(axis=0)
        for a,b in runs(rule):
            if b-a>.2*h:rule[a:b]=False
        if not rule.any():continue
        # A text stroke touching a rule cannot safely be erased with it.
        for a,b in runs(rule):
            if local[:,max(0,a-2):a].any() or local[:,b:min(local.shape[1],b+2)].any():rule[a:b]=False
        local[:,rule]=False
        # Keep the entire immediately adjacent value field, including spaced
        # digits. Do not merge across a second table separator or choose an
        # amount based on its OCR value.
        separators=[(a,b) for a,b in runs(rule) if a>x2-l]
        if not separators:continue
        a,b=separators[0];limit=separators[1][0] if len(separators)>1 else local.shape[1]
        words=[]
        for start,stop in ((0,a),(b,limit)):
            cols=np.flatnonzero(local[:,start:stop].any(axis=0))
            if len(cols):words.append([start+int(cols[0]),start+int(cols[-1])+1])
        if len(words)!=2:continue
        segments=[]
        for a,b in words:
            ys=np.flatnonzero(local[:,a:b].any(axis=1))
            if not len(ys) or ys[0]<2 or ys[-1]>=local.shape[0]-2:break
            if a<2 or b>local.shape[1]-2 or rule[max(0,a-2):b+2].any():break
            segments.append((l+a-2,top+int(ys[0])-2,l+b+2,top+int(ys[-1])+3))
        if len(segments)!=2:continue
        if not (segments[0][0]<=x1 and segments[0][2]>=x2):continue
        results.append(tuple(segments))
    return sorted(set(results))


def text_regions(image, anchor, *, below=False):
    x1,y1,x2,y2=anchor;h=y2-y1
    if h<5 or h>image.height/8:return []
    # A single text row and at most one label/amount neighbourhood. Boundaries
    # are blank foreground columns; OCR need not have detected any digit.
    left=max(0,x1-7*h);right=min(image.width,x2+24*h)
    # Amount glyphs can be taller than the label on the same baseline. The
    # narrow label-height band used previously cut off those glyphs before
    # they could even become a candidate. Blank edges must still be present.
    top=max(0,y1-round(.6*h));bottom=min(image.height,y2+round((2.8 if below else .6)*h))
    ink=np.asarray(image.crop((left,top,right,bottom)).convert('L'))<190
    occupied=ink.any(axis=0);parts=runs(occupied)
    words=[]
    for a,b in parts:
        if words and a-words[-1][1]<.65*h:words[-1][1]=b
        else:words.append([a,b])
    boxes=set()
    for i,(a,b) in enumerate(words):
        if b+left<x1 or a+left>x2:continue
        for start in range(max(0,i-1),i+1):
            for end in range(i,min(len(words),i+4)):
                l,r=words[start][0],words[end][1]
                if r-l<2*h or r-l>24*h:continue
                # Missing whitespace at the search edge is not a boundary.
                if l<2 or r>ink.shape[1]-2:continue
                patch=ink[:,l:r];ys=np.flatnonzero(patch.any(axis=1))
                if not len(ys) or ys[0]<2 or ys[-1]>=ink.shape[0]-2:continue
                boxes.add((left+l-2,top+int(ys[0])-2,left+r+2,top+int(ys[-1])+3))
    return sorted(boxes)
