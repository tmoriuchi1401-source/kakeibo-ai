"""Non-AI accounting roles, independent of outbound-image privacy clearance.

Only fixed role codes and geometry leave this module. It neither reads amounts
as accounting answers nor changes the original crop mapping/failure counts.
"""
from collections import Counter
import re

POLICY='medical-accounting-roles-v1'
LEXICON={
    'receipt_heading':('領収書','領収証'),
    'treatment_heading':('診療費',), 'visit_heading':('外来','入院'),
    'receipt_stamp':('領収印','領収済印'),
    'burden_rate':('負担割合','負担率'),
    'insurer_burden':('保険者負担金額','保険者負担額','保険者負担'),
    'points':('総点数','合計点数','診療点数','点数'),
    'prior_receipt':('前回入金額','前回領収額','前回までの累計入金額'),
    'current_charge':('今回請求額','今回請求金額'),
    'insurance_component':('保険分負担金額','保険分の負担金額'),
    'included_tax':('内消費税','うち消費税','内消費税額'),
    'settlement_unresolved':('請求額','請求金額','未収金額','未収額','預り金','お預り金',
                             '預かり金','返金額','返金','釣銭','お釣り','累計入金額'),
    'partial_payment':('一部入金','一部入金額','分割支払','分割入金','部分入金'),
}
NONPAYMENT={'receipt_heading','receipt_stamp','burden_rate','insurer_burden','points','prior_receipt'}


def _union(boxes):
    return (min(b[0] for b in boxes),min(b[1] for b in boxes),max(b[2] for b in boxes),max(b[3] for b in boxes))


def _contains(a,b):
    return a[0]-2<=b[0]<b[2]<=a[2]+2 and a[1]-2<=b[1]<b[3]<=a[3]+2


def _aligned(a,b):
    return max(0,min(a[3],b[3])-max(a[1],b[1]))>=.6*min(a[3]-a[1],b[3]-b[1])


def _lexical(value):
    from .medical_anonymization import compact
    return re.sub(r'[：:（）()\[\]【】・]','',compact(value))


def _word_check(image,box,phrase):
    """Confirm an uncertain *label* locally; never requires readable digits."""
    if image is None:return False
    from PIL import Image
    from .medical_anonymization import tokens
    l,t,r,b=box;pad=max(8,round((b-t)*.5))
    canvas=Image.new('RGB',(r-l+2*pad,b-t+2*pad),'white')
    canvas.paste(image.crop(box),(pad,pad))
    for view in (canvas,canvas.convert('L').point(lambda p:0 if p<190 else 255).convert('RGB')):
        observed=tokens(view,7)
        if (observed and min(t['confidence'] for t in observed)>=65
                and _lexical(''.join(t['text'] for t in observed))==phrase):return True
    return False


def label_fields(streams,image=None):
    from .medical_anonymization import LABELS
    vocabulary=dict(LEXICON,payment=LABELS)
    phrases={phrase:role for role,words in vocabulary.items() for phrase in words}
    found={}
    for stream in streams:
        for first in stream:
            a=first['box'];h=a[3]-a[1]
            row=sorted((t for t in stream if t['box'][0]>=a[0] and _aligned(a,t['box'])),key=lambda t:t['box'][0])
            selected=[]
            for token in row[:12]:
                if selected and token['box'][0]-selected[-1]['box'][2]>2.5*max(h,token['box'][3]-token['box'][1]):break
                selected.append(token);phrase=_lexical(''.join(t['text'] for t in selected))
                if phrase not in phrases:continue
                box=_union([t['box'] for t in selected]);confidence=min(t['confidence'] for t in selected)
                good=confidence>=65 or _word_check(image,box,phrase)
                if good:found[(phrases[phrase],box)]={'role':phrases[phrase],'box':box,'phrase':phrase}
    # One printed label observed by several OCR passes is one physical field.
    fields=[]
    for field in sorted(found.values(),key=lambda f:-(f['box'][2]-f['box'][0])*(f['box'][3]-f['box'][1])):
        if any(f['role']==field['role'] and (_contains(f['box'],field['box']) or _contains(field['box'],f['box'])) for f in fields):continue
        fields.append(field)
    # The charge words in a combined document heading are not an amount field.
    for heading in [f for f in fields if f['role']=='receipt_heading']:
        b=heading['box'];h=b[3]-b[1]
        prefix=[f for f in fields if f['role']=='treatment_heading' and _aligned(f['box'],b)
                and 0<=b[0]-f['box'][2]<=8*h]
        if len(prefix)==1:heading['box']=_union([b,prefix[0]['box']])
    # A second OCR pass can give the same word taller/shifted glyph boxes.
    # Re-read their union as the exact full role label before merging them.
    for specific in [f for f in fields if f['role']=='current_charge']:
        for generic in [f for f in fields if f['role']=='settlement_unresolved' and f['phrase'] in specific['phrase']]:
            a,b=specific['box'],generic['box'];h=max(a[3]-a[1],b[3]-b[1])
            union=_union([a,b])
            if (abs(a[2]-b[2])<=h and union[3]-union[1]<=2*h
                    and _word_check(image,union,specific['phrase'])):
                specific['box']=union
    return [f for f in fields if f['role'] not in {'treatment_heading','visit_heading'}]


def _charge_relation(image,charge,mapping,label):
    """Explicit current charge/current receipt labels in one ruled value column.

    Equality of their amounts, OCR confidence and mere proximity are not proof.
    A common physical table boundary plus both current-period labels is required.
    Header cells may be merged; an inner divider need not cross the header.
    """
    if image is None or label!='今回入金額':return False
    segments=mapping.get('retained_regions_original',[])
    if len(segments)!=2:return False
    a,b=charge['box'],segments[0];h=max(a[3]-a[1],b[3]-b[1])
    if not (a[3]<b[1] and b[1]-a[3]<=8*h and abs(a[0]-b[0])<=h and abs(a[2]-b[2])<=h):return False
    import numpy as np
    from .medical_text_regions import _bridge
    ink=np.asarray(image.convert('L'))<190
    left,right=segments[0][2],segments[1][0]
    columns=list(range(left,right))+list(range(max(0,min(a[0],b[0])-8*h),min(a[0],b[0])))
    for x in columns:
        if _bridge(ink[a[1]:b[3],max(0,x-2):min(image.width,x+3)].any(axis=1)).all():return True
    return False


def _core_boxes(text,parts):
    from .medical_anonymization import MONEY_CUES,compact
    cores=[]
    for start in range(len(parts)):
        for stop in range(start+1,len(parts)+1):
            chosen=parts[start:stop];value=compact(''.join(t['text'] for t in chosen))
            if any(cue in value for cue in MONEY_CUES):
                box=_union([t['box'] for t in chosen])
                if not any(_contains(box,old) for old in cores):cores.append(box)
                break
    return cores or [_union([t['box'] for t in parts])]


def _unassigned_currency(streams,fields,selected,image):
    """An unlabelled currency field cannot disappear just for lacking a cue."""
    from .medical_anonymization import compact,AnonymizationHold
    from .medical_text_regions import ruled_regions,adjacent_ruled_regions
    numeric=r'[0-9OIl?？□�,，.\-−]+'
    observations=set()
    for stream in streams:
        for token in stream:
            value=compact(token['text'])
            if re.fullmatch(r'[¥￥]'+numeric+'円?|'+numeric+'円',value):observations.add(tuple(token['box']))
            if value!='円':continue
            for left in stream:
                a,b=left['box'],token['box'];h=max(a[3]-a[1],b[3]-b[1])
                if 0<=b[0]-a[2]<=h and _aligned(a,b) and re.fullmatch(numeric,compact(left['text'])):
                    observations.add(_union([a,b]))
    covered=[selected['box']]
    if image is not None:
        for field in fields:
            if field['role'] not in {'insurer_burden','prior_receipt','current_charge_linked','billing_component_linked'}:continue
            try:covered.extend(ruled_regions(image,field['box']))
            except AnonymizationHold:pass
            try:covered.extend(adjacent_ruled_regions(image,field['box']))
            except AnonymizationHold:pass
    unknown=[]
    for box in sorted(observations):
        if any(_contains(b,box) for b in covered):continue
        if any(_contains(b,box) or _contains(box,b) for b in unknown):continue
        unknown.append(box)
    return unknown


def evaluate_roles(streams,cues,unresolved,mapping,label,*,image=None):
    """Classify each discovery with positive role/relationship evidence."""
    from .medical_anonymization import anchors,compact
    fields=label_fields(streams,image)
    selected={'role':'payment','box':tuple(mapping['crop_coordinates_original']),'phrase':label,'selected':True}
    fields=[selected]+[f for f in fields if not(f['role']=='payment' and _contains(selected['box'],f['box']))]
    charges=[f for f in fields if f['role']=='current_charge']
    linked=charges[0] if len(charges)==1 and _charge_relation(image,charges[0],mapping,label) else None
    if linked:linked['role']='current_charge_linked'
    for f in fields:
        if f['role'] not in {'insurance_component','included_tax'}:continue
        a=f['box'];top=linked['box'] if linked else None;bottom=selected['box']
        h=max(a[3]-a[1],top[3]-top[1]) if top else 0
        header_component=bool(top and _aligned(a,top) and top[2]<=a[0] and a[2]-top[0]<=30*h)
        inner_component=bool(top and top[1]<=a[1]<a[3]<=bottom[3] and top[0]-2<=a[0]<a[2]<=bottom[2]+2)
        if header_component or inner_component:f['role']='billing_component_linked'
    # Larger known labels cover their embedded generic cue (e.g. 前回入金額).
    fields=[f for f in fields if not any(g is not f and _contains(g['box'],f['box'])
        and g['box']!=f['box'] and g['role'] not in {'settlement_unresolved','payment'} for g in fields)]
    traces={}
    for stream in streams:
        detail={};anchors(stream,details=detail)
        for key,indexes in detail.items():traces.setdefault(key,[]).append([stream[i] for i in indexes])
    seen=set();results=[];unknown=set();payments={'selected'}
    nonpayment=NONPAYMENT|{'current_charge_linked','billing_component_linked'}
    for ordinal,(text,box) in enumerate(cues):
        matches=[]
        for parts in traces.get((text,tuple(box)),[]):
            cores=_core_boxes(text,parts);owners=[]
            for core in cores:
                covered=[(i,f) for i,f in enumerate(fields) if _contains(f['box'],core)]
                # Conflicting role observations cannot be silently resolved.
                choices={i for i,f in covered if f.get('selected') or f['role'] in nonpayment}
                if not choices:choices={i for i,f in covered}
                if len(choices)!=1:owners=[];break
                owners.append(next(iter(choices)))
            if owners and len(set(owners))==1:matches.append(owners[0])
        if len(set(matches))==1:
            index=matches[0];field=fields[index];fid='selected' if field.get('selected') else 'field-'+str(index)
            role=field['role']
            if role=='payment':kind='payment_candidate';reason='current_payment_field';payments.add(fid)
            elif role in nonpayment:kind='nonpayment';reason=role
            else:kind='unknown';reason=('partial_payment_relation_unresolved' if role=='partial_payment' else 'settlement_relationship_unresolved');unknown.add(fid)
        else:
            parts=next(iter(traces.get((text,tuple(box)),[])),[])
            cores=_core_boxes(text,parts) if parts else [box]
            fid='unassigned-'+str(tuple(sorted(tuple(b) for b in cores)))
            kind='unknown';reason=('unassigned_charge_field' if '請求' in compact(text) else
                                  'unassigned_burden_field' if '負担' in compact(text) else 'unknown_money_role')
            unknown.add(fid)
        underlying=kind;role_reason=reason
        if fid in seen:kind='duplicate';reason='same_physical_label_tokens'
        seen.add(fid)
        results.append({'ordinal':ordinal,'kind':kind,'underlying_kind':underlying,'reason':reason,'role_reason':role_reason,'field':fid,
            'box':list(box),'cutout_unresolved':any(tuple(box)==tuple(b) for b,_ in unresolved)})
    # Recognized alternate/partial payment labels remain a veto even if the
    # deliberately broad discovery did not generate their exact full phrase.
    for index,f in enumerate(fields):
        if f['role']=='payment' and not f.get('selected'):payments.add('field-'+str(index))
        if f['role']=='partial_payment':unknown.add('field-'+str(index))
    headings=[f for f in fields if f['role']=='receipt_heading']
    if len(headings)>1:unknown.add('multiple_receipt_units')
    currencies=_unassigned_currency(streams,fields,selected,image)
    unknown.update('unassigned-currency-'+str(i) for i in range(len(currencies)))
    selected_results=[r for r in results if r['cutout_unresolved']]
    # All original failed cuts must remain represented in the separate audit.
    complete=len(selected_results)==len(unresolved)
    if not complete:unknown.add('candidate_correspondence_incomplete')
    return {'policy':POLICY,'binding':{k:mapping[k] for k in ('source_sha256','source_image_sha256','page','unit','crop_sha256')},
        'original_cutout_unresolved':len(unresolved),'original_verified_payment_images':mapping['verified_payment_cells'],
        'candidates':results,'cutout_classifications':dict(Counter(r['kind'] for r in selected_results)),
        'independent_payment_fields':len(payments),'unresolved_payment_conflicts':len(unknown)+len(payments)-1,
        'meaning_reasons':dict(Counter({r['field']:r['role_reason'] for r in results if r['underlying_kind']=='unknown'}.values())),
        'additional_unassigned_currency_fields':len(currencies),
        'field_evidence':[{'field':'selected' if f.get('selected') else 'field-'+str(i),'role':f['role'],'box':list(f['box'])} for i,f in enumerate(fields)],
        'complete_candidate_correspondence':complete}


def review_summary(evidence):
    counts=evidence['cutout_classifications']
    return ('会計上の役割：重複検出'+str(counts.get('duplicate',0))+'、根拠付き非支払'+str(counts.get('nonpayment',0))
        +'、支払候補'+str(counts.get('payment_candidate',0))+'、意味未確定'+str(counts.get('unknown',0))+'。'
        +'元の切出し未解決'+str(evidence['original_cutout_unresolved'])+'箇所は記録を保持。'
        +'会計上の未解決競合'+str(evidence['unresolved_payment_conflicts'])+'件。')


def save_evaluation(state,analysis_id,evidence,key):
    """Append separately signed policy evidence; never rewrite AI provenance."""
    from copy import deepcopy
    import hmac
    from .receipt_reimport_production import digest,encoded
    from .drive_run_state import StateError
    record=state.get(analysis_id);mapping=record['mapping']
    if (record['phase']!='complete' or evidence.get('policy')!=POLICY
            or evidence.get('binding')!={k:mapping[k] for k in ('source_sha256','source_image_sha256','page','unit','crop_sha256')}
            or evidence.get('original_cutout_unresolved')!=mapping['unresolved_candidates']
            or evidence.get('original_verified_payment_images')!=mapping['verified_payment_cells']):
        raise StateError('medical_accounting_evidence_binding_changed')
    evaluation={'analysis_id':analysis_id,'analysis_digest':digest(record),'evidence':deepcopy(evidence)}
    eid=digest(evaluation)
    evaluation['integrity_tag']=hmac.new(key,b'medical-accounting-evaluation\0'+encoded(evaluation),'sha256').hexdigest()
    existing=state.store.value.get('medical_accounting_evaluations',{}).get(eid)
    if existing is not None:
        if existing!=evaluation:raise StateError('medical_accounting_evaluation_changed')
        return eid
    value=deepcopy(state.store.value);value.setdefault('medical_accounting_evaluations',{})[eid]=evaluation
    state.store.save(value)
    return eid


def verify_evaluation(value,eid,analysis_id,record,key):
    import hmac
    from .receipt_reimport_production import digest,encoded
    from .drive_run_state import StateError
    saved=value.get('medical_accounting_evaluations',{}).get(eid)
    if not saved:raise StateError('medical_accounting_evaluation_missing')
    unsigned={k:v for k,v in saved.items() if k!='integrity_tag'}
    expected=hmac.new(key,b'medical-accounting-evaluation\0'+encoded(unsigned),'sha256').hexdigest()
    if (digest(unsigned)!=eid or not hmac.compare_digest(expected,saved.get('integrity_tag',''))
            or unsigned['analysis_id']!=analysis_id or unsigned['analysis_digest']!=digest(record)
            or unsigned['evidence'].get('policy')!=POLICY):
        raise StateError('medical_accounting_evaluation_integrity_failed')
    return unsigned['evidence']
