"""Opt-in handoff UI/API on the existing synthetic authenticated host.

No financial endpoint, client construction or production registration.
Fresh observation is explicit POST; GET displays saved data only.
"""
from hashlib import sha256
import json
from pathlib import Path

from flask import Blueprint, Response, jsonify, render_template, request

from .drive_run_state import StateError
from .iphone_persistent_drafts import unique
from .unified_receipt_review import digest
from .unified_receipt_shadow import ExistingHostSessionGate, ShadowConfig

PREFIX = '/isolated-synthetic/iphone-sheet-handoff'
LABELS = dict(DRAFT_IMPORTED='下書き取込済み・未確定',
              FINANCIAL_BOUNDARY_STOPPED='確認済み・未記帳', UNKNOWN='UNKNOWN：結果不明・要照合')


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
    bp = Blueprint('iphone_sheet_handoff', __name__, url_prefix=PREFIX,
                   template_folder='unified_shadow_templates')

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
        response.headers['Content-Security-Policy'] = "default-src 'none'; style-src 'unsafe-inline'; script-src 'self'; connect-src 'self'; img-src 'self'; form-action 'none'; frame-ancestors 'none'; base-uri 'none'"
        return response

    # The pinned host applies its own CSP after blueprint hooks. Scope the
    # existing UI policy to this opt-in namespace; leave production unchanged.
    def privacy_last(response):
        return privacy(response) if request.path == PREFIX or request.path.startswith(PREFIX+'/') else response
    app.after_request_funcs.setdefault(None, []).insert(0, privacy_last)

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

    def view_context():
        return dict(handoff=True, ui_blueprint='iphone_sheet_handoff',
                    csrf=gate.current()[1].csrf, owner=adapter.owner)

    @bp.get('')
    def listing():
        # Saved journal and existing draft reads only. No RawReader.read(),
        # source observation, authority issuance or CAS from page navigation.
        _, ledger, _ = adapter._state()
        rows = adapter.confirmation.read(gate)
        unresolved = adapter.uncertain or adapter._unresolved(ledger)
        pending_ids = [rid for rid, r in ledger['requests'].items() if r['status'] == 'ATTEMPTING']
        for row in rows:
            records = sorted(((rid, r) for rid, r in ledger['requests'].items()
                              if r['case_id'] == row['case_id']), key=lambda x:x[1]['attempt_revision'])
            row['handoff_blocked'] = unresolved or bool(row.get('result'))
            row['imported'] = False
            if row.get('result'):
                # Evidence from another composition is not Sheet approval.
                row['result'] = dict(request_id=row['result']['request_id'], label=LABELS['UNKNOWN'])
            if records:
                rid, r = records[-1]
                result = adapter.inspect(rid, gate)
                row['result'] = dict(**result, label=LABELS[result['status']])
                row['imported'] = result['status'] == 'DRAFT_IMPORTED'
                row['handoff_blocked'] |= result['status'] != 'DRAFT_IMPORTED'
            if unresolved:
                row['handoff_blocked'] = True
                row['result'] = dict(request_id=pending_ids[0] if pending_ids else 'unavailable',
                                     label=LABELS['UNKNOWN'])
        return render_template('iphone_one_tab.html', rows=rows, result=None, **view_context())

    @bp.get('/ui.js')
    def r1_javascript():
        return Response((Path(__file__).parent/'unified_shadow_templates/iphone_sheet_handoff.js').read_text('utf8'),
                        mimetype='application/javascript')

    @bp.get('/view-results/<rid>')
    def r1_result(rid):
        result = adapter.inspect(rid, gate)
        return render_template('iphone_one_tab.html', rows=[],
            result=dict(**result, label=LABELS[result['status']]), **view_context())

    @bp.get('/original/<cid>')
    def r1_original(cid):
        ui = adapter.confirmation
        if cid in ui.cases:
            return render_template('iphone_one_tab_original.html', cid=cid, sid=ui.cases[cid].source_id,
                                   ui_blueprint='iphone_sheet_handoff')
        if cid in ui.held and ui.held[cid].get('source_id'):
            return Response('保留・合成原本未収録 / '+cid+' / '+ui.held[cid]['source_id'], mimetype='text/plain')
        raise StateError('sheet_handoff_held_or_unmapped')

    @bp.get('/source/<cid>')
    def r1_source(cid):
        if cid not in adapter.confirmation.cases:
            raise StateError('sheet_handoff_held_or_unmapped')
        c = adapter.confirmation.cases[cid]
        raw = c.readers.source_bytes.revision_bytes(c.source_id, c.source['drive_revision_id'])
        if type(raw) is not bytes or sha256(raw).hexdigest() != c.source['sha256']:
            raise StateError('sheet_handoff_source_UNKNOWN')
        return Response(raw, mimetype='image/png')

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
