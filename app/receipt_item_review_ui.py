"""Three visible columns without exposing legacy hidden C:P identities.

Physical A/B/Q are adjacent on screen while C:P stay hidden. New per-row
metadata occupies hidden R:W. No legacy card is rewritten or moved between
columns. Locations come from receipt/item markers, never fixed row numbers.
"""
import json
from .drive_run_state import StateError
from .pdf_review_fields import SHEET_ID, SCHEMA as LEGACY, PAYMENT_DATE_HINT, sheet_date_value
from .pdf_page_general import category_choices
from .receipt_item_review import SCHEMA, ACTIONS, FIELDS, check_snapshot

WIDTHS={0:107,1:74,16:133}
YELLOW={'red':1,'green':.97,'blue':.80}
ORANGE={'red':1,'green':.88,'blue':.72}


def locate(rows,source,page):
    found=[]
    for i,row in enumerate(rows):
        if len(row)>20 and row[17]==SCHEMA:
            encoded=row[19]
        elif len(row)>5 and row[2]==LEGACY:
            encoded=row[4]
        else:continue
        try:ident=json.loads(encoded)
        except Exception:raise StateError('item_review_ui_identity_invalid') from None
        if ident.get('source_file_id')==source and ident.get('page_numbers')==[page]:found.append(i)
    if not found or found!=list(range(found[0],found[-1]+1)):
        raise StateError('item_review_ui_missing_or_fragmented')
    return found


def encoded_rows(card):
    result=[];items={i['item_id']:i for i in card['items']}
    for field,label,value in card['rows']:
        row=['']*23;row[0]=label;row[1]=sheet_date_value(value) if field=='date' else value
        row[17:21]=[SCHEMA,card['token'],json.dumps(card['identity'],separators=(',',':')),field]
        if field.startswith('item:'):
            item=items[field[5:]];row[16]=item['category'];row[21]=item['item_id']
            row[22]='review' if (item['item_id'] in card['review_items'] if 'review_items' in card else card['hard_blocked']) else 'normal'
        elif field=='item_header':row[16]='カテゴリ'
        elif field in {'state','reason'}:row[22]='review' if card['hard_blocked'] else 'normal'
        result.append(row)
    return result


def cell_range(row,col,end_col=None,*,sheet_id=SHEET_ID):
    return {'sheetId':sheet_id,'startRowIndex':row,'endRowIndex':row+1,
            'startColumnIndex':col,'endColumnIndex':col+1 if end_col is None else end_col}


def rules_for_rows(rows,start,*,sheet_id=SHEET_ID):
    """CF references are derived from semantic per-row/item identity markers."""
    rules=[]
    def rule(region,formula,color):
        rules.append({'ranges':[region],'booleanRule':{'condition':{'type':'CUSTOM_FORMULA',
            'values':[{'userEnteredValue':formula}]},'format':{'backgroundColor':color}}})
    for offset,row in enumerate(rows):
        index=start+offset;n=index+1;field=row[20]
        if field in {'date','amount'}:
            rule(cell_range(index,1,sheet_id=sheet_id),f'=LEN(TRIM(TO_TEXT($B{n})))=0',YELLOW)
        if field.startswith('item:'):
            rule(cell_range(index,16,sheet_id=sheet_id),f'=LEN(TRIM(TO_TEXT($Q{n})))=0',YELLOW)
            rule(cell_range(index,0,2,sheet_id=sheet_id),f'=$W{n}="review"',ORANGE)
        elif field in {'state','reason'}:
            rule(cell_range(index,1,17,sheet_id=sheet_id),f'=$W{n}="review"',ORANGE)
    return rules


def requests(before,expected,cards,categories,*,column_count,row_count,sheet_id=SHEET_ID):
    if before!=expected or not 1<=len(cards)<=20:raise StateError('item_review_ui_changed')
    source=cards[0]['identity']['source_file_id'];page=cards[0]['identity']['page_number']
    indices=locate(before,source,page);start=indices[0]
    if any(c['identity']['source_file_id']!=source or c['identity']['page_number']!=page for c in cards):
        raise StateError('item_review_ui_page_mismatch')
    rows=[r for c in cards for r in encoded_rows(c)]
    extra=len(rows)-len(indices)
    if extra<0:raise StateError('item_review_ui_shrink_forbidden')
    batch=[]
    if column_count<23:
        batch.append({'appendDimension':{'sheetId':sheet_id,'dimension':'COLUMNS','length':23-column_count}})
    if extra:
        batch.append({'insertDimension':{'range':{'sheetId':sheet_id,'dimension':'ROWS',
            'startIndex':indices[-1]+1,'endIndex':indices[-1]+1+extra},'inheritFromBefore':True}})
    region={'sheetId':sheet_id,'startRowIndex':start,'endRowIndex':start+len(rows),'startColumnIndex':0,'endColumnIndex':23}
    batch.append({'unmergeCells':{'range':region}})
    cells=[{'values':[{'userEnteredValue':{'numberValue':v} if type(v) in (int,float)
            else {'stringValue':str(v)}} for v in row]} for row in rows]
    batch.extend([{'updateCells':{'range':region,'rows':cells,'fields':'userEnteredValue'}},
        {'setDataValidation':{'range':region}},
        {'repeatCell':{'range':region,'cell':{'userEnteredFormat':{'wrapStrategy':'WRAP',
            'verticalAlignment':'MIDDLE','backgroundColor':{'red':1,'green':1,'blue':1},
            'textFormat':{'fontSize':10,'foregroundColor':{'red':.12,'green':.12,'blue':.12}}}},
            'fields':'userEnteredFormat.wrapStrategy,userEnteredFormat.verticalAlignment,userEnteredFormat.backgroundColor,userEnteredFormat.textFormat'}}])
    for col,width in WIDTHS.items():
        batch.append({'updateDimensionProperties':{'range':{'sheetId':sheet_id,'dimension':'COLUMNS','startIndex':col,'endIndex':col+1},
            'properties':{'pixelSize':width,'hiddenByUser':False},'fields':'pixelSize,hiddenByUser'}})
    for first,last in [(2,16),(17,23)]:
        batch.append({'updateDimensionProperties':{'range':{'sheetId':sheet_id,'dimension':'COLUMNS','startIndex':first,'endIndex':last},
            'properties':{'hiddenByUser':True},'fields':'hiddenByUser'}})
    choices=category_choices(categories)
    for offset,row in enumerate(rows):
        index=start+offset;n=index+1;field=row[20]
        is_item=field.startswith('item:') or field=='item_header'
        if not is_item:
            batch.append({'mergeCells':{'range':cell_range(index,1,17,sheet_id=sheet_id),'mergeType':'MERGE_ALL'}})
        height=80 if field.startswith('compare:') else 90 if field=='notice' and any(c.get('editable_items') for c in cards) else 48 if is_item else 42 if field in {'reason','notice','memo','result'} else 34
        batch.append({'updateDimensionProperties':{'range':{'sheetId':sheet_id,'dimension':'ROWS','startIndex':index,'endIndex':index+1},
            'properties':{'pixelSize':height},'fields':'pixelSize'}})
        if field=='original':
            from .pdf_review_fields import original_uri
            uri=original_uri(source,page)
            if uri:batch.append({'repeatCell':{'range':cell_range(index,1,sheet_id=sheet_id),
                'cell':{'userEnteredFormat':{'textFormat':{'link':{'uri':uri}}}},'fields':'userEnteredFormat.textFormat.link'}})
        if field=='date':
            batch.extend([{'setDataValidation':{'range':cell_range(index,1,sheet_id=sheet_id),'rule':{
                'condition':{'type':'DATE_IS_VALID'},'strict':True,'inputMessage':PAYMENT_DATE_HINT}}},
                {'repeatCell':{'range':cell_range(index,1,sheet_id=sheet_id),'cell':{'userEnteredFormat':{
                    'numberFormat':{'type':'DATE','pattern':'yyyy/mm/dd'}},'note':PAYMENT_DATE_HINT},'fields':'userEnteredFormat.numberFormat,note'}}])
        if field=='amount':
            batch.extend([{'setDataValidation':{'range':cell_range(index,1,sheet_id=sheet_id),'rule':{
                'condition':{'type':'CUSTOM_FORMULA','values':[{'userEnteredValue':f'=OR(ISBLANK(B{n}),AND(ISNUMBER(B{n}),B{n}>0,B{n}=INT(B{n})))'}]},'strict':True}}},
                {'repeatCell':{'range':cell_range(index,1,sheet_id=sheet_id),'cell':{'userEnteredFormat':{
                    'numberFormat':{'type':'NUMBER','pattern':'#,##0"円"'}}},'fields':'userEnteredFormat.numberFormat'}}])
        if field.startswith('item:'):
            batch.extend([{'setDataValidation':{'range':cell_range(index,16,sheet_id=sheet_id),'rule':{
                'condition':{'type':'ONE_OF_LIST','values':[{'userEnteredValue':v} for v in choices]},'strict':True,'showCustomUi':True}}},
                {'repeatCell':{'range':cell_range(index,1,sheet_id=sheet_id),'cell':{'userEnteredFormat':{
                    'numberFormat':{'type':'NUMBER','pattern':'#,##0"円"'}}},'fields':'userEnteredFormat.numberFormat'}}])
        if field=='action':
            batch.append({'setDataValidation':{'range':cell_range(index,1,sheet_id=sheet_id),'rule':{
                'condition':{'type':'ONE_OF_LIST','values':[{'userEnteredValue':v} for v in ACTIONS]},'strict':True,'showCustomUi':True}}})
        if field=='structure_confirmation':
            from .receipt_item_confirmation import CHOICES
            batch.append({'setDataValidation':{'range':cell_range(index,1,sheet_id=sheet_id),'rule':{
                'condition':{'type':'ONE_OF_LIST','values':[{'userEnteredValue':v} for v in CHOICES]},'strict':True,'showCustomUi':True}}})
    # Caller removes ONLY this renderer's previously verified rules before a
    # repeat projection. Never purge other pages' CF or data validation.
    for rule in reversed(rules_for_rows(rows,start,sheet_id=sheet_id)):
        batch.append({'addConditionalFormatRule':{'rule':rule,'index':0}})
    return batch,rows,start,extra


def read_snapshot(rows,expected,original_link):
    found=[r for r in rows if len(r)>20 and r[17]==SCHEMA and r[18]==expected['token']]
    if len(found)!=len(expected['rows']):raise StateError('item_review_snapshot_stale')
    if any(json.loads(r[19])!=expected['identity'] for r in found):raise StateError('item_review_ui_identity_changed')
    current={'identity':expected['identity'],'token':expected['token'],
             'rows':[[r[20],r[0],r[1]] for r in found],'items':[],'original_link':original_link}
    baseline={i['item_id']:i for i in expected['items']}
    for row in found:
        if row[20].startswith('item:'):
            key=row[20][5:]
            if key not in baseline or row[21]!=key:raise StateError('item_review_ui_item_changed')
            current['items'].append({**baseline[key],'name':row[0],'amount':row[1],
                                    'category':row[16] if len(row)>16 else ''})
    check_snapshot(current,expected)
    return current


def terminal_hide_requests(rows,expected,request,accounting_readback,*,sheet_id=SHEET_ID):
    """Non-destructive projection: a status cell can never hide a card."""
    if (request['status']!='complete' or request['snapshot']['identity']!=expected['identity']
            or accounting_readback(request)!='complete'):
        raise StateError('item_review_terminal_readback_required')
    indices=[i for i,row in enumerate(rows) if len(row)>20 and row[17]==SCHEMA and row[18]==expected['token']]
    if len(indices)!=len(expected['rows']) or indices!=list(range(indices[0],indices[-1]+1)):
        raise StateError('item_review_ui_missing_or_fragmented')
    return [{'updateDimensionProperties':{'range':{'sheetId':sheet_id,'dimension':'ROWS',
        'startIndex':indices[0],'endIndex':indices[-1]+1},'properties':{'hiddenByUser':True},'fields':'hiddenByUser'}}]
