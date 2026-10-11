"""Embeddable unified request window. No HTTP server, live client or schedule.

An authenticated host supplies its current Case repository, existing owner
confirmation URL, request bridge, and POST/CSRF verifier. Browser input can only
request validation or submit a server-prepared request; it cannot supply grants,
route declarations, Cases, actor IDs or inspection evidence.
"""
from copy import deepcopy
from html import escape
import json

from .drive_run_state import StateError
from .unified_receipt_eligibility import current_snapshot
from .unified_receipt_review import digest
from .unified_receipt_write import LABELS, prohibited


class UnifiedReceiptController:
    def __init__(self, bridge, case_repository, verify_post, *, owner_link=None):
        self.bridge,self.cases,self.verify_post=bridge,case_repository,verify_post
        self.owner_link=owner_link or (lambda case:'')

    def case(self, case_id):
        cases=self.cases()
        if len({c.case_id for c in cases})!=len(cases):raise StateError('unified_duplicate_case_reference')
        selected=[c for c in cases if c.case_id==case_id]
        if len(selected)!=1:raise StateError('unified_case_not_unique')
        c=deepcopy(selected[0])
        # Explicit deny fences from the entire repository also apply here.
        if any(x.disposition=='LEGACY_UNMAPPED_HOLD' and x.source_id==c.source_id
               and x.identity.get('source_content_hash')==c.identity.get('source_content_hash')
               and set(x.pages).intersection(c.pages) for x in cases):
            raise StateError('unified_legacy_page_reuse_forbidden')
        return c

    def _post(self, context):
        if self.verify_post(context) is not True:raise StateError('unified_authenticated_post_required')

    def prepare(self, case_id, action, request_id):
        return self.bridge.prepare(self.case(case_id),action,request_id)

    def submit(self, payload, context):
        self._post(context)
        return self.bridge.submit(self.case(payload.get('case_id')),payload)

    def recover(self, payload, context):
        self._post(context)
        return self.bridge.recover_readback(self.case(payload.get('case_id')),payload)

    def validate_inputs(self, case_id, changes, expected_digest, context, *, action='post'):
        """Native dry validation. Preserve unsent input; do not issue authority.

        Accepted native owner snapshots must subsequently be read by the host.
        A dry-validation result never becomes an enabled request.
        """
        self._post(context);c=self.case(case_id)
        if not self.bridge.config.enabled or prohibited(c):raise StateError('unified_case_write_forbidden')
        if digest(current_snapshot(c))!=expected_digest:raise StateError('unified_draft_snapshot_changed')
        if not isinstance(changes,dict) or not changes:raise StateError('unified_draft_invalid')
        markers=[f[0] for f in c.fields]
        if len(set(markers))!=len(markers) or not set(changes)<=set(markers):
            raise StateError('unified_draft_field_not_unique')
        draft=deepcopy(c)
        for row in draft.fields:
            if row[0] in changes:row[2]=changes[row[0]]
        port=self.bridge.adapters.get((c.kind,action))
        if port is None or not hasattr(port,'validate_draft'):raise StateError('unified_native_draft_unavailable')
        checked=port.validate_draft(c,draft)
        return dict(state='validated_awaiting_owner',label=LABELS['validated_awaiting_owner'],
                    validation=checked,snapshot_digest=digest(current_snapshot(draft)),operation_targets=[],
                    draft=deepcopy(draft.fields),owner_confirmation_url=self.owner_link(c))


def render_request_detail(case, prepared, *, submit_url='', csrf_token='', owner_url=''):
    """Field markers/stable IDs only; OFF and held views contain no submit form."""
    esc=lambda x:escape(str(x),quote=True)
    targets=prepared.get('operation_targets',[])
    if len(targets)>1:raise StateError('unified_multiple_operation_targets')
    state=prepared.get('state','held');label=LABELS.get(state,LABELS['held'])
    heading=f'{case.case_id}・p{",".join(map(str,case.pages))}・レシート{case.identity.get("receipt_index", "未対応")}・{label}'
    rows=''.join(f'<dt>{esc(label)}</dt><dd data-field="{esc(marker)}">{esc(value)}</dd>'
                 for marker,label,value in case.fields)
    body=f'<article id="{esc(case.case_id)}"><h2>{esc(heading)}</h2><p>{esc(label)}</p><dl>{rows}</dl>'
    if targets and submit_url and csrf_token and not prohibited(case):
        r=targets[0]
        if r.get('case_id')!=case.case_id or r.get('current_snapshot_digest')!=digest(current_snapshot(case)):
            raise StateError('unified_ui_request_snapshot_changed')
        body+=f'<form method="post" action="{esc(submit_url)}"><input type="hidden" name="csrf" value="{esc(csrf_token)}">'
        body+=f'<input type="hidden" name="request" value="{esc(json.dumps(r,ensure_ascii=True))}">'
        body+='<button type="submit">この案件の要求を送信</button></form>'
    elif owner_url and state=='validated_awaiting_owner':
        body+=f'<a href="{esc(owner_url)}">この案件の本人確認を開く</a>'
    body+='<p>要求受付後も、会計3表のread-backと原本全体の終端確認が必要です。</p>'
    body+=f'<footer>{esc(heading)}・案件終了</footer></article>'
    return ('<!doctype html><html lang="ja"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            '<style>body{font:16px system-ui;margin:16px}article{border:2px solid #456778;max-width:960px;margin:auto}'
            'h2,footer{background:#dceaf4;padding:12px;overflow-wrap:anywhere}dl,p,form{padding:12px}'
            'dd{margin:0 0 12px;overflow-wrap:anywhere}button{padding:12px;width:100%}a{overflow-wrap:anywhere}</style>'+body+'</html>')
