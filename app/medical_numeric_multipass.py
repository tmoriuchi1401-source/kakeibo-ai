"""Local, reference-free comparison of correlated numeric OCR views.

No winner, amount, confidence score, transport or production decision is returned.
Source digests are transient grouping keys, never exported or persisted.
"""
from dataclasses import dataclass, field
import re

from .medical_layout_shadow import PageFrame
from .medical_numeric_shadow import NumericShadow, observe_numeric_fragments, _surface
from .medical_receipt_privacy import _StructuredOcrToken, _structured_amount


@dataclass(frozen=True)
class NumericOcrPass:
    source_digest: bytes = field(repr=False)
    psm: int
    frame: PageFrame
    tokens: tuple[_StructuredOcrToken, ...] = field(repr=False)


@dataclass(frozen=True)
class NumericComparison:
    anchor: tuple[int, int] = field(repr=False)
    matching_configurations: int
    tokenization_variants: int
    conflicting_interpretations: bool
    unresolved_quality: bool
    missing_configurations: int


@dataclass(frozen=True)
class NumericMultiPass:
    input_ordinals: tuple[int, ...] = field(repr=False)
    observations: tuple[NumericShadow, ...] = field(repr=False)
    comparisons: tuple[NumericComparison, ...] = field(repr=False)
    source_groups: int
    duplicate_passes: int
    issues: tuple[str, ...]

    def aggregate(self):
        return {
            'retained_passes':len(self.observations), 'source_groups':self.source_groups,
            'duplicate_passes':self.duplicate_passes,
            'repeated_numeric_views':sum(c.matching_configurations > 1 for c in self.comparisons),
            'conflicting_numeric_views':sum(c.conflicting_interpretations for c in self.comparisons),
            'quality_unresolved_views':sum(c.unresolved_quality for c in self.comparisons),
            'coverage_unresolved_views':sum(c.missing_configurations > 0 for c in self.comparisons),
            'observation_incomplete':int('observation_incomplete' in self.issues),
        }


_QUALITY = {'low_confidence','invalid_confidence','invalid_geometry','malformed_numeric',
            'fragment_syntax','branching_adjacency','digit_concatenation_unproven','overlapping_observations'}


def _overlap(a, b, *, contained=False):
    l,t,r,d = a; x,y,z,w = b
    area = max(0,min(r,z)-max(l,x))*max(0,min(d,w)-max(t,y))
    first=(r-l)*(d-t); second=(z-x)*(w-y)
    denominator=min(first,second) if contained else first+second-area
    return denominator > 0 and area / denominator >= 0.5


def compare_numeric_passes(passes: tuple[NumericOcrPass, ...]) -> NumericMultiPass:
    """Compare full-frame resizes of a source page; never register distinct images.

    The caller computes a digest from the immutable *base* image/source bytes and
    keeps actual page ordinals. Digests prove grouping only within this local
    experiment, not OCR independence. Repeated configurations never add votes.
    Boxes are normalized only for a verified full-frame resize, not crops/deskew.
    """
    try:
        return _compare(passes)
    except Exception:
        return NumericMultiPass((),(),(),0,0,('observation_incomplete','invalid_observation_input'))


def _compare(passes):
    if type(passes) is not tuple or not 1 <= len(passes) <= 12:
        raise ValueError()
    retained=[]; ordinals=[]; observations=[]; configurations=[]; groups=[]; rows=[]
    issues={'correlated_ocr_views','payment_role_unproven'}
    duplicates=0
    for ordinal,p in enumerate(passes):
        if (type(p) is not NumericOcrPass or type(p.source_digest) is not bytes or len(p.source_digest)!=32
                or type(p.psm) is not int or p.psm not in (3,6,11) or type(p.frame) is not PageFrame):
            raise ValueError()
        if p in retained:
            duplicates+=1
            continue
        observation=observe_numeric_fragments(p.tokens,(p.frame,),expected_pages=p.frame.page,
                                               observation_complete=bool(p.tokens))
        # A page is evaluated separately, while keeping its real source ordinal.
        if p.frame.page != 1:
            from dataclasses import replace
            observation=observe_numeric_fragments(tuple(replace(t,page=1) for t in p.tokens),
                (replace(p.frame,page=1),),expected_pages=1,
                observation_complete=bool(p.tokens) and all(t.page==p.frame.page for t in p.tokens))
        group=(p.source_digest,p.frame.page)
        configuration=(*group,p.psm,p.frame.width,p.frame.height)
        if configuration in configurations:
            issues.add('configuration_reobserved')
        if group in groups:
            prior=retained[groups.index(group)].frame
            if abs((p.frame.width/p.frame.height)/(prior.width/prior.height)-1) > 0.01:
                issues.add('coordinate_relationship_unresolved')
        if 'observation_incomplete' in observation.issues:
            issues.add('observation_incomplete')
        index=len(retained)
        retained.append(p); ordinals.append(ordinal); observations.append(observation)
        configurations.append(configuration); groups.append(group)
        for position,item in enumerate((*observation.atoms,*observation.spans)):
            members=item.members if hasattr(item,'members') else (item.ordinal,)
            parts=tuple(_surface(p.tokens[i].text) for i in members)
            box=(min(p.tokens[i].x for i in members)/p.frame.width,
                 min(p.tokens[i].y for i in members)/p.frame.height,
                 max(p.tokens[i].x+p.tokens[i].width for i in members)/p.frame.width,
                 max(p.tokens[i].y+p.tokens[i].height for i in members)/p.frame.height)
            rows.append((index,position,box,_structured_amount(''.join(parts)),parts,item.issues))
    if len(rows)>2048 or 'coordinate_relationship_unresolved' in issues:
        issues.add('observation_incomplete')
        if len(rows)>2048:
            issues.add('comparison_limit_exceeded')
        return NumericMultiPass(tuple(ordinals),tuple(observations),(),len(set(groups)),duplicates,tuple(sorted(issues)))
    comparisons=[]
    for index,position,box,value,parts,quality in rows:
        if value is None or 'invalid_geometry' in quality:
            continue
        same_group=[row for row in rows if groups[row[0]]==groups[index]]
        # Include contained fragments, not only similarly sized whole spans.
        affected=[row for row in same_group if _overlap(box,row[2],contained=True)]
        matches=[row for row in affected if row[3]==value and _overlap(box,row[2])]
        available={configurations[row[0]] for row in affected}
        expected={configurations[i] for i,g in enumerate(groups) if g==groups[index]}
        comparisons.append(NumericComparison((index,position),
            len({configurations[row[0]] for row in matches}),
            len({tuple(re.sub('[^0-9]','',part) for part in row[4]) for row in matches}),
            any(row[3] is not None and row[3]!=value for row in affected),
            bool(issues & {'observation_incomplete','configuration_reobserved'}) or
            any(row[3] is None or set(row[5]) & _QUALITY for row in affected),
            len(expected-available)))
    return NumericMultiPass(tuple(ordinals),tuple(observations),tuple(comparisons),len(set(groups)),duplicates,tuple(sorted(issues)))
