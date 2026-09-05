"""Value-free local region fingerprints; no registration or payment selection."""
from dataclasses import dataclass, field, replace

from .medical_layout_shadow import PageFrame, observe_layout
from .medical_numeric_shadow import observe_numeric_fragments, _adjacent, _intervenes
from .medical_numeric_multipass import NumericOcrPass, _overlap


@dataclass(frozen=True)
class RegionFingerprint:
    members: tuple[int, ...] = field(repr=False)
    box: tuple[float, float, float, float] | None = field(repr=False)
    pattern: tuple[str, ...]
    context: tuple[int, ...]
    negative_context: bool
    issues: tuple[str, ...]


@dataclass(frozen=True)
class RegionView:
    source_digest: bytes = field(repr=False)
    page: int
    aspect_ratio: float
    regions: tuple[RegionFingerprint, ...] = field(repr=False)
    issues: tuple[str, ...]


@dataclass(frozen=True)
class RegionSet:
    input_ordinals: tuple[int, ...] = field(repr=False)
    views: tuple[RegionView, ...] = field(repr=False)
    duplicate_passes: int
    issues: tuple[str, ...]


@dataclass(frozen=True)
class RegionCorrespondence:
    left: int | None
    right: tuple[int, ...]
    classification: str
    issues: tuple[str, ...]


_KINDS=('numeric_like','label_like','unreadable','symbol')
_UNCERTAIN={'invalid_geometry','cluster_extent_unresolved','branching_cluster'}


def _view(p):
    if (type(p) is not NumericOcrPass or type(p.source_digest) is not bytes or len(p.source_digest)!=32
            or type(p.frame) is not PageFrame or type(p.frame.page) is not int or p.frame.page<1):
        raise ValueError()
    tokens=tuple(replace(t,page=1) for t in p.tokens)
    frame=replace(p.frame,page=1)
    complete=bool(tokens) and all(t.page==p.frame.page for t in p.tokens)
    layout=observe_layout(tokens,(frame,),expected_pages=1,observation_complete=complete)
    numeric=observe_numeric_fragments(tokens,(frame,),expected_pages=1,observation_complete=complete)
    atoms={a.ordinal:a for a in numeric.atoms}
    valid={r.ordinal for r in layout.regions if r.box is not None}
    kinds={r.ordinal:('symbol' if r.ordinal in atoms and 'fragment_syntax' in atoms[r.ordinal].issues
                     else r.kind) for r in layout.regions}
    edges={i:set() for i in valid}; forward={i:set() for i in valid}; backward={i:set() for i in valid}
    for i in valid:
        for j in valid:
            if i==j or not _adjacent(tokens[i],tokens[j]):
                continue
            if any(_intervenes(tokens[i],tokens[j],tokens[k]) for k in valid-{i,j}):
                continue
            edges[i].add(j); edges[j].add(i); forward[i].add(j); backward[j].add(i)
    components=[]; remaining=set(valid)
    while remaining:
        seed=min(remaining); pending=[seed]; members=set()
        while pending:
            i=pending.pop()
            if i in members:
                continue
            members.add(i); pending.extend(edges[i]-members)
        remaining-=members
        issue={'branching_cluster'} if any(len(forward[i])>1 or len(backward[i])>1 for i in members) else set()
        if len(members)>6:
            components.extend(((i,),{'cluster_extent_unresolved'}) for i in sorted(members))
        else:
            components.append((tuple(sorted(members,key=lambda i:(tokens[i].x,tokens[i].y,i))),issue))
    components.extend(((r.ordinal,),{'invalid_geometry'}) for r in layout.regions if r.ordinal not in valid)
    components.sort(key=lambda item:min(item[0]))
    regions=[]
    for members,initial in components:
        quality=set(initial); negative=False; context=[0]*16
        for i in members:
            quality.update(layout.regions[i].issues)
            if i in atoms:
                quality.update(atoms[i].issues)
            negative |= layout.regions[i].negative_context
        for span in numeric.spans:
            if set(span.members) & set(members):
                quality.update(span.issues)
                if not set(span.members)<=set(members):
                    quality.add('span_crosses_region')
        box=None
        if all(i in valid for i in members):
            l=min(tokens[i].x for i in members); t=min(tokens[i].y for i in members)
            r=max(tokens[i].x+tokens[i].width for i in members); b=max(tokens[i].y+tokens[i].height for i in members)
            height=max(tokens[i].height for i in members)
            box=(l/frame.width,t/frame.height,r/frame.width,b/frame.height)
            for i in valid-set(members):
                token=tokens[i]; cx=token.x+token.width/2; cy=token.y+token.height/2
                if not (l-3*height<=cx<=r+3*height and t-2*height<=cy<=b+2*height):
                    continue
                sector=0 if cx<l else 1 if cx>r else 2 if cy<t else 3
                context[sector*4+_KINDS.index(kinds[i])]+=1
                negative |= layout.regions[i].negative_context
        regions.append(RegionFingerprint(members,box,tuple(kinds[i] for i in members),tuple(context),negative,tuple(sorted(quality))))
    issues=set(layout.issues)|set(numeric.issues)|{'local_region_unproven'}
    return RegionView(p.source_digest,p.frame.page,p.frame.width/p.frame.height,tuple(regions),tuple(sorted(issues)))


def fingerprint_region_passes(passes: tuple[NumericOcrPass, ...]) -> RegionSet:
    try:
        if type(passes) is not tuple or not 1<=len(passes)<=12:
            raise ValueError()
        retained=[]; ordinals=[]; views=[]; duplicates=0
        for ordinal,p in enumerate(passes):
            if p in retained:
                duplicates+=1
                continue
            views.append(_view(p)); retained.append(p); ordinals.append(ordinal)
        return RegionSet(tuple(ordinals),tuple(views),duplicates,('correlated_ocr_views',))
    except Exception:
        return RegionSet((),(),0,('observation_incomplete','invalid_observation_input'))


def compare_region_views(left: RegionView, right: RegionView, *, cross_source=False):
    """All candidate correspondences survive. No amount or reference is read.

    Same-source views must be verified full-frame resizes by the caller. Different
    sources require explicit pairing and remain unregistered hypotheses. No
    transform is inferred from a coincident numeric value or normalized position.
    """
    source_same=left.source_digest==right.source_digest
    prohibited=(source_same and left.page!=right.page) or (not source_same and not cross_source)
    if source_same and abs(left.aspect_ratio/right.aspect_ratio-1)>0.01:
        prohibited=True
    incomplete='observation_incomplete' in left.issues or 'observation_incomplete' in right.issues
    edges={i:[] for i in range(len(left.regions))}; incoming={j:[] for j in range(len(right.regions))}
    weak_edges=set()
    if not prohibited:
        for i,a in enumerate(left.regions):
            for j,b in enumerate(right.regions):
                if a.box is None or b.box is None or not _overlap(a.box,b.box,contained=True):
                    continue
                h=max(a.box[3]-a.box[1],b.box[3]-b.box[1])
                if not _overlap(a.box,b.box) or abs(a.box[3]-b.box[3])>0.5*h:
                    weak_edges.add((i,j))
                edges[i].append(j); incoming[j].append(i)
    output=[]
    for i,a in enumerate(left.regions):
        targets=edges[i]; quality=set(a.issues)
        for j in targets:
            quality.update(right.regions[j].issues)
        if any((i,j) in weak_edges for j in targets):
            quality.add('partial_region_coverage')
        if prohibited or not source_same:
            quality.add('coordinate_relationship_unresolved')
        if (prohibited or not source_same or incomplete or a.box is None or quality & _UNCERTAIN
                or 'partial_region_coverage' in quality
                or len(targets)>1 or any(len(incoming[j])>1 for j in targets)):
            state='ambiguous_correspondence'
        elif not targets:
            state='region_missing'
        else:
            b=right.regions[targets[0]]
            # Context mismatch is retained; it never drops a competing edge.
            if sum(abs(x-y) for x,y in zip(a.context,b.context))>4:
                state='ambiguous_correspondence'; quality.add('context_unresolved')
            elif a.pattern!=b.pattern:
                state='tokenization_variant'
            else:
                l,t,r,d=a.box; x,y,z,w=b.box
                inter=max(0,min(r,z)-max(l,x))*max(0,min(d,w)-max(t,y))
                union=(r-l)*(d-t)+(z-x)*(w-y)-inter
                state='stable_region' if union>0 and inter/union>=0.9 else 'bbox_variant'
        output.append(RegionCorrespondence(i,tuple(targets),state,tuple(sorted(quality))))
    for j,b in enumerate(right.regions):
        if not incoming[j]:
            state='ambiguous_correspondence' if prohibited or not source_same or incomplete or b.box is None else 'region_missing'
            output.append(RegionCorrespondence(None,(j,),state,b.issues))
    return tuple(output)
