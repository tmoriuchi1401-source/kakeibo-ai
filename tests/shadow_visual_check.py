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
import time
from threading import Thread
import unittest
from urllib.parse import urlsplit, parse_qsl, quote

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


def safe_url(raw):
    """Whitelist URL components only; never persist headers or auth payloads."""
    unknown = dict(url='unknown', scheme='unknown', hostname='unknown', port='unknown', path='unknown')
    if not raw:
        return unknown
    try:
        u = urlsplit(raw)
        if u.scheme in {'data', 'blob'}:
            return dict(url=u.scheme+':<redacted>', scheme=u.scheme, hostname='unknown', port='unknown', path='<redacted>')
        if u.scheme not in {'http', 'https', 'ws', 'wss'} or not u.hostname:
            return unknown
        port = u.port or (443 if u.scheme in {'https', 'wss'} else 80)
        import re
        keys = [key if re.fullmatch(r'[a-zA-Z0-9_.-]{1,64}', key) else 'redacted-key' for key, _ in parse_qsl(u.query, keep_blank_values=True)]
        query = '&'.join(quote(key, safe='')+'=<redacted>' for key in keys)
        host = '['+u.hostname+']' if ':' in u.hostname else u.hostname
        authority = host+(':'+str(u.port) if u.port else '')
        return dict(url=u.scheme+'://'+authority+u.path+('?' + query if query else ''),
                    scheme=u.scheme, hostname=u.hostname, port=str(port), path=u.path)
    except ValueError:
        return unknown


def redaction_self_test():
    sample = safe_url('https://private-user:private-pass@example.test:8443/font.css?token=private-token&signature=private-signature#private-fragment')
    encoded = json.dumps(sample)
    assert not any(value in encoded for value in ('private-user', 'private-pass', 'private-token', 'private-signature', 'private-fragment'))
    assert sample['url'] == 'https://example.test:8443/font.css?token=<redacted>&signature=<redacted>'
    assert 'private-data' not in json.dumps(safe_url('data:text/plain,private-data'))


def font_inventory():
    def families(command):
        executable = shutil.which(command[0])
        if not executable:
            return dict(available=False, families=[])
        result = subprocess.run([executable, *command[1:]], text=True, capture_output=True, timeout=20)
        return dict(available=result.returncode == 0, families=sorted(set(result.stdout.strip().splitlines())))
    return dict(japanese_language_fonts=families(['fc-list', ':lang=ja', '--format=%{family}\n']),
                japanese_sample_glyph_fonts=families(['fc-list', ':charset=65e5 672c 8a9e 9818 53ce 66f8 78ba 8a8d', '--format=%{family}\n']),
                system_ui_match=families(['fc-match', 'system-ui:lang=ja', '--format=%{family}\n']),
                sans_serif_match=families(['fc-match', 'sans-serif:lang=ja', '--format=%{family}\n']),
                noto_package_status=families(['dpkg-query', '-W', '-f=${Package} ${Version} ${Status}\n', 'fonts-noto-cjk', 'fonts-noto-core']))


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
        redaction_self_test()
        redaction = subprocess.run([node, str(ROOT/'tests/shadow_visual_browser.cjs'), '--redaction-self-test'], text=True, capture_output=True, timeout=20)
        self.assertEqual(redaction.returncode, 0, 'Node URL redaction self-test failed')
        print('Python and Node URL redaction self-tests PASSED')
        output = ROOT/'artifacts/shadow-visual'
        output.mkdir(parents=True,exist_ok=False)
        (output/'font-inventory.json').write_text(json.dumps(font_inventory(), ensure_ascii=False, indent=2), encoding='utf-8')
        violations = []
        proxy_records = []
        class DenyProxy(BaseHTTPRequestHandler):
            def deny(self):
                url = safe_url('https://'+self.path if self.command == 'CONNECT' else self.path)
                if self.command == 'CONNECT':
                    # CONNECT reveals an authority, not a requested resource path.
                    url['path'] = 'unknown'
                proxy_records.append(dict(sequence=len(proxy_records)+1, layer='proxy', time_ms=int(time.time()*1000),
                    method=self.command, **url, resource_type='unknown', document_url='unknown', redirect_from='unknown',
                    initiator_type='unknown', initiator_stack=[], frame_url='unknown', allowed=False,
                    url_completeness='authority-only' if self.command == 'CONNECT' else 'scheme-host-port-path'))
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
            netlog_file = folder/'netlog.json'
            try:
                netlog = json.loads(netlog_file.read_text('utf8'))
            finally:
                netlog_file.unlink(missing_ok=True)
            netlog_url_violations = []
            def inspect(value):
                if isinstance(value,dict):
                    for k,v in value.items():
                        if k in {'url','original_url'} and isinstance(v,str):
                            parsed=urlsplit(v)
                            if parsed.scheme in {'http','https','ws','wss'} and (parsed.scheme+'://'+parsed.netloc) != base:
                                violations.append('browser netlog contains non-localhost URL')
                                netlog_url_violations.append(safe_url(v))
                        inspect(v)
                elif isinstance(value,list):
                    for v in value: inspect(v)
            inspect(netlog)
            transport_external_count = 0
            kinds = {v:k for k,v in netlog['constants']['logEventTypes'].items()}
            for event in netlog['events']:
                if kinds.get(event['type']) in {'TCP_CONNECT_ATTEMPT','UDP_CONNECT'}:
                    params = event.get('params',{})
                    address = params.get('address') or params.get('remote_address')
                    if isinstance(address,str):
                        host = urlsplit('//'+address).hostname
                        if host and not ipaddress.ip_address(host).is_loopback:
                            violations.append('browser transport attempted non-loopback endpoint')
                            transport_external_count += 1
            records_file = output/'network-records.json'
            page = json.loads(records_file.read_text('utf-8')) if records_file.exists() else dict(observer='no page evidence produced', records=[])
            records = page['records']+proxy_records
            records.sort(key=lambda r: r['time_ms'])
            for index, record in enumerate(records, 1):
                record['sequence'] = index
            page['records'] = records
            page['netlog_external_urls'] = netlog_url_violations
            records_file.write_text(json.dumps(page, ensure_ascii=False, indent=2), encoding='utf-8')
            external = lambda r: not r['allowed']
            observed = {(r['hostname'], r['port']) for r in records if r['layer'] != 'proxy' and external(r)}
            summary = dict(page_level_external=sum(r['layer']=='page-fetch' and external(r) for r in records),
                cdp_external=sum(r['layer']=='cdp-network' and external(r) for r in records),
                proxy_external=len(proxy_records),
                proxy_only_external=sum((r['hostname'],r['port']) not in observed for r in proxy_records),
                proxy_only_matching_rule='destination host/port absent from both page observers; CONNECT has no path',
                localhost_allowed=sum(r['allowed'] and r['scheme'] not in {'data','blob'} for r in records),
                data_blob=sum(r['scheme'] in {'data','blob'} for r in records),
                netlog_external_url_observations=len(netlog_url_violations),
                netlog_external_transport_attempts=transport_external_count,
                raw_netlog_deleted=not netlog_file.exists())
            (output/'network-summary.json').write_text(json.dumps(summary, indent=2), encoding='utf-8')
            after = deepcopy(h.db.data),deepcopy(h.db.versions),h.db.writes,h.f.io.raw,h.f.io.writes,h.f.state_io.raw,h.f.state_io.writes,deepcopy(h.snapshot)
            safety = dict(state_and_inputs_unchanged=after == before, memory_state_write_delta=h.db.writes-before[2],
                review_state_write_delta=h.f.io.writes-before[4]+h.f.state_io.writes-before[6],
                write_or_display_spy_calls=violations.count('GET/render/resize write or display call'),
                accounting_write=h.f.accounting_appends, ai_send=h.f.ai_sends, source_move=h.f.source_moves,
                production_authority_update=0, production_clients='memory only; real constructors rejected by spy')
            (output/'safety-counts.json').write_text(json.dumps(safety, indent=2), encoding='utf-8')
            print('NETWORK SUMMARY '+json.dumps(summary));print('WRITE SUMMARY '+json.dumps(safety))
            # Diagnostics are safely persisted before any network failure is evaluated.
            self.assertEqual(result.returncode,0,'headless check failed (redacted artifacts saved)')
            self.assertEqual(after,before,'GET/render/resize modified state or inputs')
            self.assertEqual((h.f.accounting_appends,h.f.ai_sends,h.f.source_moves),(0,0,0))
            from PIL import Image
            images=sorted(output.glob('*.png'))
            self.assertEqual(len(images),4)
            for path in images:
                with Image.open(path) as image:
                    self.assertEqual(image.width,390 if 'mobile' in path.name else 1440)
                    self.assertGreaterEqual(image.height,844 if 'mobile' in path.name else 1000)
            self.assertEqual({p.name for p in output.iterdir()},
                {'list-pc-1440.png','list-mobile-390.png','detail-pc-1440.png','detail-mobile-390.png',
                 'network-records.json','network-summary.json','font-inventory.json','computed-fonts.json','viewport-metrics.json','safety-counts.json'})
            self.assertEqual(summary['page_level_external'],0,'page external request attempted')
            self.assertEqual(summary['cdp_external'],0,'CDP external request attempted')
            self.assertEqual(violations,[],'network or write boundary violated')
            print('GET/render/resize writes=0; accounting=0; AI=0; move=0; production authority=0; screenshots=4')


if __name__=='__main__': unittest.main(verbosity=2)
