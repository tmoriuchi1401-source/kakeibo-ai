"""Standalone CI visual check using installed Chrome and Node, no new packages.

Not part of pytest discovery: existing 4158 tests remain unchanged. Authentication
uses HostRig's existing signed Google test path, never a disabled auth gate.
"""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from threading import Thread
import unittest
from urllib.parse import urlsplit

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from werkzeug.serving import make_server, WSGIRequestHandler

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT/'tests')]
from unified_shadow_fixtures import HostRig, Document, DOMAIN, add_native_unit_cards


class QuietHandler(WSGIRequestHandler):
    def log(self, *_args, **_kwargs): pass


def synthetic_tls(folder):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'isolated-localhost')])
    now = datetime.now(timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
        .serial_number(x509.random_serial_number()).not_valid_before(now-timedelta(minutes=1))
        .not_valid_after(now+timedelta(hours=1))
        .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address('127.0.0.1'))]),critical=False)
        .sign(key,hashes.SHA256()))
    cert_file, key_file = folder/'local-cert.pem', folder/'local-key.pem'
    cert_file.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_file.write_bytes(key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption()))
    import base64
    spki = base64.b64encode(sha256(key.public_key().public_bytes(serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo)).digest()).decode()
    return (str(cert_file),str(key_file)), spki


class ShadowVisualCheck(unittest.TestCase):
    def test_pc_390_list_detail_and_no_writes(self):
        self.assertEqual(sys.platform,'linux','existing Linux renderer required; no install fallback')
        chrome = next((shutil.which(name) for name in ('google-chrome','google-chrome-stable','chromium','chromium-browser') if shutil.which(name)),None)
        node = shutil.which('node')
        self.assertTrue(chrome and node,'existing browser/Node missing; do not install')
        output = ROOT/'artifacts/shadow-visual'
        output.mkdir(parents=True,exist_ok=False)
        violations = []
        class DenyProxy(BaseHTTPRequestHandler):
            def deny(self):
                violations.append('browser attempted non-loopback proxy request')
                self.send_error(403)
            do_CONNECT = do_GET = do_POST = deny
            def log_message(self,*_args): pass
        with pytest.MonkeyPatch.context() as patch, tempfile.TemporaryDirectory(prefix='shadow-visual-') as temporary:
            folder = Path(temporary)
            h = HostRig(patch)
            self.assertEqual(h.login().status_code,303)
            add_native_unit_cards(h)
            selected = ['isolated-native-units-1','isolated-native-units-2','isolated-legacy-hold',h.f.case_id]
            h.snapshot = [deepcopy(next(c for c in h.snapshot if c['case_id']==cid)) for cid in selected]
            expected = [dict(case_id=c['case_id'],**c['display']) for c in h.snapshot]
            self.assertEqual([c['unit_count'] for c in expected],[1,2,0,0])
            unit_ids = [u['unit_id'] for c in expected for u in c['units']]
            self.assertEqual(len(set(unit_ids)),3,'each synthetic Unit belongs to one case')
            before = deepcopy(h.db.data),deepcopy(h.db.versions),h.db.writes,h.f.io.raw,h.f.io.writes,h.f.state_io.raw,h.f.state_io.writes,deepcopy(h.snapshot)
            def forbidden(*_args,**_kwargs):
                violations.append('GET/render/resize write or display call')
                raise AssertionError('read-only rendering boundary violated')
            patch.setattr(Document,'create',forbidden);patch.setattr(Document,'update',forbidden)
            patch.setattr(h.f.io,'replace_versioned',forbidden)
            patch.setattr(h.f.state_io,'replace_versioned',forbidden)
            patch.setattr(h.f.grouping,'display',forbidden)
            patch.setattr(h.f.port,'submit_native',forbidden)
            from google.cloud import firestore
            from googleapiclient import discovery
            patch.setattr(firestore,'Client',forbidden);patch.setattr(discovery,'build',forbidden)
            tls, spki = synthetic_tls(folder)
            server = make_server('127.0.0.1',0,h.app,ssl_context=tls,request_handler=QuietHandler)
            proxy = ThreadingHTTPServer(('127.0.0.1',0),DenyProxy)
            threads = [Thread(target=s.serve_forever,daemon=True) for s in (server,proxy)]
            for thread in threads: thread.start()
            base = 'https://127.0.0.1:'+str(server.server_port)
            cookies = [dict(name=name,value=h.client.get_cookie(name,domain=DOMAIN).value,url=base,
                path='/',secure=True,httpOnly=True,sameSite='Lax') for name in (h.web.COOKIE,h.web.LOGIN_COOKIE)]
            config = dict(chrome=chrome,profile=str(folder/'profile'),netlog=str(folder/'netlog.json'),spki=spki,
                proxy='http://127.0.0.1:'+str(proxy.server_port),base=base,output=str(output),cookies=cookies,
                cases=expected,detail_id='isolated-native-units-2')
            try:
                result = subprocess.run([node,str(ROOT/'tests/shadow_visual_browser.cjs')],input=json.dumps(config),
                    text=True,capture_output=True,timeout=120)
            finally:
                for s in (server,proxy): s.shutdown();s.server_close()
                for thread in threads: thread.join(timeout=5)
            # Browser output is measured synthetic DOM data only. Cookies, TLS
            # keys, Chrome profile, netlog and stderr NEVER enter artifact paths.
            if result.stdout.strip(): print(result.stdout.strip())
            self.assertEqual(result.returncode,0,'headless check failed (sanitized measurements above)')
            netlog = json.loads((folder/'netlog.json').read_text('utf8'))
            def inspect(value):
                if isinstance(value,dict):
                    for k,v in value.items():
                        if k in {'url','original_url'} and isinstance(v,str):
                            parsed=urlsplit(v)
                            if parsed.scheme in {'http','https','ws','wss'} and parsed.hostname not in {'127.0.0.1','localhost','::1'}:
                                violations.append('browser netlog contains non-localhost URL')
                        inspect(v)
                elif isinstance(value,list):
                    for v in value: inspect(v)
            inspect(netlog)
            kinds = {v:k for k,v in netlog['constants']['logEventTypes'].items()}
            for event in netlog['events']:
                if kinds.get(event['type']) in {'TCP_CONNECT_ATTEMPT','UDP_CONNECT'}:
                    params = event.get('params',{})
                    address = params.get('address') or params.get('remote_address')
                    if isinstance(address,str):
                        host = urlsplit('//'+address).hostname
                        if host and not ipaddress.ip_address(host).is_loopback:
                            violations.append('browser transport attempted non-loopback endpoint')
            self.assertEqual(violations,[],'network or write boundary violated')
            after = deepcopy(h.db.data),deepcopy(h.db.versions),h.db.writes,h.f.io.raw,h.f.io.writes,h.f.state_io.raw,h.f.state_io.writes,deepcopy(h.snapshot)
            self.assertEqual(after,before,'GET/render/resize modified state or inputs')
            self.assertEqual((h.f.accounting_appends,h.f.ai_sends,h.f.source_moves),(0,0,0))
            from PIL import Image
            images=sorted(output.glob('*.png'))
            self.assertEqual(len(images),4)
            for path in images:
                with Image.open(path) as image:
                    self.assertEqual(image.width,390 if 'mobile' in path.name else 1440)
                    self.assertGreaterEqual(image.height,844 if 'mobile' in path.name else 1000)
            self.assertEqual({p.suffix for p in output.iterdir()},{'.png'})
            print('GET/render/resize writes=0; accounting=0; AI=0; move=0; production authority=0; screenshots=4')


if __name__=='__main__': unittest.main(verbosity=2)
