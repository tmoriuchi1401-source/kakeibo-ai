"""Explicit, isolated composition AFTER the unchanged Flask application factory.

No environment switch, deployed factory import, live client, auth bypass or
accounting submit. The caller supplies the existing host's factory and runtime.
"""
from dataclasses import dataclass
import hmac

from flask import Blueprint, Response, g, render_template, request

from .drive_run_state import StateError
from .unified_receipt_purposes import (ACCOUNTING_POST, PURPOSE_ACTIONS,
    PurposeBoundary, VerifiedRead, VerifiedPost)

PREFIX = '/isolated-shadow/receipts'
BLUEPRINT = 'unified_receipt_isolated_shadow'


@dataclass(frozen=True)
class ShadowConfig:
    enabled: bool = False
    environment: str = 'disabled'


class ExistingHostSessionGate:
    """Read existing encrypted ticket, request and revocable Google login.

    Reuses existing browser binding, owner policy, logins.actor and ticket CSRF.
    Never touches/extends a login, prepares auth requests or issues authority.
    """
    def __init__(self, runtime_factory, cookies):
        self.runtime_factory, self.cookies = runtime_factory, cookies

    def runtime(self):
        if not hasattr(g, 'hga_runtime'):
            g.hga_runtime = self.runtime_factory()
            g.hga_runtime.browser_login_cookie = request.cookies.get(self.cookies['login'])
        rt = g.hga_runtime
        if (rt.settings.get('mode') != 'synthetic_only'
                or getattr(rt.client, 'shadow_memory_only', False) is not True):
            raise StateError('unified_shadow_isolation_required')
        return rt

    def current(self):
        rt = self.runtime()
        cookie = request.cookies.get(self.cookies['login'])
        login = rt.logins.current(cookie)
        if login is None:
            raise StateError('login_session_invalid')
        ticket = rt.tickets.load(request.cookies.get(self.cookies['ticket']))
        gateway = rt.gateway(ticket.request_id)
        gateway.verify_login = lambda record: rt.logins.verify_bound(record, cookie)
        _, record = gateway.record(ticket.request_id, 'authenticated')
        gateway.browser(record, ticket, request.cookies.get(self.cookies['ticket']))
        # Fresh operation actor is made by the EXISTING shared-login service.
        actor = rt.logins.actor(cookie, record)
        return rt, ticket, record, actor

    def read(self, _context):
        _, _, record, actor = self.current()
        return VerifiedRead(actor.actor_id, record['expires_at'], True)

    def post(self, _context, purpose, rid, binding_digest, expected_revision):
        rt, ticket, record, actor = self.current()
        supplied = request.headers.get('X-CSRF-Token', '')
        if (request.method != 'POST' or request.headers.get('Origin') != rt.origin
                or not supplied or not hmac.compare_digest(supplied, ticket.csrf)):
            raise StateError('human_general_auth_csrf_invalid')
        return VerifiedPost(actor.actor_id, purpose, rid, binding_digest,
            expected_revision, record['created_at'], record['expires_at'],
            csrf_valid=True, session_active=True)


def make_isolated_shadow_application(make_application, runtime_factory, boundary, *,
                                     config=ShadowConfig(), cookies=None):
    """Calls actual make_application -> create_app(configured_runtime) unchanged.

    Registration is outside the host, never part of its production composition.
    Explicit Python injection and a memory-only runtime are BOTH mandatory.
    """
    app = make_application()
    if config.enabled is not True:
        return app
    if config.environment != 'isolated_synthetic' or cookies is None:
        raise StateError('unified_shadow_isolation_required')
    rt = runtime_factory()
    if (rt.settings.get('mode') != 'synthetic_only'
            or getattr(rt.client, 'shadow_memory_only', False) is not True):
        raise StateError('unified_shadow_isolation_required')
    if not isinstance(boundary, PurposeBoundary):
        raise StateError('unified_shadow_boundary_required')
    gate = ExistingHostSessionGate(runtime_factory, cookies)
    # Copy the boundary; do not mutate its services, evidence or authority.
    isolated = PurposeBoundary(boundary.owner, gate.post, gate.read,
        boundary.snapshot_reader, boundary.requests, boundary.ports,
        boundary.accounting, enabled=True, clock=rt.clock)
    bp = Blueprint(BLUEPRINT, __name__, url_prefix=PREFIX,
                   template_folder='unified_shadow_templates')

    def fail(message, status):
        return Response(render_template('unified_shadow/page.html', cases=[],
            detail=False, message=message, title='確認を継続できません'), status=status)

    @bp.errorhandler(StateError)
    def rejected(error):
        code = str(error)
        if code == 'HTTP_412':
            return fail('状態が更新されました。再送信せず、現在の状態を確認してください。', 412)
        if 'stale' in code or 'changed' in code or 'conflict' in code:
            return fail('確認対象の状態が変わっています。再追記せず詳細を確認してください。', 409)
        if 'expired' in code:
            return fail('確認期限が切れました。新しい確認から本人認証してください。', 410)
        if code.startswith('login_session_') or 'authenticated' in code:
            return fail('本人確認が必要です。ログアウトまたは期限切れの場合は再認証してください。', 403)
        return fail('要求を受け付けませんでした。入力を保持し、現在の状態を確認してください。', 400)

    @bp.route('', methods=['GET'], endpoint='list')
    def listing():
        return render_template('unified_shadow/page.html', cases=isolated.get_list({}),
            detail=False, message='', title='領収書確認・隔離shadow')

    @bp.route('/cases/<case_id>', methods=['GET'], endpoint='detail')
    def detail(case_id):
        return render_template('unified_shadow/page.html', cases=[isolated.get_detail(case_id, {})],
            detail=True, message='', title='案件詳細・隔離shadow')

    def payload(keys):
        value = request.get_json(silent=True)
        if not isinstance(value, dict) or set(value) != keys:
            raise StateError('unified_request_invalid')
        return value

    def purpose_path(purpose):
        if purpose not in PURPOSE_ACTIONS:
            raise StateError('unified_purpose_route_required')

    @bp.route('/<purpose>/prepare', methods=['POST'], endpoint='prepare')
    def prepare(purpose):
        purpose_path(purpose)
        # Authenticate even malformed requests before observing any case.
        gate.read({})
        v = payload({'case_id', 'requested_action', 'request_id', 'expected_revision'})
        return isolated.prepare(v['case_id'], purpose, v['requested_action'],
            v['request_id'], v['expected_revision'], {})

    @bp.route('/<purpose>/submit', methods=['POST'], endpoint='submit')
    def submit(purpose):
        purpose_path(purpose)
        gate.read({})
        v = request.get_json(silent=True)
        if not isinstance(v, dict):
            raise StateError('unified_request_invalid')
        if purpose == ACCOUNTING_POST:
            # Validate the exact existing financial request and all evidence,
            # but NEVER call bridge.submit, materializer, claim or writer.
            from .unified_receipt_write import OperationRequest
            r = OperationRequest.parse(v)
            prepared = isolated.prepare(r.case_id, purpose, 'post', r.request_id,
                                         r.expected_state_revision, {})
            if prepared.get('operation_targets') != [v]:
                raise StateError('unified_shadow_accounting_evidence_invalid')
            raise StateError('unified_shadow_accounting_transport_forbidden')
        if v.get('purpose') != purpose:
            raise StateError('unified_purpose_route_required')
        return isolated.submit(v, {})

    old_paths = {rule.rule for rule in app.url_map.iter_rules()}
    if (BLUEPRINT in app.blueprints or any(p == PREFIX or p.startswith(PREFIX + '/') for p in old_paths)):
        raise StateError('unified_shadow_route_collision')
    app.register_blueprint(bp)
    return app
