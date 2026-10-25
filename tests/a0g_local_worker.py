"""Subprocess fixture. Never authorization, token issue or Google network."""
import json
from pathlib import Path
import socket
import ssl
import sys
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/'tests')]
import pytest
from a0g_local_fakes import install
from staging_shadow import google_a0g_local as local

def denied(*args,**kwargs): raise AssertionError('live_network_forbidden')
socket.socket.connect=denied
socket.socket.connect_ex=denied
with pytest.MonkeyPatch.context() as mp:
    root,authority,digest,contract,sha,counter,mode=sys.argv[1:]
    install(mp,Path(root),counter,mode)
    result=local.run_once(authority_path=authority,authority_digest=digest,contract_path=contract,expected_sha=sha,enabled=True)
    print(json.dumps(result))
