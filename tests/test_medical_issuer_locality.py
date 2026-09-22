"""Synthetic addresses/identifiers only; locality never links or posts alone."""
from copy import deepcopy
from hashlib import sha256
from unittest.mock import Mock
import json

from PIL import Image
import pytest

from app import medical_issuer_locality as locality, medical_locality_models as models
from app.medical_document_identity import compare_identities
from test_medical_document_identity import evidence
from test_medical_local_reading import token

ISSUER = 'Synthetic clinic'


def observations():
    address = token('架空県架空市北区一丁目', (100,10,400,30))
    address['quad'] = ((100,10),(400,10),(400,30),(100,30))
    return [address, token(ISSUER, (100,35,500,55))]


def test_only_one_selected_issuer_adjacent_address_is_used():
    obs = observations()
    assert locality.address_region(obs, ISSUER) == ('北区', obs[0])


def test_slightly_overlapping_neighbor_column_is_not_an_intervening_issuer():
    obs=observations()
    obs.append(token('〒000-0000',(0,25,104,35)))
    assert locality.address_region(obs,ISSUER)==('北区',obs[0])
    obs[-1]['text']='患者住所'
    assert locality.address_region(obs,ISSUER) is None


@pytest.mark.parametrize('change', ['patient','referrer','low_address','low_issuer','duplicate_address',
    'duplicate_issuer','wrong_issuer','far','other_column','address_below','no_municipality','two_wards',
    'patient_above','intervening_field'])
def test_ambiguous_or_other_party_address_stays_unknown(change):
    obs = observations()
    if change == 'patient': obs.insert(1, token('患者住所', (100,30,400,34)))
    if change == 'referrer': obs[0]['text'] = '紹介先 '+obs[0]['text']
    if change == 'low_address': obs[0]['confidence'] = 89
    if change == 'low_issuer': obs[1]['confidence'] = 89
    if change == 'duplicate_address': obs.append(deepcopy(obs[0]))
    if change == 'duplicate_issuer': obs.append(deepcopy(obs[1]))
    if change == 'wrong_issuer': obs[1]['text'] = 'Other clinic'
    if change == 'far': obs[1]['box'] = (100,200,500,220)
    if change == 'other_column': obs[0]['box'] = (510,10,700,30)
    if change == 'address_below': obs[0]['box'] = (100,60,400,80)
    if change == 'no_municipality': obs[0]['text'] = '北区一丁目'
    if change == 'two_wards': obs[0]['text'] += ' 架空県架空市南区'
    if change == 'patient_above':obs.insert(0,token('患者住所',(100,0,400,8)))
    if change == 'intervening_field':obs.insert(1,token('別施設',(100,30,400,34)))
    assert locality.address_region(obs, ISSUER) is None


@pytest.mark.parametrize('change',['missing','shape','nan','outside','crossed','reverse','bbox','flat'])
def test_rectification_requires_original_valid_detector_geometry(change):
    t = observations()[0]
    if change == 'missing': t.pop('quad')
    if change == 'shape': t['quad'] = ((100,10),(400,30))
    if change == 'nan': t['quad'] = ((100,float('nan')),(400,10),(400,30),(100,30))
    if change == 'outside': t['quad'] = ((-1,10),(400,10),(400,30),(-1,30))
    if change == 'crossed': t['quad'] = ((100,10),(400,30),(400,10),(100,30))
    if change == 'reverse': t['quad'] = tuple(reversed(t['quad']))
    if change == 'bbox': t['box'] = (100,0,400,30)
    if change == 'flat': t['quad'] = ((100,10),(400,10),(400,10),(100,10))
    with pytest.raises(ValueError): locality.line_image(Image.new('RGB',(600,100),'white'), t)


@pytest.mark.parametrize('reads,accepted,calls', [
    (['架空市北区一丁目'],True,1),
    (['架空市南区一丁目','架空市北区一丁目'],False,1),
    (['架空市北区 架空市南区','架空市北区'],False,1),
    (['','架空市北区一丁目'],True,2),
    (['','架空市南区'],False,2),
    (['',''],False,2),
])
def test_fixed_bounded_rereading_preserves_disagreement(monkeypatch,tmp_path,reads,accepted,calls):
    monkeypatch.setattr(models,'verified_directory',lambda:tmp_path)
    reader=Mock(side_effect=reads);monkeypatch.setattr(locality,'recognize_line',reader)
    assert locality.confirm_ward(Image.new('RGB',(600,100),'white'),observations()[0],'北区') is accepted
    assert reader.call_count==calls
    assert all(len(c.args)==2 and c.args[1]==tmp_path and not c.kwargs for c in reader.call_args_list)


def test_local_subprocess_has_fixed_arguments_no_expected_text_and_no_shell(monkeypatch,tmp_path):
    import subprocess
    from types import SimpleNamespace
    runner=Mock(return_value=SimpleNamespace(returncode=0,stdout='架空市北区'.encode()))
    monkeypatch.setattr(subprocess,'run',runner)
    directory=tmp_path/'model directory with spaces'
    assert locality.recognize_line(Image.new('RGB',(100,40),'white'),directory)=='架空市北区'
    args=runner.call_args.args[0];kwargs=runner.call_args.kwargs
    assert args[1:]==['stdin','stdout','-l','jpn+eng','--tessdata-dir',str(directory),'--oem','1','--psm','7']
    assert kwargs['shell'] is False and kwargs['timeout']==20 and kwargs['stderr']==subprocess.DEVNULL
    assert kwargs['input'].startswith(b'\x89PNG')
    assert not any('北区' in a for a in args)
    runner.side_effect=subprocess.TimeoutExpired(args,20)
    with pytest.raises(TimeoutError):locality.recognize_line(Image.new('RGB',(100,40)),directory)


def test_reader_returns_only_keyed_region_reference_and_fails_closed(monkeypatch):
    monkeypatch.setattr(locality,'confirm_ward',lambda *a:True)
    result=locality.read_locality(None,observations(),ISSUER,lambda k,v:'f'*64)
    assert result=={'policy':locality.POLICY,'readers':2,'ward_ref':'f'*64}
    assert '北区' not in json.dumps(result,ensure_ascii=False) and 'quad' not in result
    monkeypatch.setattr(locality,'confirm_ward',Mock(side_effect=ValueError('missing model')))
    assert locality.read_locality(None,observations(),ISSUER,lambda *a:'f'*64)=={}


def test_document_packet_keeps_locality_opaque(monkeypatch):
    from app import medical_document_identity as identity
    from test_medical_local_reading import parsed,proof
    monkeypatch.setattr(identity,'confirm_number',lambda *a:True)
    monkeypatch.setattr(locality,'confirm_ward',lambda *a:True)
    p=proof();p['document_binding'].update(source_sha256='a'*64,source_image_sha256='a'*64,page=1,unit=1)
    obs=observations()+[token('請求書番号001234',(0,80,200,100))]
    receipt=parsed();receipt.merchant=ISSUER
    result=identity.read_identity(None,obs,receipt,p,b'x'*32)
    assert result['issuer_locality']['readers']==2
    text=json.dumps(result,ensure_ascii=False)
    assert all(value not in text for value in ('北区','架空県',ISSUER,'001234','quad','box','text'))


def pair(monkeypatch):
    left=evidence(monkeypatch,merchant='Synthetic North clinic')
    right=evidence(monkeypatch,number='876543',source='b',day='2026-08-31',merchant='Synthetic South clinic')
    left['issuer_locality']={'policy':locality.POLICY,'readers':2,'ward_ref':'c'*64}
    right['issuer_locality']={'policy':locality.POLICY,'readers':2,'ward_ref':'d'*64}
    left['issuer_contacts']={'telephone':'e'*64}
    right['issuer_contacts']={'fax':'f'*64}
    return left,right


def test_distinct_bill_date_issuer_ward_and_contact_can_corroborate_other_spending(monkeypatch):
    left,right=pair(monkeypatch)
    assert compare_identities(left,right)=='different'
    assert compare_identities(right,left)=='different'


@pytest.mark.parametrize('change',['no_locality','unknown_policy','single_reader','bad_ward','same_ward',
    'same_date','invalid_date','no_contact','bad_contact','unknown_contact_role','shared_contact',
    'cross_role_shared_contact','repeated_contact','same_serial','no_serial','same_pixels','other_key'])
def test_partial_or_conflicting_corrobation_cannot_clear_duplicate_hold(monkeypatch,change):
    left,right=pair(monkeypatch)
    if change=='no_locality':right.pop('issuer_locality')
    if change=='unknown_policy':right['issuer_locality']['policy']='other'
    if change=='single_reader':right['issuer_locality']['readers']=1
    if change=='bad_ward':right['issuer_locality']['ward_ref']='unknown'
    if change=='same_ward':right['issuer_locality']=deepcopy(left['issuer_locality'])
    if change=='same_date':right['date']=left['date']
    if change=='invalid_date':right['date']='2026-99-99'
    if change=='no_contact':right['issuer_contacts']={}
    if change=='bad_contact':right['issuer_contacts']={'telephone':'unknown'}
    if change=='unknown_contact_role':right['issuer_contacts']={'patient':'f'*64}
    if change=='shared_contact':right['issuer_contacts']=deepcopy(left['issuer_contacts'])
    if change=='cross_role_shared_contact':right['issuer_contacts']={'fax':'e'*64}
    if change=='repeated_contact':right['issuer_contacts']={'telephone':'f'*64,'fax':'f'*64}
    if change=='same_serial':right['serial_ref']=left['serial_ref']
    if change=='no_serial':right.pop('serial_ref')
    if change=='same_pixels':right['source_image_sha256']=left['source_image_sha256']
    if change=='other_key':right['key_ref']='c'*64
    assert compare_identities(left,right)=='unknown'


def test_runtime_model_verification_never_downloads_or_accepts_changed_bytes(monkeypatch,tmp_path):
    import urllib.request
    request=Mock(side_effect=AssertionError('runtime must stay offline'))
    monkeypatch.setattr(urllib.request,'urlopen',request)
    monkeypatch.setattr(models,'directory',lambda:tmp_path)
    monkeypatch.setattr(models,'MODELS',{'jpn':sha256(b'synthetic').hexdigest()})
    with pytest.raises(ValueError):models.verified_directory()
    (tmp_path/'jpn.traineddata').write_bytes(b'synthetic')
    assert models.verified_directory()==tmp_path
    (tmp_path/'jpn.traineddata').write_bytes(b'changed')
    with pytest.raises(ValueError):models.verified_directory()
    request.assert_not_called()
