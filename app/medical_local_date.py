"""Bounded non-AI receipt date association; never infers a missing date."""
from collections import defaultdict
from datetime import date
import re

from .medical_anonymization import compact

ROLES = {'領収日':'payment', '支払日':'payment', '入金日':'payment', '会計日':'accounting',
         '発行日':'issue', '生年月日':'excluded', '診療日':'excluded', '受診日':'excluded',
         '処方日':'excluded', '期限':'excluded', '期間':'excluded'}
PATTERN = re.compile(r'(?<!\d)(?:(20\d{2})|((?:令和|平成|昭和))(元|\d{1,2}))[年/．.\-](\d{1,2})[月/．.\-](\d{1,2})(?:日|(?!\d))')
ERAS = {'令和':(2018,date(2019,5,1),date(2099,12,31)),
        '平成':(1988,date(1989,1,8),date(2019,4,30)),
        '昭和':(1925,date(1926,12,25),date(1989,1,7))}


def receipt_date(observations):
    """Associate dates with their own label, not the confidence of a whole row.

    An issue date remains provisional until paid-receipt evidence is checked by
    the posting decision. Birth/treatment/deadline dates cannot supply it.
    """
    groups=defaultdict(list)
    vertical=[]
    # Printed forms often put 発行日 above its value, in the same column.
    # Associate one exact role and one complete date only; never borrow a
    # nearby birth/treatment date or cross an intervening text row.
    labels=[t for t in observations if compact(t['text']) in ROLES]
    for token in observations:
        if not PATTERN.fullmatch(compact(token['text'])):continue
        b=token['box'];choices=[]
        for label in labels:
            a=label['box'];h=max(a[3]-a[1],b[3]-b[1])
            if (0<=b[1]-a[3]<=1.5*h and abs((a[0]+a[2]-b[0]-b[2])/2)<=max(a[2]-a[0],b[2]-b[0])*.3
                and not any(t is not label and t is not token and a[3]<t['box'][1]<b[1]
                            and max(a[0],t['box'][0])<min(a[2],t['box'][2]) for t in observations)):
                choices.append(label)
        if len(choices)!=1:continue
        label=choices[0]
        # Validate the lexical date/role through the same parser below. The
        # virtual same-line geometry is used only after original adjacency.
        if min(label['confidence'],token['confidence'])<70:continue
        a=label['box'];joined=dict(token,text=label['text']+token['text'],
            confidence=min(label['confidence'],token['confidence']),box=(a[0],a[1],a[0]+max(a[2]-a[0],b[2]-b[0]),a[3]))
        vertical.append([joined])
    for token in observations:groups[tuple(token['line'])].append(token)
    # Also join separate OCR cells on the same baseline, within a bounded gap.
    rows=[]
    for token in sorted(observations,key=lambda t:(t['box'][1],t['box'][0])):
        box=token['box'];h=box[3]-box[1];cy=(box[1]+box[3])/2
        row=next((r for r in rows if abs(r[0]-cy)<=.35*min(h,r[1])),None)
        if row is None:rows.append([cy,h,[token]])
        else:row[2].append(token)
    candidates={};parsed_count=0;rejected=defaultdict(int)
    for group in list(groups.values())+[r[2] for r in rows]+vertical:
        ordered=sorted(group,key=lambda t:t['box'][0]);text='';owners=[]
        for token in ordered:
            part=compact(token['text']);text+=part;owners.extend([token]*len(part))
        for match in PATTERN.finditer(text):
            parsed_count+=1
            try:
                west,era,ey,month,day=match.groups()
                value=date(int(west) if west else ERAS[era][0]+(1 if ey=='元' else int(ey)),int(month),int(day))
                if era and not ERAS[era][1]<=value<=ERAS[era][2]:raise ValueError
            except ValueError:rejected['date_invalid']+=1;continue
            labels=[(m.start(),m.end(),role) for label,role in ROLES.items()
                    for m in re.finditer(re.escape(label),text[:match.start()])]
            if not labels:rejected['date_role_missing']+=1;continue
            start,end,role=max(labels,key=lambda x:x[1])
            if role=='excluded':rejected['date_role_excluded']+=1;continue
            between=text[end:match.start()]
            if len(between)>3 or any(c not in ':：()（）' for c in between):
                rejected['date_role_not_adjacent']+=1;continue
            evidence=owners[start:match.end()]
            first,last=evidence[0]['box'],evidence[-1]['box']
            h=max(t['box'][3]-t['box'][1] for t in evidence)
            if (abs((first[1]+first[3]-last[1]-last[3])/2)>h*.6
                    or any(b['box'][0]-a['box'][2]>4*h for a,b in zip(evidence,evidence[1:]))):
                rejected['date_spatial_association_unknown']+=1;continue
            if min(t['confidence'] for t in evidence)<70:
                rejected['date_evidence_uncertain']+=1;continue
            suffix=text[match.end():match.end()+2]
            if suffix.startswith(('～','~','から','-')):
                rejected['date_period_not_payment']+=1;continue
            candidates[(value.isoformat(),role)]=True
    days={day for day,role in candidates}
    roles={role for day,role in candidates}
    basis=('payment' if 'payment' in roles else 'accounting' if 'accounting' in roles else 'issue') if candidates else ''
    return (next(iter(days)) if len(days)==1 else ''),{
        'date_candidates':len(days),'date_evidence_verified':len(days)==1,
        'date_basis':basis,'date_parse_matches':parsed_count,'date_rejections':dict(rejected)}
