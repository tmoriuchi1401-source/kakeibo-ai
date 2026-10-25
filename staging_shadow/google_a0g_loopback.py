"""One-shot Desktop callback. No token exchange, logging or persistence."""
from http.server import BaseHTTPRequestHandler, HTTPServer
import re
import socket
import threading
import time
from urllib.parse import parse_qsl, urlsplit

from .google_a0g import fail, validate_callback


def parse_callback(target, *, contract, expected_sha, state):
    if not isinstance(target,str) or len(target)>8192 or not target.startswith('/oauth2callback?'):
        fail('callback_path')
    u = urlsplit(target)
    if u.scheme or u.netloc or u.path!='/oauth2callback' or u.fragment:
        fail('callback_path')
    if re.search(r'%(?![0-9A-Fa-f]{2})',u.query): fail('callback_query')
    try:
        pairs = parse_qsl(u.query,keep_blank_values=True,strict_parsing=True,max_num_fields=8,
                          encoding='utf8',errors='strict')
    except (ValueError,UnicodeError):
        fail('callback_query')
    values = {}
    for k,v in pairs:
        if k in values: fail('callback_duplicate_key')
        values[k] = v
    if 'error' in values: fail('authorization_denied')
    if set(values)!={'state','code','scope','picked_file_ids'}:
        fail('callback_fields')
    picked = values['picked_file_ids'].split(',')
    scopes = values['scope'].split(' ')
    validate_callback(contract,expected_sha=expected_sha,expected_state=state,
                      returned_state=values['state'],picked_file_ids=picked,scopes=scopes)
    code = values['code']
    if not code or len(code)>4096 or not code.isascii() or any(ch.isspace() for ch in code):
        fail('callback_code')
    return dict(code=code,state=values['state'],scopes=scopes,picked_file_ids=picked)


class LoopbackReceiver:
    """Bind BEFORE opening the system browser; receive exactly one request.

    The absolute watchdog also shuts down a slow accepted connection. HTTP
    request paths/headers are never logged or reflected. No remote listener.
    """
    def __init__(self, *, contract, expected_sha, state, seconds):
        if not 0 < seconds <= 120: fail('callback_deadline')
        self._contract,self._sha,self._state = contract,expected_sha,state
        self._deadline = time.monotonic()+seconds
        self._value = None; self._error = False; self._used = False
        self._connection = None; self._lock = threading.Lock()
        receiver = self
        class Server(HTTPServer):
            allow_reuse_address = False
            def get_request(self):
                conn,addr = super().get_request()
                receiver._connection = conn
                conn.settimeout(max(.001,min(1,receiver._deadline-time.monotonic())))
                return conn,addr
            def handle_error(self,*args): receiver._error = True
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args): pass
            def log_error(self,*args): pass
            def send_error(self,*args,**kwargs):
                receiver._error = True
                self.send_response_only(400)
                self.send_header('Content-Length','0')
                self.send_header('Connection','close')
                self.end_headers()
            def do_GET(self):
                try:
                    receiver.accept(self.path)
                    self.send_response_only(303)
                    self.send_header('Location','/complete')
                    self.send_header('Cache-Control','no-store')
                    self.send_header('Referrer-Policy','no-referrer')
                    self.send_header('Content-Security-Policy',"default-src 'none'")
                    self.send_header('Content-Length','0')
                    self.send_header('Connection','close')
                    self.end_headers()
                except Exception:
                    receiver._error = True
                    self.send_error(400)
            def do_POST(self): self.send_error(400)
            def do_HEAD(self): self.send_error(400)
        self._server = Server(('127.0.0.1',0),Handler)
        self._server.timeout = seconds
        port = self._server.server_address[1]
        self.redirect_uri = f'http://127.0.0.1:{port}/oauth2callback'
        self._timer = threading.Timer(seconds,self._abort)
        self._timer.daemon = True
        self._timer.start()

    def accept(self,target):
        with self._lock:
            if self._used or self._error or time.monotonic()>=self._deadline:
                self._value = None; self._error = True
                fail('callback_consumed_or_expired')
            self._used = True  # consume before parsing, including invalid/denied
            try:
                self._value = parse_callback(target,contract=self._contract,expected_sha=self._sha,state=self._state)
            except BaseException:
                self._error = True
                raise

    def _abort(self):
        self._error = True
        self._value = None
        conn = self._connection
        if conn is not None:
            try: conn.shutdown(socket.SHUT_RDWR)
            except OSError: pass
            try: conn.close()
            except OSError: pass
        self._server.server_close()

    def receive(self):
        try:
            self._server.handle_request()
            if self._error or self._value is None or time.monotonic()>=self._deadline:
                fail('callback_denied_or_timeout')
            value,self._value = self._value,None
            return value
        finally:
            self.close()

    def close(self):
        self._timer.cancel()
        self._abort()
        self._value = None
        self._state = None
        self._contract = None
