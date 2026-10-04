"""New explicit AI intent in the existing two-column review UI, no writer.

Queue/hidden cells are untrusted input. Confirmer identity must come from the
separately authenticated event adapter supplied to HumanGeneralConfirmation,
never from a display cell or a service-account/trigger owner's guessed email.
"""
from .drive_run_state import StateError
from .human_general_authority import binding_fields,eligible
from .page_receipt_model import digest
from .pdf_page_review import check_snapshot,original_uri

CHOICES=['未選択','一般レシート','医療','給与','不明のまま']

def general_review_card(page):
    if not eligible(page):raise StateError('human_general_review_not_eligible')
    identity={**binding_fields(page),'page_numbers':[page.page_number],
              'kind':'human_general','schema':'human-general-review-v1'}
    return {'identity':identity,'token':digest(identity),'rows':[
        ('target','対象','ページ p'+str(page.page_number)),
        ('automatic','自動判定','要確認（自動判定は変更しません）'),
        ('state','状態','ページ種類・解析許可の確認待ち'),
        ('original','原本','原本を開く（p'+str(page.page_number)+'）'),
        ('notice','確認事項','一般レシートを選ぶと、このページだけ外部AIで一般レシートを解析します。医療・給与の送信や会計記帳は許可しません。'),
        ('human_general_kind','種類','未選択'),('result','処理結果','未確認・AI送信なし')]}

def process_general_request(service,sheet,request_id,*,route_kind=None):
    _,snapshot=sheet.request(request_id)
    identity=snapshot.get('identity',{})
    if identity.get('kind')!='human_general':raise StateError('human_general_request_invalid')
    page=service.current_page(identity.get('source_file_id'),identity.get('page_number'))
    expected=general_review_card(page)
    fields=check_snapshot(snapshot,expected)
    live=sheet.read_card(expected['token'])
    if check_snapshot(live,expected)['human_general_kind']!=fields['human_general_kind']:
        raise StateError('human_general_owner_intent_changed')
    choice=fields['human_general_kind']
    if choice=='一般レシート':
        grant=service.confirm(page,operation='confirm_general_receipt_ai',request_id=request_id)
        # Confirm saves + exact read-back before the Sheet becomes a projection.
        result='一般レシート解析許可を保存済み（記帳は未実行）'
    elif choice in {'医療','給与'}:
        if route_kind is None:raise StateError('page_kind_route_required')
        service.hold(page,request_id=request_id)
        route_kind(page,choice,request_id)
        grant=None;result='完全手入力待ち' if choice=='医療' else '給与・一般AI送信なし'
    elif choice=='不明のまま':
        service.hold(page,request_id=request_id)
        grant=None;result='保留・AI送信なし'
    else:raise StateError('human_general_explicit_operation_required')
    sheet.finish_card(request_id,snapshot,result,service.clock())
    return grant
