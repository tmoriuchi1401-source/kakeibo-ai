"""Source/ordinal identity shared by observation, confirmation and completion."""
from hashlib import sha256
import json
import re
from .drive_run_state import StateError


def page_identity(source_hash, number, count):
    if (not isinstance(source_hash,str) or not re.fullmatch('[0-9a-f]{64}',source_hash)
            or type(number) is not int or type(count) is not int or not 1<=number<=count<=50):
        raise StateError('pdf_page_identity_invalid')
    return sha256(json.dumps(['pdf-page-v1',source_hash,number,count],
        ensure_ascii=True,separators=(',',':')).encode()).hexdigest()
