"""Small HTTPS-only synthetic confirmation UI; GET never changes durable state."""
from hashlib import sha256
import hmac
import secrets
import time
from urllib.parse import urlsplit

from flask import Flask, Response, request, redirect
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
from markupsafe import escape
from werkzeug.exceptions import HTTPException

from app.drive_run_state import StateError
from app.human_general_auth_transport import UUID, TTL

COOKIE = '__Host-hga'
START_COOKIE = '__Host-hga-start'


def screen(text, content=''):
    return ('<!doctype html><html lang="ja"><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<title>一般レシートの確認</title><style>'
        'body{margin:0;background:#f4f6f7;color:#152b32;font:18px system-ui;overflow-wrap:anywhere}'
        'main{max-width:420px;margin:auto;padding:24px 18px}h1{font-size:24px}'
        'button,a{display:block;box-sizing:border-box;width:100%;padding:16px;margin-top:18px;'
        'font:inherit;text-align:center;border-radius:10px}button{background:#166450;color:white;'
        'border:0;min-height:54px}a{color:#166450}small{font-size:14px}</style>'
        '<main><h1>'+str(escape(text))+'</h1>'+content+'</main></html>')


def create_app(runtime_factory):
    app = Flask(__name__)
    app.config['MAX_CONTENT_LENGTH'] = 16384

    def runtime():
        return runtime_factory()

    def own_origin(rt):
        if request.headers.get('Origin') != rt.origin:
            raise StateError('human_general_auth_csrf_invalid')

    def rid(value):
        if not isinstance(value, str) or not UUID.fullmatch(value):
            raise StateError('human_general_auth_request_invalid')
        return value

    def ticket(rt):
        value = rt.tickets.load(request.cookies.get(COOKIE))
        return value

    @app.before_request
    def https_only():
        # Cloud Run terminates TLS. Origin is fixed from private config, never
        # inferred from the client Host/X-Forwarded-Host header.
        if request.path != '/health' and request.scheme != 'https' and request.headers.get('X-Forwarded-Proto') != 'https':
            return Response(screen('安全な接続で開いてください'), status=400)

    @app.after_request
    def headers(response):
        response.headers.update({
            # Form POSTs under no-referrer carry Origin:null in real browsers.
            # Preserve their Origin without exposing any URL path/query.
            'Cache-Control': 'no-store', 'Pragma': 'no-cache', 'Referrer-Policy': 'strict-origin',
            'X-Content-Type-Options': 'nosniff', 'X-Frame-Options': 'DENY',
            'Content-Security-Policy': "default-src 'none'; style-src 'unsafe-inline'; form-action 'self' https://accounts.google.com; frame-ancestors 'none'; base-uri 'none'",
            'Strict-Transport-Security': 'max-age=31536000',
            'Permissions-Policy': 'camera=(), microphone=(), geolocation=()'})
        return response

    @app.errorhandler(StateError)
    def rejected(error):
        code = str(error)
        if code == 'HTTP_412':
            message, status = '現在の状態が変わりました。最初から確認してください', 412
        elif code.endswith('identity_rejected'):
            message, status = 'このアカウントでは確定できません', 403
        elif code.endswith('request_expired'):
            message, status = '確認期限が切れました', 410
        elif code.endswith('replay_or_unknown_request'):
            message, status = 'この確認はすでに使用されているか、無効です', 409
        elif 'stale' in code or 'source_changed' in code:
            message, status = 'このページは現在の状態と一致しません', 409
        else:
            message, status = '確認を完了できませんでした。再追記せず状態を確認してください', 400
        return Response(screen(message), status=status)

    @app.errorhandler(Exception)
    def unavailable(_error):
        # Never emit raw error/traceback/request data into logs or HTML.
        return Response(screen('現在、確認を利用できません'), status=503)

    @app.errorhandler(HTTPException)
    def http_failure(error):
        return Response(screen('この操作は利用できません'), status=error.code)

    @app.route('/health', methods=['GET'])
    def health():
        return {'status': 'ok', 'mode': 'synthetic_only'}

    @app.route('/start', methods=['GET', 'POST'])
    def start():
        rt = runtime()
        value = rid(request.args.get('request') if request.method == 'GET' else request.form.get('request'))
        proof = request.args.get('proof') if request.method == 'GET' else request.form.get('proof')
        try:
            link = URLSafeTimedSerializer(rt.key, salt='hga-link-v1').loads(proof or '', max_age=TTL)
            if link != {'request': value}:
                raise ValueError()
        except (ValueError, BadSignature, SignatureExpired):
            raise StateError('human_general_auth_request_invalid') from None
        signer = URLSafeTimedSerializer(rt.key, salt='hga-start-v1')
        if request.method == 'GET':
            gateway = rt.gateway(value)
            gateway.record(value, 'prepared')
            nonce = secrets.token_urlsafe(32)
            token = signer.dumps({'request': value, 'nonce_hash': sha256(nonce.encode()).hexdigest()})
            response = Response(screen('一般レシートの確認',
                '<p>対象：synthetic page</p><p>Googleで本人確認後、明示的に確定します。</p>'
                '<form method="post" action="/start"><input type="hidden" name="request" value="'+value+'">'
                '<input type="hidden" name="proof" value="'+str(escape(proof))+'">'
                '<input type="hidden" name="csrf" value="'+str(escape(token))+'">'
                '<button>Googleで本人確認する</button></form>'))
            response.set_cookie(START_COOKIE, nonce, max_age=TTL, secure=True, httponly=True, samesite='Lax', path='/')
            return response
        own_origin(rt)
        try:
            form = signer.loads(request.form.get('csrf', ''), max_age=TTL)
            cookie = request.cookies.get(START_COOKIE, '')
            if form['request'] != value or not cookie or not hmac.compare_digest(form['nonce_hash'], sha256(cookie.encode()).hexdigest()):
                raise ValueError()
        except (ValueError, KeyError, BadSignature, SignatureExpired):
            raise StateError('human_general_auth_csrf_invalid') from None
        session = rt.begin(value)
        response = redirect(session.authorization_url, code=303)
        # form_post from Google is cross-site; None is required here. State,
        # nonce, PKCE, callback Origin and explicit-confirm CSRF still apply.
        response.set_cookie(COOKIE, session.cookie, max_age=TTL, secure=True, httponly=True, samesite='None', path='/')
        response.delete_cookie(START_COOKIE, secure=True, httponly=True, samesite='Lax', path='/')
        return response

    @app.route('/oauth/callback', methods=['POST'])
    def callback():
        if request.headers.get('Origin') != 'https://accounts.google.com':
            raise StateError('human_general_auth_oauth_state_invalid')
        rt = runtime()
        session = ticket(rt)
        if request.form.get('error'):
            raise StateError('human_general_auth_identity_rejected')
        rt.gateway(session.request_id).callback(session, cookie=request.cookies.get(COOKIE),
            state=request.form.get('state'), code=request.form.get('code'))
        return redirect('/confirm', code=303)

    @app.route('/confirm', methods=['GET', 'POST'])
    def confirm():
        rt = runtime()
        session = ticket(rt)
        gateway = rt.gateway(session.request_id)
        if request.method == 'GET':
            _, record = gateway.record(session.request_id, 'authenticated')
            gateway.browser(record, session, request.cookies.get(COOKIE))
            gateway.fresh(record)
            _, tag = rt.state(session.request_id, 'authorities').read_versioned()
            return Response(screen('一般レシートとして確定',
                '<p>対象：synthetic page</p><p>Googleアカウント：確認済み</p>'
                '<p>一般レシートであることを確認し、このページだけをGeminiへ送信して解析することを許可します。</p>'
                '<small>このテストでは会計処理を実行しません。</small>'
                '<form method="post" action="/confirm"><input type="hidden" name="csrf" value="'+str(escape(session.csrf))+'">'
                '<input type="hidden" name="etag" value="'+str(escape(tag))+'">'
                '<button name="action" value="confirm">一般レシートとして確定</button>'
                '<button name="action" value="cancel">キャンセル</button></form>'))
        own_origin(rt)
        if request.form.get('action') == 'cancel':
            if not hmac.compare_digest(request.form.get('csrf', ''), session.csrf):
                raise StateError('human_general_auth_csrf_invalid')
            response = Response(screen('キャンセルしました'))
            response.delete_cookie(COOKIE, secure=True, httponly=True, samesite='None', path='/')
            return response
        if request.form.get('action') != 'confirm':
            raise StateError('human_general_auth_csrf_invalid')
        if not request.form.get('etag'):
            raise StateError('HTTP_412')
        gateway.confirm(session, cookie=request.cookies.get(COOKIE), csrf=request.form.get('csrf'),
            origin=request.headers.get('Origin'), method='POST',
            confirmation_factory=rt.factory(session.request_id, expected_tag=request.form['etag']))
        return redirect('/result', code=303)

    @app.route('/result', methods=['GET'])
    def result():
        rt = runtime()
        session = ticket(rt)
        gateway = rt.gateway(session.request_id)
        _, record = gateway.record(session.request_id, 'complete')
        gateway.browser(record, session, request.cookies.get(COOKIE))
        page = gateway.fresh(record)
        grant = rt.factory(session.request_id)(None).current(page)
        if grant['confirmation_digest'] != record['authority_digest']:
            raise StateError('synthetic_readback_mismatch')
        return Response(screen('確認が完了しました', '<p>synthetic確認を保存しました。</p><p>会計処理は実行していません。</p>'))

    return app


def make_application():
    from .runtime import configured_runtime
    return create_app(configured_runtime)
