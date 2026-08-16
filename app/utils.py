from __future__ import annotations
import hashlib, json, re
from datetime import datetime, timezone

def sha256_text(s:str)->str: return hashlib.sha256(s.encode("utf-8")).hexdigest()
def canonical_hash(obj)->str: return sha256_text(json.dumps(obj,ensure_ascii=False,sort_keys=True,default=str))
def normalize_store(s:str)->str:
    s=(s or "").lower().strip(); s=re.sub(r"[\s　\-_/\.]+","",s)
    repl={"ａｅｏｎ":"aeon","amazon.co.jp":"amazon","amazonjp":"amazon"}
    for a,b in repl.items(): s=s.replace(a,b)
    return s

def now_jst_string():
    from zoneinfo import ZoneInfo
    return datetime.now(ZoneInfo("Asia/Tokyo")).strftime("%Y-%m-%d %H:%M:%S")
