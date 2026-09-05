import ast
from dataclasses import replace
import json
from pathlib import Path

import pytest

from app.medical_layout_shadow import PageFrame
from app.medical_numeric_shadow import observe_numeric_fragments
from app.medical_receipt_privacy import _StructuredOcrToken as Token, build_receipt_privacy_preview


def token(text, x=20, y=20, confidence=96, page=1, width=10, height=20, line=1):
    return Token(text, page, x, y, width, height, confidence, (1, 1, line, 5))


def observe(*tokens, frames=(PageFrame(1, 800, 1000),), pages=1, complete=True):
    return observe_numeric_fragments(tokens, frames, expected_pages=pages, observation_complete=complete)


def test_currency_comma_and_digits_can_be_observed_without_erasing_atoms():
    result = observe(token('¥'), token('1', 32), token(',',44), token('280',56, width=30))
    assert len(result.atoms) == 4
    whole = next(s for s in result.spans if s.members == (0,1,2,3))
    assert whole.parse_valid
    assert 'payment_role_unproven' in whole.issues
    assert result.aggregate()['fragment_atoms'] == 2
    assert not hasattr(whole, 'amount')


def test_adjacent_valid_numbers_are_ambiguous_even_with_high_confidence():
    result = observe(token('12'), token('80',32))
    assert result.spans[0].parse_valid
    assert 'digit_concatenation_unproven' in result.spans[0].issues
    assert all(a.parse_valid for a in result.atoms)


@pytest.mark.parametrize('confidence', [69, -1, float('nan'), 101])
def test_invalid_or_low_confidence_survives_every_span(confidence):
    result = observe(token('¥'), token('1280',32,confidence=confidence,width=30))
    span = next(s for s in result.spans if s.members == (0,1))
    assert span.parse_valid
    assert set(span.issues) & {'low_confidence','invalid_confidence'}
    assert set(result.atoms[1].issues) & {'low_confidence','invalid_confidence'}


@pytest.mark.parametrize('text', ['1.280', '1,,280', '-1280', '1O80'])
def test_malformed_and_low_confidence_are_distinct_preserved_flags(text):
    result = observe(token(text,confidence=30))
    assert result.atoms[0].issues == ('low_confidence','malformed_numeric')


def test_separated_period_is_never_rewritten_to_a_comma():
    result = observe(token('1'), token('.',32), token('280',44,width=30))
    whole = next(s for s in result.spans if s.members == (0,1,2))
    assert not whole.parse_valid
    assert 'malformed_numeric' in whole.issues
    assert len(result.atoms) == 3


@pytest.mark.parametrize('barrier', ['word', '???', 'O', '2.3'])
def test_no_skipping_an_intervening_low_confidence_token(barrier):
    result = observe(token('1'),token(barrier,32,confidence=20),token('280',44,width=30))
    assert not any(s.members == (0,2) for s in result.spans)


def test_baseline_and_gap_override_ocr_line_keys():
    assert not observe(token('1'),token('280',32,y=100)).spans
    assert not observe(token('1'),token('280',90)).spans
    assert observe(token('1',line=4),token('280',32,line=7)).spans


def test_currency_comma_baseline_and_scale_invariance():
    original = (token('¥'),token('1',32),token(',',44,y=35,height=5),token('280',56,width=30))
    base = observe(*original)
    for scale in (0.25, 2, 10):
        tokens = tuple(replace(t,x=t.x*scale,y=t.y*scale,width=t.width*scale,height=t.height*scale) for t in original)
        result = observe(*tokens,frames=(PageFrame(1,800*scale,1000*scale),))
        assert result == base


def test_overlapping_duplicates_and_branches_cannot_look_unique():
    result = observe(token('¥'), token('1280',32,width=30), token('1280',32,width=30))
    assert len(result.atoms) == 3
    assert len(result.spans) == 2
    assert all('branching_adjacency' in s.issues for s in result.spans)
    assert all('overlapping_observations' in s.issues for s in result.spans)


def test_pages_never_join_and_missing_coverage_is_explicit():
    result = observe(token('1'),token('280',32,page=2),
                     frames=(PageFrame(1,800,1000),PageFrame(2,800,1000)),pages=2)
    assert not result.spans
    assert 'observation_incomplete' in observe(token('1'),complete=False).issues
    assert 'observation_incomplete' in observe().issues


def test_long_runs_retain_atoms_and_signal_a_bound_instead_of_silent_truncation():
    result = observe(*(token('1',20+12*i) for i in range(8)))
    assert len(result.atoms) == 8
    assert 'span_limit_exceeded' in result.issues
    assert 'observation_incomplete' in result.issues


def test_magnitude_and_expected_value_have_no_selection_role(capsys):
    result = observe(token('PRIVATE_SENTINEL'),token('987654',50,width=30),token('¥',100),token('10',112))
    exposed = repr(result)+repr(result.atoms)+repr(result.spans)+json.dumps(result.aggregate())
    assert 'PRIVATE_SENTINEL' not in exposed and '987654' not in exposed
    assert capsys.readouterr() == ('','')
    assert all(type(v) is int for v in result.aggregate().values())


def test_no_production_import_or_resolution_change():
    tokens=(token('支払額',width=60),token('1',90),token('280',102,width=30,confidence=20))
    before=build_receipt_privacy_preview('病院 診療',tokens)
    observe(*tokens)
    assert build_receipt_privacy_preview('病院 診療',tokens)==before
    assert before.status=='needs_review'
    for path in (Path(__file__).resolve().parents[1]/'app').glob('*.py'):
        if path.name in {'medical_numeric_shadow.py', 'medical_numeric_multipass.py'}:
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding='utf-8'))):
            names=[node.module or ''] if isinstance(node,ast.ImportFrom) else [a.name for a in node.names] if isinstance(node,ast.Import) else []
            assert not any(module in name for name in names for module in ('medical_numeric_shadow', 'medical_numeric_multipass'))


@pytest.mark.parametrize('parts', [('¥12', '80'), ('12', '80円'), ('¥', '12', '80')])
def test_currency_does_not_prove_that_adjacent_digit_fragments_are_one_value(parts):
    result = observe(*(token(text, 20 + 12 * i) for i, text in enumerate(parts)))
    whole = next(s for s in result.spans if s.members == tuple(range(len(parts))))
    assert whole.parse_valid
    assert 'digit_concatenation_unproven' in whole.issues
    assert len(result.atoms) == len(parts)
