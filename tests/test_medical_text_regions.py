"""Geometry and privacy regression fixtures are entirely synthetic."""
from pathlib import Path
from io import BytesIO
from PIL import Image,ImageDraw
import pytest

from app.medical_anonymization import AnonymizationHold,png,prepare_payment_crop,validate_png
from app.medical_text_regions import text_regions,ruled_region,adjacent_ruled_regions


def test_candidate_discovery_does_not_require_numeric_ocr_or_a_frame():
    image=Image.new('RGB',(450,300),'white');draw=ImageDraw.Draw(image)
    draw.rectangle((40,40,90,58),fill='black');draw.rectangle((140,40,190,58),fill='black')
    proposals=text_regions(image,(40,40,90,59))
    assert any(l<40 and r>190 and t<40 and b>58 for l,t,r,b in proposals)
    # Ink that touches the search band's edge is not called bounded.
    draw.line((40,25,190,25),fill='black',width=40)
    assert not text_regions(image,(40,40,90,59))


def test_taller_amount_glyphs_are_not_cut_by_the_label_height():
    image=Image.new('RGB',(600,300),'white');draw=ImageDraw.Draw(image)
    draw.rectangle((40,50,120,70),fill='black')
    draw.rectangle((300,43,340,73),fill='black')
    assert any(l<40 and r>340 and t<43 and b>73 for l,t,r,b in text_regions(image,(40,50,120,71)))


def test_connected_lines_bound_a_right_aligned_label_and_tolerate_small_gaps():
    image=Image.new('RGB',(600,300),'white');draw=ImageDraw.Draw(image)
    draw.rectangle((10,40,550,90),outline='black')
    draw.line((300,40,301,40),fill='white')
    box=ruled_region(image,(140,50,240,70))
    assert 10<box[0]<140 and 240<box[2]<550 and 40<box[1]<50 and 70<box[3]<90
    draw.line((550,40,550,90),fill='white')
    with pytest.raises(AnonymizationHold):ruled_region(image,(140,50,240,70))


def test_separate_label_and_amount_cells_keep_original_pixels_and_reject_extra_text():
    original=(Path(__file__).parent/'fixtures/synthetic_medical_payment.png').read_bytes()
    isolated=validate_png(prepare_payment_crop(original,'image/png').payload)
    page=Image.new('RGB',(900,600),'white');page.paste(isolated,(50,140))
    draw=ImageDraw.Draw(page);draw.rectangle((40,150,520,215),outline='black',width=2)
    draw.line((240,150,240,215),fill='black',width=2)
    result=prepare_payment_crop(png(page),'image/png',automatic=True)
    assert result.mapping['validation']=='adjacent_ruled_cells_positive_glyphs_complete_ink'
    assert len(result.mapping['retained_regions_original'])==2
    output=validate_png(result.payload);outer=result.mapping['crop_coordinates_original'];pad=result.mapping['derived_padding_pixels']
    for box in result.mapping['retained_regions_original']:
        l,t,r,b=box
        assert output.crop((l-outer[0]+pad,t-outer[1]+pad,r-outer[0]+pad,b-outer[1]+pad)).tobytes()==page.crop(box).tobytes()
    draw.text((250,178),'PRIVATE ID 123',fill='black')
    with pytest.raises(AnonymizationHold):prepare_payment_crop(png(page),'image/png',automatic=True)


def test_open_row_with_an_isolated_separator_has_honest_provenance():
    original=(Path(__file__).parent/'fixtures/synthetic_medical_payment.png').read_bytes()
    isolated=validate_png(prepare_payment_crop(original,'image/png').payload)
    page=Image.new('RGB',(900,600),'white');page.paste(isolated,(50,140))
    draw=ImageDraw.Draw(page);draw.line((240,70,240,290),fill='black',width=2)
    crop=prepare_payment_crop(png(page),'image/png',automatic=True)
    assert crop.mapping['validation']=='ruled_separator_text_fields_positive_glyphs_complete_ink'
    assert len(crop.mapping['retained_regions_original'])==2
    draw.text((250,178),'PRIVATE ID 123',fill='black')
    with pytest.raises(AnonymizationHold):prepare_payment_crop(png(page),'image/png',automatic=True)


def test_real_ocr_can_prepare_a_synthetic_unframed_region_without_owner_input():
    original=(Path(__file__).parent/'fixtures/synthetic_medical_payment.png').read_bytes()
    isolated=validate_png(prepare_payment_crop(original,'image/png').payload)
    page=Image.new('RGB',(900,600),'white');page.paste(isolated,(50,140))
    crop=prepare_payment_crop(png(page),'image/png',automatic=True)
    assert crop.mapping['validation']=='whitespace_text_region_positive_glyphs_complete_ink'
    assert crop.mapping['verified_payment_cells']==1 and crop.mapping['unresolved_candidates']==0
    assert crop.label=='領収金額' and 'human_review_digest' not in crop.mapping


def test_unframed_region_rejects_unknown_ink_in_the_retained_numeric_region():
    original=(Path(__file__).parent/'fixtures/synthetic_medical_payment.png').read_bytes()
    isolated=validate_png(prepare_payment_crop(original,'image/png').payload)
    page=Image.new('RGB',(900,600),'white');page.paste(isolated,(50,140))
    ImageDraw.Draw(page).text((220,170),'PATIENT',fill='black')
    with pytest.raises(AnonymizationHold):prepare_payment_crop(png(page),'image/png',automatic=True)
