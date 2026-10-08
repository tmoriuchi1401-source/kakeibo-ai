"""Scoped projection updates in the existing tab; no authority or posting.

Find the card by its structured source/page binding, never by fixed row numbers.
Only that contiguous card is touched. Its old owner fields must be unchanged.
"""
import json
from .drive_run_state import StateError
from .page_receipt_model import digest
from .pdf_page_review import SCHEMA, SHEET_ID, payment_date_ui_requests, original_uri
from .pdf_page_general import input_rows, category_choices


def locate(rows, source_id, number):
    found=[]
    for i,row in enumerate(rows):
        if len(row)<6 or row[2]!=SCHEMA:continue
        try:identity=json.loads(row[4])
        except (ValueError,TypeError):raise StateError('pdf_card_identity_invalid') from None
        if identity.get('source_file_id')==source_id and identity.get('page_numbers')==[number]:found.append(i)
    if not found or found!=list(range(found[0],found[-1]+1)):
        raise StateError('pdf_single_card_missing_or_fragmented')
    if len({rows[i][3] for i in found})!=1 or len({rows[i][4] for i in found})!=1:
        raise StateError('pdf_single_card_identity_conflict')
    return found


def pending(page):
    identity={'schema':SCHEMA,'kind':'general_receipt_preanalysis',
        'source_file_id':page.source.source_file_id,'source_content_hash':page.source.source_content_hash,
        'page_numbers':[page.page_number],'stable_page_identity':page.stable_page_identity,
        'review_identity':page.review_identity,'authority_revision':page.authority_revision,
        'accounting_allowed':False}
    rows=[('target','対象','p'+str(page.page_number)),('kind','種別','一般レシート確認'),
        ('state','状態','本人認証・Gemini送信許可待ち'),('original','原画像を見る','原本を開く（p'+str(page.page_number)+'）'),
        ('notice','入力案内','認証前はAI解析しません。読めない項目だけ後から入力できます。'),
        *input_rows()]
    rows=[('hga_auth_link','Gemini送信を許可する','iPhoneでの画面確認後に認証リンクを表示します') if f=='manual_action' else
          (f,l,'会計記帳は行いません' if f=='result' else v) for f,l,v in rows]
    return {'identity':identity,'token':digest([identity,rows]),'rows':rows}


def patch_requests(before_rows, expected_rows, card, categories, *, link=None, preserve_owner_inputs=False):
    """Build a bounded batchUpdate from a fresh optimistic preflight read.

    A Sheets projection cannot confer authority. The backend must independently
    revalidate the protected card/snapshot before any later confirmation.
    """
    if before_rows!=expected_rows:raise StateError('pdf_single_card_projection_changed')
    ident=card['identity'];indices=locate(before_rows,ident['source_file_id'],ident['page_numbers'][0])
    if len(indices)!=len(card['rows']):raise StateError('pdf_single_card_height_change_requires_review')
    token=card['token'];cells=[];serialized=[];requests=[]
    for index,(field,label,value) in zip(indices,card['rows']):
        retained=preserve_owner_inputs and field in {'date','amount','category','merchant','payment','memo'}
        if retained:
            existing=[before_rows[i] for i in indices if before_rows[i][5]==field]
            if len(existing)!=1:raise StateError('pdf_single_card_owner_input_conflict')
            value=existing[0][1] if len(existing[0])>1 else ''
        if field=='date' and not retained:
            from .pdf_page_review import sheet_date_value
            value=sheet_date_value(value)
        encoded=[label,value,SCHEMA,token,json.dumps(ident,separators=(',',':')),field]+['']*8
        serialized.append(encoded)
        cells.append({'values':[{'userEnteredValue':{'numberValue':v} if type(v) in (int,float) else {'stringValue':str(v)}} for v in encoded]})
        region={'sheetId':SHEET_ID,'startRowIndex':index,'endRowIndex':index+1,'startColumnIndex':1,'endColumnIndex':2}
        editable=field in {'date','amount','category','merchant','payment','memo'}
        format={'backgroundColor':{'red':1,'green':.98,'blue':.88} if editable else {'red':1,'green':1,'blue':1},
                'textFormat':{}}
        uri=original_uri(ident['source_file_id'],ident['page_numbers'][0]) if field=='original' else link if field=='hga_auth_link' else None
        if uri:format['textFormat']['link']={'uri':uri}
        if field=='amount':format['numberFormat']={'type':'NUMBER','pattern':'#,##0'}
        requests.append({'repeatCell':{'range':region,'cell':{'userEnteredFormat':format},
            'fields':'userEnteredFormat.backgroundColor,userEnteredFormat.textFormat.link'+(',userEnteredFormat.numberFormat' if field=='amount' else '')}})
        if field=='category':
            requests.append({'setDataValidation':{'range':region,'rule':{'condition':{'type':'ONE_OF_LIST','values':[
                {'userEnteredValue':v} for v in category_choices(categories)]},'strict':True,'showCustomUi':True}}})
        if field=='amount':
            cell='B'+str(index+1)
            requests.append({'setDataValidation':{'range':region,'rule':{'condition':{'type':'CUSTOM_FORMULA','values':[
                {'userEnteredValue':f'=OR(ISBLANK({cell}),AND(ISNUMBER({cell}),{cell}>0,{cell}=INT({cell})))'}]},'strict':True}}})
    region={'sheetId':SHEET_ID,'startRowIndex':indices[0],'endRowIndex':indices[-1]+1,'startColumnIndex':0,'endColumnIndex':14}
    requests=[{'updateCells':{'range':region,'rows':cells,'fields':'userEnteredValue'}},
              {'setDataValidation':{'range':{**region,'startColumnIndex':1,'endColumnIndex':2}}},*requests]
    requests.extend(payment_date_ui_requests(serialized,start_row_index=indices[0]))
    return requests


def completion_card_requests(before_rows,expected_rows,cards,categories):
    """One page's block only, vertically expanded for independently bound units.

    Preserve other rows. Never delete rows, shrink a frozen receipt count, or
    enable posting. Hidden cells are projection, never trusted authority.
    """
    if before_rows!=expected_rows or not 1<=len(cards)<=20:
        raise StateError('pdf_single_card_projection_changed')
    sid=cards[0]['identity']['source_file_id'];number=cards[0]['identity']['page_numbers'][0]
    found=[];old_identities=[]
    for index,row in enumerate(before_rows):
        if len(row)<6 or row[2]!=SCHEMA:continue
        try:identity=json.loads(row[4])
        except (ValueError,TypeError):raise StateError('pdf_card_identity_invalid') from None
        if identity.get('source_file_id')==sid and identity.get('page_numbers')==[number]:
            found.append(index);old_identities.append(identity)
    if not found or found!=list(range(found[0],found[-1]+1)) or len(found)%13:
        raise StateError('pdf_single_card_missing_or_fragmented')
    count=len(found)//13
    initializing=count==1 and all(i.get('kind')=='general_receipt_preanalysis' for i in old_identities)
    if not initializing and count!=len(cards):raise StateError('pdf_card_receipt_count_changed')
    new_ids=[c['identity'].get('receipt_unit_id') for c in cards]
    if any(not v for v in new_ids) or len(set(new_ids))!=len(cards):raise StateError('pdf_card_receipt_identity_conflict')
    template=before_rows[found[0]:found[0]+13];extra=13*len(cards)-len(found)
    if extra<0:raise StateError('pdf_card_receipt_count_changed')
    requests=[]
    if extra:
        requests.append({'insertDimension':{'range':{'sheetId':SHEET_ID,'dimension':'ROWS',
            'startIndex':found[-1]+1,'endIndex':found[-1]+1+extra},'inheritFromBefore':True}})
    for i,original in enumerate(cards):
        card={**original,'rows':list(original['rows'])}
        if (len(card['rows'])!=13 or card['identity']['source_file_id']!=sid
                or card['identity']['page_numbers']!=[number]
                or card['identity'].get('kind')!='general_receipt_completion'):
            raise StateError('pdf_card_receipt_identity_conflict')
        if not initializing:
            prior=old_identities[13*i]
            if (prior.get('receipt_unit_id')!=new_ids[i] or prior.get('candidate_digest')!=card['identity']['candidate_digest']):
                raise StateError('pdf_card_candidate_changed')
        card['rows']=[(f,l,'会計writeは未承認です' if f=='manual_action' else
            'read-only検証中・記帳しません' if f=='result' else v) for f,l,v in card['rows']]
        start=found[0]+13*i;virtual=[[] for _ in range(start)]+template
        requests.extend(patch_requests(virtual,virtual,card,categories))
    return requests
