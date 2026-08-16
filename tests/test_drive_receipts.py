import pytest

from app.drive_receipts import normalize_folder_id


def test_normalize_drive_folder_id():
    folder_id = "1AbCdEfGhIjKlMnOp"
    assert normalize_folder_id(folder_id) == folder_id
    assert normalize_folder_id(f"https://drive.google.com/drive/folders/{folder_id}") == folder_id
    assert normalize_folder_id(f"https://drive.google.com/drive/u/0/folders/{folder_id}?usp=sharing") == folder_id


def test_reject_invalid_drive_folder_id():
    with pytest.raises(ValueError, match="フォルダIDが不正"):
        normalize_folder_id("not a folder id!")
