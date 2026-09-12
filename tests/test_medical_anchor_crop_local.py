"""Synthetic-only tests for the local anchor-guided crop evaluation."""
import ast
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw
import pytest

from app.medical_ocr_observation_shadow import OcrObservation, TextRegion
from scripts import medical_anchor_crop_local as anchor


def region(ordinal, text, box, confidence=.99):
    x, y, right, bottom = box
    return TextRegion(ordinal, text, ((x, y), (right, y), (right, bottom), (x, bottom)),
                      confidence, .99, ())


def observation(regions, width=800, height=800):
    return OcrObservation("synthetic", 1, "a" * 64, "synthetic", (), width, height,
                          tuple(regions), ())


@pytest.mark.parametrize("text,cue", [
    ("領収金額", "exact_strong"), ("領収金额", "near_strong"),
    ("請求額", "exact_strong"), ("支払額", "exact_strong"), ("合計", "exact_other")])
def test_exact_and_near_payment_labels(text, cue):
    assert anchor.label_match(text)[0] == cue


def test_split_vertical_label_and_numeric_geometry_create_candidate():
    obs = observation([
        region(0, "金", (100, 100, 130, 130)), region(1, "額", (100, 135, 130, 165)),
        region(2, "3,050円", (150, 130, 240, 165)),
    ])
    anchors = anchor.find_anchors(obs)
    assert any(a["cue"].startswith("split_") for a in anchors)
    candidates = anchor.choose_crops(obs, [])
    assert candidates and candidates[0]["method"] == "local_rectangle"
    assert anchor.contains(candidates[0]["box"], [100, 100, 240, 165])


def test_smallest_enclosing_structure_wins_after_anchor_pair_exists():
    obs = observation([region(0, "領収金額", (110, 120, 200, 150)),
                       region(1, "3050", (250, 120, 320, 150))])
    structures = [
        {"box": [50, 50, 700, 500], "kind": "line_group", "line_support": 4},
        {"box": [100, 100, 350, 180], "kind": "closed_table", "line_support": 20},
    ]
    candidate = anchor.choose_crops(obs, structures)[0]
    assert candidate["box"] == [100, 100, 350, 180]
    assert candidate["method"] == "closed_table"


def test_strong_payment_label_outranks_generic_total_with_better_geometry():
    obs = observation([
        region(0, "合計", (100, 100, 150, 130)), region(1, "300", (160, 100, 210, 130)),
        region(2, "領収金額", (100, 300, 190, 330)), region(3, "300", (250, 360, 300, 390)),
    ])
    candidates = anchor.choose_crops(obs, [])
    assert candidates[0]["cue"] == "exact_strong"


def test_amount_value_does_not_change_selected_box():
    first = observation([region(0, "支払額", (100, 100, 170, 130)),
                         region(1, "1234", (200, 100, 260, 130))])
    second = observation([region(0, "支払額", (100, 100, 170, 130)),
                          region(1, "987654", (200, 100, 260, 130))])
    assert anchor.choose_crops(first, [])[0]["box"] == anchor.choose_crops(second, [])[0]["box"]


def test_no_label_means_no_crop_even_with_numbers():
    obs = observation([region(0, "患者番号", (100, 100, 180, 130)),
                       region(1, "123456", (200, 100, 270, 130))])
    assert anchor.choose_crops(obs, []) == []


def test_manual_evaluation_cannot_participate_in_selection():
    source = Path(anchor.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    modules = {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    assert not modules.intersection({"requests", "urllib", "app.receipt_pipeline", "app.medical_gemini_shadow"})
    assert source.index('(output / "automatic-results.json").write_bytes(frozen)') < source.index(
        'manual_bytes = (manual_root / "manifest.json").read_bytes()')


def test_pixel_crop_strips_metadata_and_preserves_pixels():
    image = Image.new("RGB", (400, 300), "white")
    ImageDraw.Draw(image).rectangle((50, 50, 300, 150), outline="black", width=3)
    image.info["private"] = "synthetic"
    box = [40, 40, 320, 180]
    with Image.open(BytesIO(anchor.png(image.crop(box)))) as result:
        assert result.tobytes() == image.crop(box).tobytes()
        assert not result.info
