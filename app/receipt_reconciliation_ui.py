"""Two-column cards/visibility requests in existing PDFページ確認 only.

Offline projection builder: no Sheet writes, new tab, trigger or authentication
side effects. Auth links are minted by the reviewed host, not by cell changes.
"""
from .receipt_audit import validate_identity, entity_key, digest, needs_attention
from .receipt_reconciliation_authority import DECISIONS
from .pdf_page_review import SCHEMA, SHEET_ID
from .drive_run_state import StateError

CHOICES = ['未選択',*DECISIONS]


def reconciliation_card(identity, candidate_ledger_id, ledger_snapshot_digest, *, preview=None):
    from .receipt_audit import identifier, checked_hash
    validate_identity(identity); identifier(candidate_ledger_id); checked_hash(ledger_snapshot_digest)
    ident = {**identity, 'stable_page_identity': identity['page_identity'],
             'schema': SCHEMA, 'kind': 'receipt_reconciliation', 'page_numbers': [identity['page_number']],
             'candidate_ledger_id': candidate_ledger_id, 'ledger_snapshot_digest': ledger_snapshot_digest}
    # Metadata remains separate from optional visible comparison information.
    # The latter is a fresh ledger projection, never a durable history snapshot.
    rows = [('target','対象','ページ p'+str(identity['page_number'])),
            ('state','状態','既存記帳との対応確認待ち'),
            ('original','原本','今回の原本を見る'),
            ('notice','確認事項','原本と既存記帳を比較。同一なら再記帳せず紐付け、別ならこの組合せだけ重複候補を解消。'),
            ('reconciliation_decision','判定','未選択'),
            ('reconciliation_auth_link','本人確認','選択後、本人認証して明示確定（セル変更だけでは確定しません）'),
            ('result','処理結果','未確定・会計writeなし')]
    if preview is not None:
        if set(preview)!={'date','facility','amount','category','source_file_id'}:
            raise StateError('reconciliation_preview_invalid')
        identifier(preview['source_file_id'])
        ident['comparison_source_file_id']=preview['source_file_id']
        rows[3:3]=[('comparison_date','既存記帳の日付',preview['date']),
                   ('comparison_facility','既存記帳の施設',preview['facility']),
                   ('comparison_amount','既存記帳の金額',preview['amount']),
                   ('comparison_category','既存記帳のカテゴリ',preview['category']),
                   ('comparison_original','既存原本','既存記帳側の原本を見る')]
    readonly = [r for r in rows if r[0] not in {'reconciliation_decision','state','result'}]
    return {'identity': ident, 'token': digest([ident,readonly]), 'rows': rows}


def append_reconciliation_requests(before,expected,card,*,sheet_id=SHEET_ID):
    """Insert a comparison beneath the existing Medical card, no input rewrite.

    Structured identity determines the insertion point. No fixed cell list,
    new tab, clearing or replacement of the Medical owner's input fields.
    """
    import json
    from .pdf_page_review import original_uri
    if before!=expected:raise StateError('reconciliation_projection_changed')
    ident=card['identity'];indices=[]
    for index,row in enumerate(before):
        if len(row)<6 or row[2]!=SCHEMA:continue
        try:existing=json.loads(row[4])
        except (ValueError,TypeError):raise StateError('reconciliation_projection_invalid') from None
        if existing.get('source_file_id')==ident['source_file_id'] and existing.get('page_numbers')==ident['page_numbers']:
            if existing.get('kind')=='receipt_reconciliation':raise StateError('reconciliation_projection_already_exists')
            indices.append(index)
    if not indices or indices!=list(range(indices[0],indices[-1]+1)):
        raise StateError('reconciliation_existing_card_required')
    start=indices[-1]+1;count=len(card['rows']);cells=[];requests=[]
    for offset,(field,label,value) in enumerate(card['rows']):
        encoded=[label,value,SCHEMA,card['token'],json.dumps(ident,separators=(',',':')),field]+['']*8
        cells.append({'values':[{'userEnteredValue':{'numberValue':v} if type(v) in (int,float)
            else {'stringValue':str(v)}} for v in encoded]})
        region=dict(sheetId=sheet_id,startRowIndex=start+offset,endRowIndex=start+offset+1,startColumnIndex=1,endColumnIndex=2)
        if field in {'original','comparison_original'}:
            sid=ident['source_file_id'] if field=='original' else ident.get('comparison_source_file_id')
            if sid:
                requests.append({'repeatCell':{'range':region,'cell':{'userEnteredFormat':{'textFormat':{'link':{
                    'uri':original_uri(sid,ident['page_number'] if field=='original' else 1)}}}},
                    'fields':'userEnteredFormat.textFormat.link'}})
        if field=='reconciliation_decision':
            requests.append({'setDataValidation':{'range':region,'rule':{'condition':{'type':'ONE_OF_LIST',
                'values':[{'userEnteredValue':v} for v in CHOICES]},'strict':True,'showCustomUi':True}}})
            requests.append({'repeatCell':{'range':region,'cell':{'userEnteredFormat':{'backgroundColor':{
                'red':1,'green':.98,'blue':.88}}},'fields':'userEnteredFormat.backgroundColor'}})
    region=dict(sheetId=sheet_id,startRowIndex=start,endRowIndex=start+count,startColumnIndex=0,endColumnIndex=14)
    return [{'insertDimension':{'range':{'sheetId':sheet_id,'dimension':'ROWS',
        'startIndex':start,'endIndex':start+count},'inheritFromBefore':True}},
        {'updateCells':{'range':region,'rows':cells,'fields':'userEnteredValue'}},
        {'setDataValidation':{'range':{**region,'startColumnIndex':1,'endColumnIndex':2}}},*requests]


def visible_cards(cards, current_states):
    """Current metadata only; no history scan and no cross-page global gate."""
    result = []
    for card in cards:
        ident = card['identity']
        if 'entity_key' in ident:
            key = ident['entity_key']
        elif ident.get('kind') == 'receipt_reconciliation':
            from .receipt_audit import IDENTITY_FIELDS
            key = entity_key({k:ident[k] for k in IDENTITY_FIELDS})
        else:
            result.append(card); continue
        state = current_states.get(key)
        if state is None or needs_attention(state): result.append(card)
    return result


def visibility_requests(existing_rows, card_states, *, sheet_id=SHEET_ID, start_row_index=0):
    """Hide terminal card rows without clearing cells or losing owner inputs.

    card_states is a trusted backend token -> current-state projection. Hidden
    cells are used only to find presentation rows, never to authorize decisions.
    """
    requests = []
    for offset, row in enumerate(existing_rows):
        if len(row) < 4 or row[2] != SCHEMA or row[3] not in card_states: continue
        state = card_states[row[3]]
        requests.append({'updateDimensionProperties': {'range': {'sheetId':sheet_id,
            'dimension':'ROWS','startIndex':start_row_index+offset,'endIndex':start_row_index+offset+1},
            'properties':{'hiddenByUser':not needs_attention(state)},'fields':'hiddenByUser'}})
    return requests
