from io import BytesIO
from pathlib import Path

from PIL import Image
import pytest

from app.medical_ocr_observation_shadow import TextRegion
from scripts.prepare_medical_vision_eval import render_sanitized, select_regions, _outside_repo


def region(ordinal, text, x, y, width=60, height=20):
    return TextRegion(ordinal,text,((x,y),(x+width,y),(x+width,y+height),(x,y+height)),.99,.99)


def test_selects_payment_context_and_related_amount_but_not_pii_or_unrelated_id():
    rows=(region(0,"領収金額",10,10),region(1,"1,234円",80,10),
          region(2,"患者番号",10,80),region(3,"987654",80,80),region(4,"2026/09/11",10,120))
    assert [r.ordinal for r in select_regions(rows)] == [0,1]


def test_keeps_negative_context_needed_to_distinguish_totals():
    rows=(region(0,"請求額",10,10),region(1,"2,000円",80,10),
          region(2,"預り金",10,40),region(3,"3,000円",80,40))
    assert [r.ordinal for r in select_regions(rows)] == [0,1,2,3]


def test_render_creates_new_metadata_free_png_with_redacted_background():
    source=Image.new("RGB",(200,120),"black")
    buffer=BytesIO(); source.save(buffer,format="PNG",pnginfo=None)
    payload=render_sanitized(buffer.getvalue(),(region(0,"支払額",10,10),))
    with Image.open(BytesIO(payload)) as result:
        assert result.format == "PNG"
        assert not result.info
        assert result.size[0] < source.size[0] and result.size[1] < source.size[1]


def test_no_context_fails_closed_and_repo_output_is_rejected(tmp_path):
    assert select_regions((region(0,"山田太郎",10,10),region(1,"123456",80,10))) == ()
    with pytest.raises(ValueError,match="no_safe_payment_context_regions"):
        render_sanitized(b"unused",())
    with pytest.raises(ValueError,match="output_must_be_outside_repository"):
        _outside_repo(tmp_path/"out",tmp_path)
