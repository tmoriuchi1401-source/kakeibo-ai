from io import BytesIO
from PIL import Image
import pytest
from scripts.crop_medical_vision_local import render, source_cards, load_source


def test_contiguous_pixels_preserved_and_mask_opaque_metadata_removed():
    image = Image.new('RGB', (100, 100), 'black')
    image.info['secret'] = 'synthetic_metadata'
    with Image.open(BytesIO(render(image, (10,10,90,90), [(20,20,30,30)]))) as result:
        assert result.size == (80,80)
        assert result.getpixel((15,15)) == (255,255,255)
        assert result.getpixel((40,40)) == (0,0,0)
        assert not result.info


def test_invalid_crop_rejected():
    with pytest.raises(ValueError):
        render(Image.new('RGB',(10,10)), (-1,0,5,5), [])


def test_source_mapping_discards_answers():
    rows = [dict(unit=i,page=1,source_path='synthetic',source_sha256='a',image_sha256='b',
                 original_candidate=123,confirmed_amount=456) for i in range(1,11)]
    assert set(source_cards({'units':rows,'records':[{'amount':789}]})[0]) == {
        'unit','page','source_path','source_sha256','image_sha256'}
    with pytest.raises(ValueError): source_cards({'units':rows[:-1]})


def test_source_tamper_rejected_before_render(tmp_path):
    source = tmp_path / 'synthetic.png'; source.write_bytes(b'changed')
    with pytest.raises(ValueError, match='source_changed'):
        load_source(dict(source_path=str(source),source_sha256='0'*64,page=1,image_sha256='1'*64))
