"""Uninstalled routes for the existing HTTPS/OIDC app, not a new server.

Reuse its screen, encrypted operation tickets, login cookie and request gateway.
No login implementation, credential, listener, source write or Sheets write.
"""
from flask import Blueprint, Response, request, redirect, url_for
from markupsafe import escape

from app.drive_run_state import StateError
from app.human_general_auth_transport import same
from .web import screen, COOKIE, LOGIN_COOKIE


def detail_table(items):
    return ('<table style="width:100%;table-layout:fixed;overflow-wrap:anywhere">'
            '<caption>登録対象の商品明細</caption><thead><tr><th>商品</th><th>数量</th><th>金額</th></tr></thead><tbody>'
            + ''.join('<tr><td>'+str(escape(i['name']))+'<br><small>'
                      +str(escape('｜'.join(i['category']) if i.get('category') else '分類未確定'))
                      +'</small></td><td>'+str(escape(i['quantity']))
                      +'</td><td>'+str(escape(i['amount']))+'円</td></tr>' for i in items)
            + '</tbody></table>')


def registration_routes(*, gateway_for, tickets, logins, journal):
    bp = Blueprint('receipt_registration', __name__)

    @bp.errorhandler(StateError)
    def stopped(error):
        denied = any(s in str(error) for s in ('login_required','access_denied','csrf','browser_binding'))
        return Response(screen('本人確認が必要です' if denied else '結果不明・照合待ち',
            '<p>処理を停止しました。再登録せず、本人確認と登録結果を確認してください。</p>'),
            status=403 if denied else 409)

    @bp.route('/receipt/register/<rid>', methods=['GET','POST'])
    def register(rid):
        ticket = tickets.load(request.cookies.get(COOKIE))
        if ticket.request_id != rid:raise StateError('receipt_registration_browser_binding_invalid')
        gateway = gateway_for(rid, request.cookies.get(LOGIN_COOKIE))
        if gateway.journal is not journal:
            raise StateError('receipt_registration_durable_journal_required')
        if request.method == 'GET':
            view = gateway.details(ticket, cookie=request.cookies.get(COOKIE))
            content = ('<p>案件：'+str(escape(view['case_id']))+'</p>'
                '<p>原本版：'+str(escape(view['snapshot']['source']['version']))+'</p>'
                '<p>支払総額：'+str(view['receipt_total'])+'円／修正後：'
                +str(view['preview']['item_total'])+'円</p>'
                + detail_table(view['preview']['items'])
                + '<a href="'+str(escape(view['original_link']))+'" rel="noreferrer">原本を見る</a>'
                + '<p>この登録では記帳しません。入力が変わった場合は停止します。</p>'
                '<form method="post"><input type="hidden" name="csrf" value="'+str(escape(ticket.csrf))+'">'
                '<input type="hidden" name="snapshot_digest" value="'+str(escape(view['snapshot']['digest']))+'">'
                '<button type="submit">この内容を登録</button></form>')
            return Response(screen('修正内容を確認', content))
        if set(request.form) != {'csrf','snapshot_digest'}:raise StateError('human_general_auth_csrf_invalid')
        _, record = gateway.record(rid, 'authenticated')
        if not same(request.form['snapshot_digest'], record['binding']['snapshot']['digest']):
            raise StateError('receipt_registration_snapshot_stale')
        gateway.confirm(ticket, cookie=request.cookies.get(COOKIE), csrf=request.form['csrf'],
                        origin=request.headers.get('Origin'), method='POST')
        return redirect(url_for('.result',rid=rid), code=303)

    @bp.get('/receipt/result/<rid>')
    def result(rid):
        value = journal.query(rid, logins=logins, cookie=request.cookies.get(LOGIN_COOKIE))
        b = value['binding']
        content = ('<p>案件：'+str(escape(b['case_id']))+'</p>'
            '<details><summary>登録情報</summary><p>要求：'+str(escape(rid))+'</p>'
            '<p>原本版：'+str(escape(b['source']['version']))+'</p>'
            '<p>登録内容ID：'+str(escape(b['snapshot']['digest']))+'</p></details>'
            '<p>登録時の結果です。現在のSheetsの入力・メモは変更しません。</p>')
        if value['validation']:
            content += detail_table(value['validation']['items'])
            if value['validation']['issues']:
                content += '<ul>'+''.join('<li>'+str(escape(i))+'</li>' for i in value['validation']['issues'])+'</ul>'
        return Response(screen(value['state'], content))

    return bp
