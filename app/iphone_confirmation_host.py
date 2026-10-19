"""Opt-in synthetic extension of the existing authenticated read-only host."""
from copy import deepcopy
import json
from pathlib import Path

from flask import Blueprint, Response, jsonify, render_template, request, url_for

from .drive_run_state import StateError
from .iphone_confirmation import ConfirmationService, SCHEMA, deny
from .iphone_persistent_drafts import unique
from .iphone_readonly_host import make_isolated_readonly_application
from .unified_receipt_shadow import ExistingHostSessionGate, ShadowConfig

PREFIX = '/isolated-synthetic/iphone-confirmation'
BLUEPRINT = 'iphone_confirmation'


def make_isolated_confirmation_application(make_application, runtime_factory, service, *,
                                          config=ShadowConfig(), cookies=None, one_tab=None):
    # OFF constructs ONLY the unchanged application. No implicit environment
    # switches, startup observations, document seeding or production routes.
    if config.enabled is not True:
        return make_application()
    if (config.environment != 'isolated_synthetic' or type(service) is not ConfirmationService
            or cookies is None):
        deny('isolation_required')
    rt = runtime_factory()
    if rt.settings.get('mode') != 'synthetic_only' or getattr(rt.client, 'shadow_memory_only', False) is not True:
        deny('isolation_required')
    app = make_isolated_readonly_application(make_application, runtime_factory, service.saved, config=config, cookies=cookies)
    @app.context_processor
    def isolated_confirmation_links():
        return dict(synthetic_confirmation_enabled=True)
    gate = ExistingHostSessionGate(runtime_factory, cookies)
    bp = Blueprint(BLUEPRINT, __name__, url_prefix=PREFIX, template_folder='unified_shadow_templates')

    def auth():
        value = gate.current(); actor = value[-1]
        if actor.issuer != 'https://accounts.google.com' or actor.actor_id != service.owner:
            deny('owner_required')
        return value

    @bp.before_request
    def security():
        if (request.headers.get('Origin') not in (None, rt.origin)
                or request.headers.get('Sec-Fetch-Site') == 'cross-site'):
            deny('origin_invalid')
        auth()  # even malformed bodies are authenticated before reading drafts

    @bp.after_request
    def privacy(response):
        response.headers['Cache-Control'] = 'no-store'
        response.headers['Referrer-Policy'] = 'no-referrer'
        response.headers['Content-Security-Policy'] = "default-src 'none'; style-src 'unsafe-inline'; script-src 'self'; connect-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'"
        return response

    @bp.errorhandler(StateError)
    def rejected(error):
        code = str(error)
        status = 410 if 'expired' in code else 403 if any(s in code for s in ('login_session_', 'authenticated', 'csrf', 'origin', 'owner_', 'session_')) else 503 if any(s in code for s in ('unknown', 'unavailable', 'quarantin', 'corrupt')) else 409
        message = ('受付結果が不明です。同じ要求の結果を確認してください。新しい要求で再送しないでください。' if status == 503 else
                   '本人確認の期限が切れました。入力を保持して再認証し、保存済みの受付結果を確認してください。' if status == 410 else
                   '本人認証・権限または送信元を確認できません。入力を保持して本人確認からやり直してください。' if status == 403 else
                   '状態が更新されたか、確認対象が一致しません。入力を保持し、現在の保存内容を別画面で確認してください。')
        if 'native_validation_held' in code:
            message='未入力・保留・対象外、または原本照合が必要な項目があります。詳細で入力と各明細の状態を保存し、再確認してください。'
        elif 'held_or_unmapped' in code:
            message='この案件は保留・表示専用です。入力を保持し、保留理由を確認してください。'
        elif 'source_UNKNOWN' in code or 'structure_observation_UNKNOWN' in code:
            message='原本の版対応や構造候補を確認できません。入力を保持し、古い確認を再利用せず保存内容を確認してください。'
        if request.method == 'POST':
            return jsonify(error=code, message=message, input_preserved=True), status
        return Response(render_template('iphone_confirmation.html', mode='error', message=message, cards=[], cid='', draft=None), status=status)

    @bp.errorhandler(Exception)
    def existing_host_error(error):
        # The pinned host has its own drive_run_state module namespace. Map
        # only its data-free codes for presentation; authentication still fails.
        kind=type(error)
        if kind.__name__=='StateError' and kind.__module__.endswith('.drive_run_state'):
            return rejected(StateError(str(error)))
        return rejected(StateError('iphone_confirmation_service_unavailable'))

    @bp.get('')
    def listing():
        if one_tab is not None:
            return render_template('iphone_one_tab.html', rows=one_tab.read(gate), result=None)
        cards, _, _, _ = service.read()
        return render_template('iphone_confirmation.html', mode='list', cards=cards.values(), message='', cid='', draft=None)

    def view(cid, mode):
        read = service.read(); cards, state, ledger, _ = read
        if cid not in cards:
            deny('case_missing')
        held = cards[cid]['display']['held']
        draft = None if held else service.draft(cid, ledger)
        form = None
        if not held:
            token, claims = service.proof(cid, 'acknowledge_snapshot' if mode == 'reconfirm' else 'save_draft', auth(), read=read)
            form = {k:claims[k] for k in ('case_id', 'purpose', 'action', 'request_id', 'expected_revision', 'input_digest')}
            form.update(proof=token)
        return render_template('iphone_confirmation.html', mode=mode, cards=[cards[cid]], cid=cid,
            draft=draft, form=form, csrf=auth()[1].csrf, revision=state['legacy']['revision'],
            observation=None if held else service.saved_mapping(cid)['source_observation'], message='')

    @bp.get('/cases/<cid>')
    def detail(cid):
        return view(cid, 'detail')

    @bp.get('/cases/<cid>/reconfirm')
    def reconfirm(cid):
        return view(cid, 'reconfirm')

    @bp.get('/results/<rid>')
    def result(rid):
        cards, _, ledger, _ = service.read()
        entry = ledger['requests'].get(rid)
        return render_template('iphone_confirmation.html', mode='result', cards=[], cid='', draft=None,
            result=entry['result'] if entry else None, message='' if entry else 'この要求の受付は確認できません。新しい要求を作らず入力を保持してください。')

    @bp.post('/cases/<cid>/request')
    def post(cid):
        if request.mimetype != 'application/json' or request.content_length is None or request.content_length > 300_000:
            deny('request_invalid')
        try:
            payload = json.loads(request.get_data(), object_pairs_hook=unique)
        except (ValueError, TypeError):
            deny('request_invalid')
        if not isinstance(payload, dict) or payload.get('case_id') != cid:
            deny('case_or_purpose_changed')
        result, replay = service.post(payload, auth(), gate)
        destination = url_for(BLUEPRINT+'.detail', cid=cid) if result['status'] == 'draft_saved' else url_for(BLUEPRINT+'.result', rid=result['request_id'])
        return jsonify(result=result, replay=replay, redirect=destination)

    @bp.get('/ui.js')
    def javascript():
        return Response((Path(__file__).parent/'unified_shadow_templates/iphone_confirmation.js').read_text(encoding='utf8'), mimetype='application/javascript')

    if one_tab is not None:
        from staging_shadow.iphone_confirmation_ui import OneTabConfirmation
        if type(one_tab) is not OneTabConfirmation or one_tab.owner != service.owner:
            deny('isolation_required')

        # The pinned host applies its no-script production CSP AFTER blueprint
        # hooks. Apply the isolated policy last, only on this synthetic surface.
        # Production paths, factory sources and auth cookies remain unchanged.
        def r1_privacy_last(response):
            if request.path == PREFIX or request.path.startswith(PREFIX+'/r1/'):
                return privacy(response)
            return response
        app.after_request_funcs.setdefault(None, []).insert(0, r1_privacy_last)

        @bp.get('/r1/<cid>/<action>')
        def r1_proof(cid, action):
            proof, claims = one_tab.proof(cid, action, gate)
            return jsonify(proof=proof, claims=claims, csrf=auth()[1].csrf)

        @bp.post('/r1/request')
        def r1_post():
            if request.mimetype != 'application/json' or request.content_length is None or request.content_length > 800000:
                deny('request_invalid')
            value = json.loads(request.get_data(), object_pairs_hook=unique)
            return jsonify(one_tab.post(value, gate))

        @bp.get('/r1/results/<rid>')
        def r1_result(rid):
            return render_template('iphone_one_tab.html', rows=[], result=one_tab.inspect(rid, gate))

        @bp.get('/r1/original/<cid>')
        def r1_original(cid):
            if cid not in one_tab.cases:
                deny('held_or_unmapped')
            c = one_tab.cases[cid]
            # Offline fixture representation only. No Drive fetch or observation.
            return Response('合成原本 / '+cid+' / '+c.source_id, mimetype='text/plain')

        @bp.get('/r1/ui.js')
        def r1_javascript():
            return Response((Path(__file__).parent/'unified_shadow_templates/iphone_one_tab.js').read_text(encoding='utf8'), mimetype='application/javascript')

    if BLUEPRINT in app.blueprints or any(r.rule.startswith(PREFIX) for r in app.url_map.iter_rules()):
        deny('route_collision')
    app.register_blueprint(bp)
    return app
