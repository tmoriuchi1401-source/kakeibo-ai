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
import os
import re
import socket
import errno
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


def isolated_network_evidence(outer_namespace):
    """Fail closed: a separate Linux namespace with loopback and no egress."""
    current = os.readlink('/proc/self/ns/net')
    assert current != outer_namespace, 'network namespace was not isolated'
    def ip(*args):
        result = subprocess.run(['ip','-j',*args],text=True,capture_output=True,check=True,timeout=10)
        return json.loads(result.stdout)
    links=ip('link','show')
    assert len(links)==1 and links[0]['ifname']=='lo' and 'UP' in links[0]['flags'], 'non-loopback interface or disabled localhost'
    routes4=ip('-4','route','show','table','all');routes6=ip('-6','route','show','table','all')
    assert all(r.get('dev')=='lo' for r in routes4+routes6), 'non-loopback route exists'
    assert not any(r.get('dst')=='default' for r in routes4+routes6), 'default route exists'
    probes=[]
    for family,kind,address in [(socket.AF_INET,socket.SOCK_STREAM,('198.51.100.1',443)),
                                (socket.AF_INET,socket.SOCK_DGRAM,('203.0.113.1',443)),
                                (socket.AF_INET6,socket.SOCK_STREAM,('2001:db8::1',443))]:
        with socket.socket(family,kind) as client:
            client.settimeout(2)
            try:client.connect(address)
            except OSError as error:
                assert error.errno==errno.ENETUNREACH, 'egress probe did not fail with no-route'
                probes.append(dict(protocol='UDP' if kind==socket.SOCK_DGRAM else 'TCP',ipv6=family==socket.AF_INET6,errno=error.errno))
            else:raise AssertionError('external connection unexpectedly possible')
    return dict(network_namespace=current,outer_namespace=outer_namespace,loopback_only=True,default_routes=0,
                external_interfaces=0,egress_probes=probes,probe_payload_bytes=0)


# Audit baseline from the already captured Chrome process diagnostics. This
# never permits network access: the proxy still rejects every CONNECT.
BACKGROUND_URL_PATHS = {
    ('www.gstatic.com','/ohttp_gateway/hpke_public_keys/v1/sbc_prod'),
    ('accounts.google.com','/ListAccounts'),
    ('update.googleapis.com','/service/update2/json'),
    ('www.google.com','/async/folae'),
    ('www.google.com','/'),
    ('android.clients.google.com','/checkin'),
    ('www.google.com','/async/ddljson'),
    ('www.google.com','/async/newtab_ogb'),
    ('www.google.com','/complete/search'),
}


def external_transport_results(netlog):
    """Require the end/error result of every observed non-loopback connect."""
    kinds={v:k for k,v in netlog['constants']['logEventTypes'].items()}
    phases=netlog['constants']['logEventPhase']
    begin=phases.get('PHASE_BEGIN',phases.get('BEGIN'))
    end=phases.get('PHASE_END',phases.get('END'))
    assert begin is not None and end is not None and begin!=end,'NetLog phase schema unverifiable'
    pending={};attempts=successes=failures=unknown=0
    for event in netlog['events']:
        kind=kinds.get(event['type'])
        if kind not in {'TCP_CONNECT_ATTEMPT','UDP_CONNECT'}:continue
        params=event.get('params',{});address=params.get('address') or params.get('remote_address')
        key=(event['source']['id'],kind)
        if event['phase']==begin and isinstance(address,str):
            try:
                host=urlsplit('//'+address).hostname
                ip=ipaddress.ip_address(host)
                local=ip.is_loopback or bool(getattr(ip,'ipv4_mapped',None) and ip.ipv4_mapped.is_loopback)
            except (ValueError,TypeError):
                unknown+=1;continue
            if not local:
                if key in pending:unknown+=1
                pending[key]=True;attempts+=1
        elif event['phase']==end and key in pending:
            pending.pop(key)
            error=params.get('net_error',0)
            if not isinstance(error,int):unknown+=1
            elif error==0:successes+=1
            elif error<0:failures+=1
            else:unknown+=1
    return dict(external_connect_attempts=attempts,external_connect_successes=successes,
                external_connect_failures=failures,unverifiable_external_connects=unknown+len(pending))


def validate_offline_policy(network,browser_privacy,summary,transport,proxy_records,urls,leakage):
    """Only an evidenced, isolated synthetic visual run can accept blocked attempts."""
    assert network['network_namespace']!=network['outer_namespace'],'network namespace not isolated'
    assert network['loopback_only'] is True and network['external_interfaces']==network['default_routes']==0,'egress isolation invalid'
    assert len(network['egress_probes'])==3 and {(p['protocol'],p['ipv6']) for p in network['egress_probes']}=={('TCP',False),('UDP',False),('TCP',True)} and all(p['errno']==errno.ENETUNREACH for p in network['egress_probes']),'egress probes unverifiable'
    assert browser_privacy['browser_network_namespace']==network['network_namespace'],'browser namespace mismatch'
    assert browser_privacy['cookie_scope_checked'] is True,'authentication scope unverifiable'
    assert summary['page_level_external']==summary['cdp_external']==0,'page-origin external request'
    assert summary['raw_netlog_deleted'] is True,'raw log cleanup unverifiable'
    assert summary['proxy_external']==len(proxy_records),'incomplete proxy observation'
    assert transport['external_connect_successes']==transport['unverifiable_external_connects']==0,'external connect succeeded or unverifiable'
    assert transport['external_connect_attempts']==transport['external_connect_failures'],'incomplete transport observation'
    assert set(leakage)=={'proxy_canary_matches','proxy_forwarded_connections','external_url_canary_matches','request_canary_matches'},'incomplete private data observation'
    assert all(value==0 for value in leakage.values()),'private data exposure or proxy forwarding'
    proxy_counts={};url_counts={}
    known_hosts={host for host,_ in BACKGROUND_URL_PATHS}
    for record in proxy_records:
        assert record['method']=='CONNECT' and record['scheme']=='https' and record['port']=='443' and record['hostname'] in known_hosts,'unexpected proxy destination or method'
        key=(record['hostname'],record['port']);proxy_counts[key]=proxy_counts.get(key,0)+1
    for record in urls:
        assert record['scheme']=='https' and record['port']=='443' and (record['hostname'],record['path']) in BACKGROUND_URL_PATHS,'unexpected background URL'
        key=(record['hostname'],record['port'],record['path']);url_counts[key]=url_counts.get(key,0)+1
    return dict(policy='kernel_blocked_synthetic_visual_only',
        proxy_destinations=[dict(host=host,port=port,count=count,classification='previously_observed_Chrome_background_blocked') for (host,port),count in sorted(proxy_counts.items())],
        netlog_destinations=[dict(host=host,port=port,path=path,count=count,classification='previously_observed_Chrome_background_blocked') for (host,port,path),count in sorted(url_counts.items())])


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
        egress_safety=getattr(self,'egress_safety',False)
        network_proof=isolated_network_evidence(self.outer_namespace) if egress_safety else None
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
        private_canaries=[];privacy=dict(proxy_canary_matches=0,proxy_forwarded_connections=0)
        class DenyProxy(BaseHTTPRequestHandler):
            def deny(self):
                if egress_safety:
                    # Examine in memory only. No header, body or canary value
                    # is copied to diagnostics. CONNECT is never forwarded.
                    transient=self.path+'\n'+str(self.headers)
                    privacy['proxy_canary_matches']+=sum(bool(s and s in transient) for s in private_canaries)
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
            if egress_safety:
                import secrets
                business_canary='SYNTHETIC_PRIVATE_RECEIPT_'+secrets.token_hex(12)
                h.snapshot[-1]['display']['source_name']+=' '+business_canary
                h.snapshot[-1]['display']['unsent_inputs'][0]['value']+=' '+business_canary
                private_canaries.extend([business_canary,h.ticket.csrf,h.ticket.nonce,h.ticket.pkce_verifier,h.ticket.state])
                private_canaries.extend(h.client.get_cookie(n,domain=DOMAIN).value for n in (h.web.COOKIE,h.web.LOGIN_COOKIE))
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
            if egress_safety:
                config.update(egress_safety=True,egress_namespace=network_proof['network_namespace'],
                              private_canaries=private_canaries,business_canary=business_canary)
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
            external_url_canary_matches=0
            def inspect(value):
                nonlocal external_url_canary_matches
                if isinstance(value,dict):
                    for k,v in value.items():
                        if k in {'url','original_url'} and isinstance(v,str):
                            parsed=urlsplit(v)
                            if parsed.scheme in {'http','https','ws','wss'} and (parsed.scheme+'://'+parsed.netloc) != base:
                                if egress_safety:external_url_canary_matches+=sum(bool(s and s in v) for s in private_canaries)
                                violations.append('browser netlog contains non-localhost URL')
                                netlog_url_violations.append(safe_url(v))
                        inspect(v)
                elif isinstance(value,list):
                    for v in value: inspect(v)
            inspect(netlog)
            transport_results=external_transport_results(netlog) if egress_safety else None
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
                 'network-records.json','network-summary.json','font-inventory.json','computed-fonts.json','viewport-metrics.json','safety-counts.json'}
                | ({'browser-privacy.json'} if egress_safety else set()))
            if egress_safety:
                browser_privacy=json.loads((output/'browser-privacy.json').read_text('utf-8'))
                audit=validate_offline_policy(network_proof,browser_privacy,summary,transport_results,proxy_records,netlog_url_violations,
                    dict(proxy_canary_matches=privacy['proxy_canary_matches'],proxy_forwarded_connections=privacy['proxy_forwarded_connections'],
                         external_url_canary_matches=external_url_canary_matches,request_canary_matches=browser_privacy['external_canary_matches']))
                proof=dict(network=network_proof,**privacy,external_url_canary_matches=external_url_canary_matches,
                    browser_privacy=browser_privacy,raw_netlog_deleted=summary['raw_netlog_deleted'],
                    rendered_viewports=4,state_and_inputs_unchanged=after==before,
                    transport_results=transport_results,background_audit=audit,policy_scope='isolated offline synthetic visual test only')
                (output/'egress-safety.json').write_text(json.dumps(proof,indent=2),encoding='utf-8')
                assert not privacy['proxy_canary_matches'] and not external_url_canary_matches and not browser_privacy['external_canary_matches'], 'private canary appeared in external request evidence'
                assert browser_privacy['cookie_scope_checked'] and network_proof['external_interfaces']==0 and summary['raw_netlog_deleted'], 'isolation or privacy evidence incomplete'
                print('OFFLINE SAFETY POLICY PASSED '+json.dumps(transport_results),flush=True)
            self.assertEqual(summary['page_level_external'],0,'page external request attempted')
            self.assertEqual(summary['cdp_external'],0,'CDP external request attempted')
            if egress_safety:
                # Only the three audited, blocked-background observations are
                # exempted after the complete policy proof passes. Everything
                # else (including all write/display calls) remains fatal.
                observed_background={'browser attempted non-loopback proxy request','browser netlog contains non-localhost URL','browser transport attempted non-loopback endpoint'}
                self.assertEqual([v for v in violations if v not in observed_background],[],'unexpected offline safety violation')
            else:self.assertEqual(violations,[],'network or write boundary violated')
            print('GET/render/resize writes=0; accounting=0; AI=0; move=0; production authority=0; screenshots=4')


def socket_process_owners(client_port, proxy_port, pid_file):
    """Read-only socket inode attribution, restricted to this Chrome subtree."""
    try:
        root_pid = int(pid_file.read_text())
    except (OSError, ValueError):
        return []
    inodes = set()
    for table in ('tcp', 'tcp6'):
        try:
            for row in Path('/proc/net/'+table).read_text().splitlines()[1:]:
                cells = row.split()
                if int(cells[1].split(':')[-1], 16) == client_port and int(cells[2].split(':')[-1], 16) == proxy_port:
                    inodes.add(cells[9])
        except OSError:
            pass
    processes = {}
    for entry in Path('/proc').iterdir():
        if entry.name.isdigit():
            try:
                fields = (entry/'stat').read_text().rsplit(')', 1)[1].split()
                processes[int(entry.name)] = (int(fields[1]), os.readlink(entry/'exe'))
            except OSError:
                pass
    descendants = {root_pid}
    while True:
        found = {pid for pid, (parent, _) in processes.items() if parent in descendants}
        if found <= descendants:
            break
        descendants |= found
    owners = []
    for pid in descendants:
        try:
            for fd in Path('/proc/'+str(pid)+'/fd').iterdir():
                try:
                    link = os.readlink(fd)
                except OSError:
                    continue
                if link.startswith('socket:[') and link[8:-1] in inodes:
                    args = Path('/proc/'+str(pid)+'/cmdline').read_bytes().decode('utf-8', 'replace').split('\0')
                    role = [a for a in args if a.startswith(('--type=', '--utility-sub-type='))]
                    owners.append(dict(pid=pid, parent_pid=processes[pid][0], exe=processes[pid][1], role=role,
                                       socket_inode=link[8:-1]))
        except OSError:
            pass
    return owners


def extract_phase_netlog(raw_file, proxy_records):
    """No params dictionaries, headers, bodies or raw bytes leave this function."""
    try:
        raw = json.loads(raw_file.read_text('utf-8'))
    finally:
        raw_file.unlink(missing_ok=True)
    names = {value: key for key, value in raw['constants']['logEventTypes'].items()}
    source_names = {value: key for key, value in raw['constants']['logSourceType'].items()}
    hosts = {r['hostname'] for r in proxy_records}
    def dependencies(value):
        found = []
        if isinstance(value, dict):
            for key, item in value.items():
                if key == 'source_dependency' and isinstance(item, dict):
                    found.append(dict(id=item.get('id'), type=source_names.get(item.get('type'), 'unknown')))
                else:
                    found.extend(dependencies(item))
        elif isinstance(value, list):
            for item in value:
                found.extend(dependencies(item))
        return found
    projected, selected = [], set()
    for event in raw['events']:
        params = event.get('params', {})
        source = event['source']
        urls = [safe_url(params[key]) for key in ('url', 'original_url') if isinstance(params.get(key), str)]
        item = dict(event_type=names.get(event['type'], 'unknown'), source_type=source_names.get(source['type'], 'unknown'),
                    source_id=source['id'], source_dependency=dependencies(params), timestamp=event['time'],
                    urls=urls, error_code=params.get('net_error') if isinstance(params.get('net_error'), int) else None)
        if any(url['hostname'] in hosts for url in urls):
            selected.add(source['id'])
        projected.append(item)
    # Dependency-connected events only; params are never copied wholesale.
    while True:
        related = set(selected)
        for item in projected:
            deps = {d['id'] for d in item['source_dependency']}
            if item['source_id'] in selected or deps & selected:
                related.add(item['source_id']);related |= deps
        if related == selected:
            break
        selected = related
    return dict(capture_mode='Default', raw_deleted=not raw_file.exists(),
                events=[item for item in projected if item['source_id'] in selected])


def chromium_source_map(binary, urls):
    """Inspect official source at the binary tag/revision, never execute it."""
    import base64
    from urllib.request import build_opener, ProxyHandler
    product = binary['product']
    version = re.search(r'\d+\.\d+\.\d+\.\d+', product).group()
    ref = 'refs/tags/'+version
    opener = build_opener(ProxyHandler({}))
    cache = {}
    def source(path):
        if path not in cache:
            address = 'https://chromium.googlesource.com/chromium/src/+/'+ref+'/'+path
            try:
                with opener.open(address+'?format=TEXT', timeout=10) as response:
                    assert urlsplit(response.geturl()).hostname == 'chromium.googlesource.com'
                    cache[path] = (base64.b64decode(response.read()).decode('utf-8'), address)
            except Exception:
                cache[path] = ('', address)
        return cache[path]
    candidates = {
        '/ListAccounts': [('google_apis/gaia/gaia_urls.cc', 'ListAccounts', 'GaiaUrls::ListAccountsURLWithSource'),
                          ('components/signin/internal/identity_manager/gaia_cookie_manager_service.cc', 'ListAccounts', 'GaiaCookieManagerService')],
        '/async/folae': [('components/omnibox/browser/aim_eligibility_service.cc', '/async/folae', 'AimEligibilityService::StartServerEligibilityRequest'),
                        ('chrome/browser/autocomplete/aim_eligibility_service.cc', '/async/folae', 'AimEligibilityService')],
        '/ohttp_gateway/hpke_public_keys/v1/sbc_prod': [('components/safe_browsing/core/common/features.cc', '/ohttp_gateway/hpke_public_keys/v1/sbc_prod', 'kHashPrefixRealTimeLookupsKeyFetchUrl')],
        '/service/update2/json': [('components/component_updater/component_updater_url_constants.cc', '/service/update2/json', 'kUpdaterJSONDefaultUrl'),
                                 ('chrome/updater/constants.cc', '/service/update2/json', 'kUpdaterJSONDefaultUrl')],
    }
    mapping = []
    for url in sorted(urls):
        path = urlsplit(url).path
        evidence = []
        for file, literal, symbol in candidates.get(path, []):
            text, address = source(file)
            if literal in text and symbol in text:
                evidence.append(dict(source_path=file, symbol=symbol, source_url=address,
                                     literal_line=next(i for i, line in enumerate(text.splitlines(), 1) if literal in line),
                                     meaning='source definition/caller evidence; not a captured runtime initiator'))
        mapping.append(dict(url=url, classification='source_file_and_symbol_confirmed' if evidence else 'unmapped', evidence=evidence))
    return dict(binary_product=product, source_ref=ref, mapping=mapping)


def process_phase_diagnostics(suite):
    assert sys.platform == 'linux', 'Linux process diagnostics required'
    redaction_self_test()
    chrome = next((shutil.which(n) for n in ('google-chrome','google-chrome-stable','chromium','chromium-browser') if shutil.which(n)), None)
    node = shutil.which('node');assert chrome and node, 'existing browser missing; do not install'
    check = subprocess.run([node, str(ROOT/'tests/shadow_visual_browser.cjs'), '--redaction-self-test'], capture_output=True, timeout=20)
    assert check.returncode == 0, 'redaction self-test failed'
    output = ROOT/'artifacts/shadow-process';output.mkdir(parents=True, exist_ok=False)
    phases, binaries, logs, cleanup = [], [], [], []
    seconds = 8
    def persist():
        comparison = {}
        for label in 'ABCD':
            runs = [p for p in phases if p['phase']==label]
            if len(runs)==2:
                sets = [{(r['hostname'],r['port'],r['path']) for r in p['records'] if r['layer']=='proxy' and r['in_observation_window']} for p in runs]
                comparison[label] = dict(common_destinations=sorted(sets[0]&sets[1]),first_only=sorted(sets[0]-sets[1]),second_only=sorted(sets[1]-sets[0]),
                                         counts=[p['proxy_external'] for p in runs])
        for name, value in [('phase-records.json',dict(suite=suite,observation_seconds=seconds,phases=phases,repeat_comparison=comparison)),
                            ('process-binaries.json',binaries),('netlog-summary.json',logs),('cleanup-confirmation.json',cleanup)]:
            (output/name).write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8')
    for phase in 'ABCD':
        for repeat in (1, 2):
            records = []
            with tempfile.TemporaryDirectory(prefix='shadow-phase-'+phase+'-') as temporary, pytest.MonkeyPatch.context() as patch:
                folder = Path(temporary);profile = folder/'profile';profile.mkdir()
                pid_file = folder/'browser.pid';result_file = folder/'phase-result.json';raw_file = folder/'netlog.json'
                server = None;host = None;violations = []
                if phase == 'D':
                    host = HostRig(patch);assert host.login().status_code == 303
                    add_native_unit_cards(host)
                    before = deepcopy(host.db.data),deepcopy(host.db.versions),host.db.writes,host.f.io.raw,host.f.io.writes,host.f.state_io.raw,host.f.state_io.writes,deepcopy(host.snapshot)
                    def deny_write(*_args, **_kwargs):
                        violations.append('write/display attempted');raise AssertionError('read-only boundary')
                    patch.setattr(Document,'create',deny_write);patch.setattr(Document,'update',deny_write)
                    patch.setattr(host.f.io,'replace_versioned',deny_write);patch.setattr(host.f.state_io,'replace_versioned',deny_write)
                    patch.setattr(host.f.grouping,'display',deny_write);patch.setattr(host.f.port,'submit_native',deny_write)
                    from google.cloud import firestore
                    from googleapiclient import discovery
                    patch.setattr(firestore,'Client',deny_write);patch.setattr(discovery,'build',deny_write)
                    tls, spki = synthetic_tls(folder)
                    server = make_server('127.0.0.1',0,host.app,ssl_context=tls,request_handler=QuietHandler)
                    Thread(target=server.serve_forever,daemon=True).start()
                    base = 'https://127.0.0.1:'+str(server.server_port)
                    cookies = [dict(name=n,value=host.client.get_cookie(n,domain=DOMAIN).value,url=base,path='/',secure=True,httpOnly=True,sameSite='Lax') for n in (host.web.COOKIE,host.web.LOGIN_COOKIE)]
                else:
                    base = 'https://127.0.0.1:1';cookies = [];_, spki = synthetic_tls(folder)
                class Proxy(BaseHTTPRequestHandler):
                    def deny(self):
                        stamp = int(time.time()*1000)
                        url = safe_url('https://'+self.path if self.command == 'CONNECT' else self.path)
                        if self.command == 'CONNECT': url['path'] = 'unknown'
                        records.append(dict(sequence=len(records)+1,layer='proxy',time_ms=stamp,method=self.command,**url,
                            resource_type='unknown',initiator_type='unknown',document_url='unknown',redirect_from='unknown',
                            initiator_stack=[],frame_url='unknown',allowed=False,
                            socket_owners=socket_process_owners(self.client_address[1],self.server.server_port,pid_file)))
                        self.send_error(403)
                    do_CONNECT=do_GET=do_POST=deny
                    def log_message(self,*_args): pass
                proxy = ThreadingHTTPServer(('127.0.0.1',0),Proxy);thread = Thread(target=proxy.serve_forever,daemon=True);thread.start()
                started = int(time.time()*1000);deadline = started+seconds*1000
                result = dict(records=[],milestones=[],tracked_pids=[],remaining_running_pids=[],phase_error=False)
                code = 0
                try:
                    if phase == 'A':time.sleep(seconds)
                    else:
                        config = dict(phase=phase,chrome=chrome,profile=str(profile),netlog=str(raw_file),pid_file=str(pid_file),
                            result_file=str(result_file),proxy='http://127.0.0.1:'+str(proxy.server_port),base=base,spki=spki,
                            cookies=cookies,detail_id='isolated-native-units-2',deadline_ms=deadline)
                        execution = subprocess.run([node,str(ROOT/'tests/shadow_visual_browser.cjs')],input=json.dumps(config),text=True,capture_output=True,timeout=40)
                        code = execution.returncode
                        if result_file.exists():result=json.loads(result_file.read_text('utf-8'))
                finally:
                    proxy.shutdown();proxy.server_close();thread.join(timeout=5)
                    if server:server.shutdown();server.server_close()
                if phase != 'A' and raw_file.exists():logs.append(dict(phase=phase,repeat=repeat,**extract_phase_netlog(raw_file,records)))
                cleanup.append(dict(phase=phase,repeat=repeat,raw_netlog_deleted=not raw_file.exists(),
                    remaining_running_pids=result['remaining_running_pids']))
                if result.get('binary'):binaries.append(dict(phase=phase,repeat=repeat,**result['binary']))
                combined = result['records']+records;combined.sort(key=lambda r:r['time_ms'])
                for index,r in enumerate(combined,1):r['sequence']=index;r['in_observation_window']=started<=r['time_ms']<=deadline
                measured = dict(phase=phase,repeat=repeat,proxy_monitor_started_ms=started,observation_deadline_ms=deadline,
                    milestones=result['milestones'],records=combined,
                    proxy_external=sum(r['layer']=='proxy' and r['in_observation_window'] for r in combined),
                    proxy_cleanup_external=sum(r['layer']=='proxy' and not r['in_observation_window'] for r in combined),
                    page_external=sum(r['layer']=='page-fetch' and not r['allowed'] for r in combined),
                    cdp_external=sum(r['layer']=='cdp-network' and not r['allowed'] for r in combined),
                    first_request_ms=min((r['time_ms'] for r in combined),default=None),phase_error=result['phase_error'] or code!=0)
                if host:
                    after = deepcopy(host.db.data),deepcopy(host.db.versions),host.db.writes,host.f.io.raw,host.f.io.writes,host.f.state_io.raw,host.f.state_io.writes,deepcopy(host.snapshot)
                    measured['write_check']=dict(state_unchanged=before==after,write_spy_calls=len(violations),accounting=host.f.accounting_appends,
                        ai=host.f.ai_sends,source_move=host.f.source_moves,production_authority=0)
                phases.append(measured);persist()
                print('PHASE '+json.dumps({k:measured[k] for k in ('phase','repeat','proxy_external','proxy_cleanup_external','page_external','cdp_external','phase_error')}),flush=True)
                assert not measured['phase_error'] and not result['remaining_running_pids'], 'phase setup or process cleanup failed'
                if host:assert before==after and not violations and (host.f.accounting_appends,host.f.ai_sends,host.f.source_moves)==(0,0,0),'write detected'
                if phase=='A':assert not records,'Phase A external request: Chrome-external process; stop'
    urls={u['url'] for log in logs for e in log['events'] for u in e['urls'] if u['hostname'] in {r['hostname'] for p in phases for r in p['records'] if r['layer']=='proxy'}}
    mapping=chromium_source_map(binaries[0],urls) if binaries else dict(mapping=[])
    (output/'source-correspondence.json').write_text(json.dumps(mapping,ensure_ascii=False,indent=2),encoding='utf-8')
    # Intentionally fail after all authorized diagnostics are persisted. No
    # diagnostic result is promoted to successful visual verification.
    assert not any(p['proxy_external'] or p['proxy_cleanup_external'] or p['page_external'] or p['cdp_external'] for p in phases), 'external requests remain; diagnosis saved, stop'


if __name__=='__main__':
    if '--egress-safety' in sys.argv:
        test=ShadowVisualCheck('test_pc_390_list_detail_and_no_writes')
        test.egress_safety=True;test.outer_namespace=sys.argv[sys.argv.index('--outer-netns')+1]
        test.test_pc_390_list_detail_and_no_writes()
    elif '--process-phases' in sys.argv:
        suite=sys.argv[sys.argv.index('--suite')+1]
        assert suite in {'general','sensitive-synthetic'}
        process_phase_diagnostics(suite)
    else:unittest.main(verbosity=2)
