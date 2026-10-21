"""JSON-only opt-in adapter on the existing synthetic authenticated host.

No UI, financial endpoint, client construction or production registration.
Fresh observation is explicit POST; GET is saved result lookup only.
"""
import json

from flask import Blueprint, jsonify, request

from .drive_run_state import StateError
from .iphone_persistent_drafts import unique
from .unified_receipt_review import digest
from .unified_receipt_shadow import ExistingHostSessionGate, ShadowConfig

PREFIX = '/isolated-synthetic/iphone-sheet-handoff'


def make_isolated_sheet_handoff_application(make_application, runtime_factory, adapter, *,
                                           config=ShadowConfig(), cookies=None):
    if config.enabled is not True:
        return make_application()
    from staging_shadow.iphone_sheet_handoff import SheetDraftHandoff
    if config.environment != 'isolated_synthetic' or type(adapter) is not SheetDraftHandoff or cookies is None:
        raise StateError('sheet_handoff_isolation_required')
    rt = runtime_factory()
    if rt.settings.get('mode') != 'synthetic_only' or getattr(rt.client, 'shadow_memory_only', False) is not True:
        raise StateError('sheet_handoff_isolation_required')
    app = make_application()
    if any(r.rule.startswith(PREFIX) for r in app.url_map.iter_rules()):
        raise StateError('sheet_handoff_route_collision')
    gate = ExistingHostSessionGate(runtime_factory, cookies)
    bp = Blueprint('iphone_sheet_handoff', __name__, url_prefix=PREFIX)

    @bp.before_request
    def security():
        adapter.confirmation._session(gate)
        if request.headers.get('Origin') not in (None, rt.origin) or request.headers.get('Sec-Fetch-Site') == 'cross-site':
            raise StateError('sheet_handoff_origin_invalid')
        if request.method == 'POST':
            gate.post({}, 'METADATA_REVIEW', 'http-request', digest(request.path), 0)
            if request.mimetype != 'application/json' or request.content_length is None or request.content_length > 800000:
                raise StateError('sheet_handoff_request_invalid')
            request.max_content_length = 800000

    @bp.after_request
    def privacy(response):
        response.headers['Cache-Control'] = 'no-store'
        response.headers['Referrer-Policy'] = 'no-referrer'
        response.headers['Content-Security-Policy'] = "default-src 'none'; frame-ancestors 'none'; base-uri 'none'"
        return response

    @bp.errorhandler(Exception)
    def rejected(error):
        # Only data-free StateError codes escape; no input or traceback echo.
        code = str(error) if type(error).__name__ == 'StateError' else 'sheet_handoff_UNKNOWN'
        status = 410 if 'expired' in code else 403 if any(k in code for k in ('owner', 'session', 'csrf', 'origin', 'authenticated')) else 503 if any(k in code for k in ('UNKNOWN', 'unknown', 'quarantin')) else 409
        return jsonify(status='UNKNOWN' if status == 503 else 'REJECTED', error=code,
            input_preserved=True, accounting_permission=False, writer_dispatches=0), status

    def body():
        try:
            value = json.loads(request.get_data(), object_pairs_hook=unique)
            if type(value) is not dict:
                raise ValueError()
            return value
        except (ValueError, TypeError):
            raise StateError('sheet_handoff_request_invalid')

    @bp.post('/prepare/<cid>/<action>')
    def prepare(cid, action):
        if body() != {}:
            raise StateError('sheet_handoff_request_invalid')
        return jsonify(adapter.prepare(cid, action, gate))

    @bp.post('/request')
    def post():
        return jsonify(adapter.post(body(), gate))

    @bp.get('/results/<rid>')
    def result(rid):
        return jsonify(adapter.inspect(rid, gate))

    app.register_blueprint(bp)
    return app
