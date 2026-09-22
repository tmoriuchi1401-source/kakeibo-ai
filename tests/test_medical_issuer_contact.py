"""Synthetic contact fields; no real patients, providers or phone numbers."""
from copy import deepcopy
from unittest.mock import Mock
import json

from PIL import Image
import pytest

from app import medical_issuer_contact as contact
from app.medical_document_identity import compare_identities
from test_medical_document_identity import evidence
from test_medical_local_reading import token

ISSUER = 'Synthetic clinic'


def observations():
    return [token(ISSUER, (100, 10, 500, 40)),
            token('Tel.', (100, 48, 140, 68)), token('03-1234-5678', (142, 48, 280, 68)),
            token('FAX.03-1234-9876', (300, 48, 490, 68))]


def test_only_labelled_issuer_adjacent_contacts_are_selected():
    regions = contact.contact_regions(observations(), ISSUER)
    assert regions == {'telephone': ('0312345678', (142, 48, 280, 68)),
                       'fax': ('0312349876', (300, 48, 490, 68))}


@pytest.mark.parametrize('change', ['plain_number', 'low_confidence', 'two_numbers', 'patient',
    'referrer', 'far', 'different_column', 'ambiguous_issuer', 'wrong_issuer', 'intervening'])
def test_unbound_or_ambiguous_phone_never_proves_a_provider(change):
    obs = observations()[:3]
    if change == 'plain_number': obs.pop(1); obs[1]['text'] = '0312345678'
    if change == 'low_confidence': obs[2]['confidence'] = 89
    if change == 'two_numbers': obs.append(token('03-1111-2222', (150, 48, 280, 68)))
    if change == 'patient': obs.append(token('患者連絡先', (100, 40, 300, 48)))
    if change == 'referrer': obs.append(token('紹介先', (100, 40, 300, 48)))
    if change == 'far':
        for t in obs[1:]: t['box'] = (t['box'][0], 250, t['box'][2], 270)
    if change == 'different_column':
        for t in obs[1:]: t['box'] = (t['box'][0]+500, 48, t['box'][2]+500, 68)
    if change == 'ambiguous_issuer': obs.append(deepcopy(obs[0]))
    if change == 'wrong_issuer': obs[0]['text'] = 'Another clinic'
    if change == 'intervening':
        obs[2]['box'] = (168, 48, 300, 68); obs.append(token('注', (145, 48, 165, 68)))
    assert contact.contact_regions(obs, ISSUER) == {}


def test_one_formatted_phone_under_issuer_is_allowed_but_patient_block_is_not():
    obs = observations()[:3]; obs.pop(1)
    assert set(contact.contact_regions(obs, ISSUER)) == {'telephone'}
    obs.append(token('患者連絡先', (100, 40, 300, 48)))
    assert contact.contact_regions(obs, ISSUER) == {}


def test_low_confidence_label_cannot_promote_its_number_as_standalone():
    obs = observations()[:3]; obs[1]['confidence'] = 50
    assert contact.contact_regions(obs, ISSUER) == {}


@pytest.mark.parametrize('readings,accepted,calls', [
    (['Tel. 03-1234-5678'], True, 1),
    (['Tel. 03-1234-5679', '03-1234-5678'], False, 1),
    (['', '03-1234-5678'], True, 2),
    (['', '03-1234-567'], False, 2),
    (['', '03-1234-5678 9999'], False, 2),
])
def test_complete_digits_must_agree_without_expected_value_in_ocr(monkeypatch, readings, accepted, calls):
    import pytesseract
    reader = Mock(side_effect=readings); monkeypatch.setattr(pytesseract, 'image_to_string', reader)
    assert contact.confirm_contact(Image.new('RGB', (200, 30), 'white'), (0, 0, 200, 30),
                                   '0312345678', 'telephone') is accepted
    assert reader.call_count == calls
    assert all(c.kwargs == {'lang': 'eng', 'config': '--psm 7', 'timeout': 20} for c in reader.call_args_list)


def test_contact_evidence_is_opaque_and_unavailable_reader_never_promotes(monkeypatch):
    monkeypatch.setattr(contact, 'confirm_contact', lambda *a: True)
    result = contact.read_contacts(None, observations(), ISSUER, lambda k,v: 'a'*64)
    assert set(result) == {'telephone', 'fax'}
    assert '031234' not in json.dumps(result) and ISSUER not in json.dumps(result)
    monkeypatch.setattr(contact, 'confirm_contact', Mock(side_effect=RuntimeError('Unavailable')))
    assert contact.read_contacts(None, observations(), ISSUER, lambda *a: 'a'*64) == {}


def test_two_matching_contacts_and_different_serials_distinguish_bills_despite_name_spelling(monkeypatch):
    left = evidence(monkeypatch)
    right = evidence(monkeypatch, number='009999', source='b', merchant='Synthetic cIinic')
    left['issuer_contacts'] = {'telephone': 'c'*64, 'fax': 'd'*64}
    right['issuer_contacts'] = {'telephone': 'c'*64, 'fax': 'd'*64}
    assert compare_identities(left, right) == 'different'
    for contacts in ({}, {'telephone': 'c'*64}, {'fax': 'd'*64}, {'fax': 'invalid'}, {'unknown': 'e'*64},
                     {'telephone': 'c'*64, 'fax': 'e'*64}, {'telephone': 'd'*64, 'fax': 'c'*64}):
        changed = deepcopy(right); changed['issuer_contacts'] = contacts
        assert compare_identities(left, changed) == 'unknown'
    changed = deepcopy(right); changed['serial_ref'] = left['serial_ref']
    assert compare_identities(left, changed) == 'unknown'
    changed.pop('serial_ref')
    assert compare_identities(left, changed) == 'unknown'
    left['issuer_contacts'] = right['issuer_contacts'] = {'telephone': 'c'*64, 'fax': 'c'*64}
    assert compare_identities(left, right) == 'unknown'


def test_contacts_cannot_link_or_override_same_pixels_or_other_key(monkeypatch):
    left = evidence(monkeypatch)
    right = evidence(monkeypatch, number='009999', merchant='Different clinic')
    left['issuer_contacts'] = {'telephone': 'c'*64, 'fax': 'd'*64}
    right['issuer_contacts'] = deepcopy(left['issuer_contacts'])
    assert compare_identities(left, right) == 'unknown'
    right['source_sha256'] = right['source_image_sha256'] = 'b'*64
    right['key_ref'] = 'e'*64
    assert compare_identities(left, right) == 'unknown'
