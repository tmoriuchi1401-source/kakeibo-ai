from app.utils import canonical_hash, normalize_store

def test_hash_stable():
    assert canonical_hash({"a":1,"b":2})==canonical_hash({"b":2,"a":1})
def test_store():
    assert normalize_store("Amazon.co.jp") in ("amazon","amazoncojp")
