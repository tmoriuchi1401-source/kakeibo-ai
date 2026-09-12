"""Synthetic drawings only; tests never open real corpus/review images."""
import ast
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw
import pytest

from app.medical_ocr_observation_shadow import OcrObservation, TextRegion
from scripts import medical_table_crop_local as table


@pytest.fixture(scope='module')
def runtime():
    executable=Path(__file__).resolve().parents[1]/'.private/medical-rapidocr-runtime/venv/Scripts/python.exe'
    if not executable.is_file(): pytest.skip('Existing optional OpenCV runtime unavailable')
    return str(executable.resolve())


def drawing(boxes=(), broken=False):
    image=Image.new('RGB',(800,800),'white'); draw=ImageDraw.Draw(image)
    for x,y,right,bottom in boxes:
        if not broken: draw.line((x,y,right,y),fill='black',width=3)
        draw.line((x,y,x,bottom,right,bottom,right,y),fill='black',width=3)
        draw.line((x,(y+bottom)//2,right,(y+bottom)//2),fill='black',width=3)
        draw.line(((x+right)//2,y,(x+right)//2,bottom),fill='black',width=3)
    return image


def region(text,box=(130,150,240,180),confidence=.99,issues=()):
    x,y,r,b=box
    return TextRegion(0,text,((x,y),(r,y),(r,b),(x,b)),confidence,.99,issues)


def observation(regions=(),issues=()):
    return OcrObservation('synthetic',1,'a'*64,'synthetic',(),800,800,tuple(regions),issues)


def test_ruled_table_detected_in_original_coordinates(runtime):
    image=drawing([(100,100,700,350)])
    before=image.tobytes()
    found=table.detect_in_runtime(table.png(image),runtime)
    assert len(found)==1 and found[0]['internal_rules']
    assert all(abs(a-b)<=5 for a,b in zip(found[0]['box'],[100,100,700,350]))
    assert image.tobytes()==before


def test_multiple_tables_remain_human_selection(runtime):
    found=table.detect_in_runtime(table.png(drawing([(100,100,700,300),(100,450,700,700)])),runtime)
    candidates,status=table.rank_tables(found,observation([region('領収金額')]))
    assert len(candidates)==2 and status=='human_selection_required'
    assert all(not c['inspection']['transmission_authorized'] for c in candidates)


@pytest.mark.parametrize('broken,boxes',[(False,()),(True,[(100,100,700,350)])])
def test_missing_or_broken_outer_frame_never_full_image_fallback(runtime,broken,boxes):
    found=table.detect_in_runtime(table.png(drawing(boxes,broken)),runtime)
    assert found==[]


def test_unknown_heading_and_incomplete_ocr_do_not_suppress_crop(runtime):
    found=table.detect_in_runtime(table.png(drawing([(100,100,700,350)])),runtime)
    candidates,status=table.rank_tables(found,observation([region('読めない文字',confidence=.2)],('observation_incomplete',)))
    assert len(candidates)==1 and status=='single_table_provisional'
    warnings=candidates[0]['inspection']['warnings']
    assert 'no_payment_heading_observed' in warnings and 'ocr_incomplete' in warnings


def test_private_clinical_and_cut_boundary_are_warnings():
    check=table.inspect_crop([100,100,700,350],observation([
        region('患者番号 検査',(80,120,200,150)),region('',(220,120,280,150),None,('recognition_missing',))]))
    assert {'private_or_clinical_suspected','text_at_crop_boundary','unrecognized_or_low_confidence'}<=set(check['warnings'])
    assert check['transmission_authorized'] is False


def test_negative_inspection_is_not_safe_or_qr_checked():
    check=table.inspect_crop([100,100,700,350],observation([region('領収金額')]))
    assert check['warnings']==[]
    assert check['privacy_status']=='unverified'
    assert check['qr_barcode_status']=='not_inspected'
    assert check['transmission_authorized'] is check['production_authorized'] is False


def test_pixel_crop_keeps_every_original_pixel_and_strips_metadata():
    image=drawing([(100,100,700,350)]); image.info['secret']='synthetic-only'
    crop=(98,98,704,354)
    with Image.open(BytesIO(table.png(image.crop(crop)))) as result:
        assert result.tobytes()==image.crop(crop).tobytes()
        assert not result.info


def test_manual_rotation_inverse():
    assert table.original_rectangle([20,50,40,90],1,[100,200])==[10,20,50,40]
    assert table.original_rectangle([20,50,40,90],0,[100,200])==[20,50,40,90]


def test_html_no_remote_resources_and_no_scripts():
    value=table.report_html([])
    assert 'https://' not in value and 'http://' not in value and '<script' not in value
    assert "connect-src 'none'" in value and "script-src 'none'" in value
    assert '未確認' in value and '未検査' in value


def test_no_production_or_network_clients_and_manual_read_after_freeze():
    source=Path(table.__file__).read_text(encoding='utf-8')
    tree=ast.parse(source)
    modules={n.module for n in ast.walk(tree) if isinstance(n,ast.ImportFrom)}
    assert not modules.intersection({'requests','urllib','app.receipt_pipeline','app.medical_gemini_shadow'})
    assert source.index("(output/'automatic-results.json').write_bytes(frozen)") < source.index("manual_bytes=(manual_root/'manifest.json').read_bytes()")
