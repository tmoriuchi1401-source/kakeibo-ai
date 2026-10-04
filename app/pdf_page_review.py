"""Two visible columns in the existing PDFページ確認; UI is intent, never authority."""
from copy import deepcopy
import json
import re

from .drive_run_state import StateError
from .pdf_grouping_ui import (GroupingSheet, TITLE, QUEUE, SHEET_ID, HEADERS,
                              project, captured_request)
from .pdf_page_kind import KINDS
from .pdf_page_kind import current_answer
from .pdf_grouping_authority import UUID
from .pdf_page_medical import (category_choices, manual_source, manual_values,
                               validate_manual_values)
from .receipt_confirmation import review_id
from .receipt_pdf_units import _digest

SCHEMA = 'pdf-page-review-v1'
LABELS = {'normal': '一般', 'medical': '医療', 'payroll': '給与', 'sensitive_unknown': '判定不能'}
EDITABLE = {'kind_choice', 'kind_action', 'group_target', 'group_action', 'date', 'facility',
            'amount', 'category', 'payment', 'memo', 'medical_action', 'duplicate_target'}
GROUP_ACTIONS = ['確定', '分割', '結合', '拒否', '保留']


def span(numbers):
    return 'p'+str(numbers[0]) + ('-p'+str(numbers[-1]) if len(numbers)>1 else '')


def cards(view, answers, categories, medical_results=None):
    """Routing labels never replace proposal.pages[].classification."""
    proposal = view['proposal']
    if proposal is None:
        raise StateError('grouping_proposal_unavailable')
    result = []
    synthetic = proposal['source_file_id'].startswith('synthetic-')
    medical_results = medical_results or {}
    def add(kind, pages, rows, proof=''):
        identity = {'schema': SCHEMA, 'kind': kind, 'source_file_id': proposal['source_file_id'],
                    'source_content_hash': proposal['source_content_hash'], 'page_numbers': pages,
                    'page_hashes': [proposal['pages'][n-1]['page_hash'] for n in pages],
                    'kind_digests': [answers[n]['confirmation_digest'] if n in answers else '' for n in pages],
                    'review_id': proof}
        if kind != 'medical':
            identity.update(proposal_digest=proposal['proposal_digest'], grouping_version=proposal['grouping_version'])
        readonly = [[field, label, value] for field, label, value in rows if field not in EDITABLE and field not in {'state','result'}]
        token = _digest([identity, readonly, [r[0] for r in rows]])
        result.append({'identity': identity, 'token': token, 'rows': rows})
    general = []
    for page in proposal['pages']:
        n = page['page_number']; answer = answers.get(n)
        kind = answer['human_classification'] if answer else page['classification']
        link = ('original', '原本', 'テストデータ（原本なし）' if synthetic else '原本を開く（p'+str(n)+'）')
        if kind == 'medical' and answer:
            source = manual_source(page, answer)
            key = review_id('medical', source)
            choices = category_choices(categories)
            rows = [('target','対象','p'+str(n)), ('kind','種別','医療（人間確認済み）'),
                    ('state','状態',medical_results.get(key,'医療入力待ち')), link,
                    ('notice','入力方法','原本を見て手入力。患者名・病名・診療内容は入力不要。'),
                    ('date','支払日',''), ('facility','施設名',''), ('amount','実支払額（円）',''),
                    ('category','カテゴリ','医療費' if '医療費' in choices else ''),
                    ('payment','支払方法（任意）',''), ('memo','メモ（任意）',''),
                    ('medical_action','操作',''), ('duplicate_target','重複先（必要時のみ）',''),
                    ('result','処理結果','')]
            add('medical',[n],rows,key)
        elif kind == 'normal':
            general.append(n)
        elif not answer:
            add('page_kind',[n],[('target','対象','p'+str(n)), ('kind','自動判定',LABELS[kind]),
                ('state','状態','ページ種別確認待ち'),link,('kind_choice','ページ種別','未選択'),
                ('kind_action','操作',''),('result','処理結果','')])
        else:
            add('held',[n],[('target','対象','p'+str(n)), ('kind','種別',LABELS[kind]+'（人間確認済み）'),
                ('state','状態','保留・外部送信なし'),link,('result','処理結果','')])
    if general:
        groups = [(i,g) for i,g in enumerate(proposal['groups'],1) if all(n in general for n in g['page_numbers'])]
        summary = '\n'.join('候補'+str(i)+': '+span(g['page_numbers']) for i,g in groups)
        label = ('一般（人間確認済み）' if all(n in answers for n in general) else
                 '一般（自動判定・一部人間確認済み）' if any(n in answers for n in general) else '一般（自動判定）')
        state = {'grouping_confirmed':'grouping確定済み・処理保留','held':'保留','rejected':'拒否'}.get(view['status'],'grouping確認待ち')
        if synthetic:state='テストデータ・'+state
        add('grouping',general,[('target','対象',span(general) if general == list(range(general[0],general[-1]+1)) else ', '.join('p'+str(n) for n in general)),
            ('kind','種別',label), ('state','状態',state), ('original','原本','テストデータ（原本なし）' if synthetic else '原本を開く（p'+str(general[0])+'）'),
            ('groups','現在のgrouping候補',summary), ('notice','判定理由','継続根拠が不足するページは単独候補。一般の回答だけではAI送信を許可しません。'),
            ('group_target','対象候補（分割・結合時）',''), ('group_action','操作',''), ('result','処理結果',view.get('result','会計・AI・元PDF移動は未実行'))])
    return result


def check_snapshot(snapshot, expected):
    if (not isinstance(snapshot,dict) or set(snapshot) != {'identity','token','rows','original_link'} or
            snapshot['identity'] != expected['identity'] or snapshot['token'] != expected['token'] or
            not isinstance(snapshot['rows'],list) or len(snapshot['rows']) != len(expected['rows'])):
        raise StateError('stale_proposal')
    for current, trusted in zip(snapshot['rows'],expected['rows']):
        if len(current)!=3 or current[:2] != list(trusted[:2]) or (trusted[0] not in EDITABLE | {'state','result'} and current[2] != trusted[2]):
            raise StateError('pdf_review_presentation_changed')
    ident=expected['identity'];n=ident['page_numbers'][0]
    link=original_uri(ident['source_file_id'], n)
    if snapshot['original_link'] != link:
        raise StateError('pdf_review_original_link_changed')
    return {r[0]:r[2] for r in snapshot['rows']}


def original_uri(source_id, number):
    if source_id.startswith('synthetic-'):
        return ''
    if not re.fullmatch(r'[A-Za-z0-9_-]{10,150}',source_id):
        raise StateError('pdf_review_source_invalid')
    return 'https://drive.google.com/file/d/'+source_id+'/view#page='+str(number)


class PageReviewSheet(GroupingSheet):
    """Reuses the same tab IDs and hidden request queue. No ledger API surface."""
    def __init__(self, service, spreadsheet_id, categories, *, load_medical_results=None):
        super().__init__(service,spreadsheet_id)
        self.categories=categories
        self.load_medical_results=load_medical_results or (lambda: {})

    def _rows(self):
        return self.service.spreadsheets().values().get(spreadsheetId=self.sid,
            range=f"'{TITLE}'!A1:N1001",valueRenderOption='FORMATTED_VALUE').execute(num_retries=0).get('values',[])

    def install(self):
        meta=self.service.spreadsheets().get(spreadsheetId=self.sid,fields='sheets(properties)').execute(num_retries=0)
        owned=next((s['properties'] for s in meta['sheets'] if s['properties']['title']==TITLE),None)
        queue=next((s['properties'] for s in meta['sheets'] if s['properties']['title']==QUEUE),None)
        if not owned or owned['sheetId']!=SHEET_ID or not queue:
            raise StateError('pdf_review_existing_ui_required')
        header=self._get(TITLE,'A1:P1')
        if header not in ([HEADERS],[['項目','内容',SCHEMA]]):
            raise StateError('pdf_review_schema_mismatch')
        # Migration is explicit. Reject queued old requests; never reinterpret them.
        if header == [HEADERS]:
            if any(len(r)>1 and r[1] in {'accepted','dispatching'} for r in self._get(QUEUE,'A2:F1001')):
                raise StateError('pdf_review_pending_legacy_request')
            self._write([{'range':f"'{TITLE}'!A1:P1001",'values':[['項目','内容',SCHEMA]+['']*13]+[['']*16 for _ in range(1000)]}])
        requests=[{'setDataValidation':{'range':{'sheetId':SHEET_ID,'startRowIndex':1,'endRowIndex':1001,'startColumnIndex':0,'endColumnIndex':16}}},
            {'updateDimensionProperties':{'range':{'sheetId':SHEET_ID,'dimension':'COLUMNS','startIndex':0,'endIndex':2},'properties':{'hiddenByUser':False},'fields':'hiddenByUser'}},
            {'updateDimensionProperties':{'range':{'sheetId':SHEET_ID,'dimension':'COLUMNS','startIndex':2,'endIndex':16},'properties':{'hiddenByUser':True},'fields':'hiddenByUser'}},
            {'updateSheetProperties':{'properties':{'sheetId':SHEET_ID,'gridProperties':{'frozenRowCount':1,'frozenColumnCount':0}},'fields':'gridProperties.frozenRowCount,gridProperties.frozenColumnCount'}},
            {'repeatCell':{'range':{'sheetId':SHEET_ID,'startRowIndex':0,'endRowIndex':1001,'startColumnIndex':0,'endColumnIndex':16},'cell':{'userEnteredFormat':{'wrapStrategy':'WRAP','verticalAlignment':'TOP','textFormat':{'foregroundColor':{'red':.15,'green':.15,'blue':.15},'underline':False}}},'fields':'userEnteredFormat.wrapStrategy,userEnteredFormat.verticalAlignment,userEnteredFormat.textFormat'}}]
        for i,width in ((0,95),(1,190)):
            requests.append({'updateDimensionProperties':{'range':{'sheetId':SHEET_ID,'dimension':'COLUMNS','startIndex':i,'endIndex':i+1},'properties':{'pixelSize':width},'fields':'pixelSize'}})
        requests.append({'repeatCell':{'range':{'sheetId':SHEET_ID,'startRowIndex':0,'endRowIndex':1,'startColumnIndex':0,'endColumnIndex':2},'cell':{'userEnteredFormat':{'textFormat':{'bold':True}}},'fields':'userEnteredFormat.textFormat.bold'}})
        self.service.spreadsheets().batchUpdate(spreadsheetId=self.sid,body={'requests':requests}).execute(num_retries=0)

    def read_card(self, token):
        rows=[r for r in self._rows()[1:] if len(r)>3 and r[2]==SCHEMA and r[3]==token]
        if not rows:
            raise StateError('pdf_review_card_missing')
        identity=json.loads(rows[0][4])
        if any(r[4]!=rows[0][4] for r in rows):
            raise StateError('pdf_review_card_identity_changed')
        start=next(i for i,r in enumerate(self._rows(),1) if len(r)>3 and r[3]==token)
        link_index=next(i for i,r in enumerate(rows) if r[5]=='original')
        linked=self.service.spreadsheets().get(spreadsheetId=self.sid,ranges=[f"'{TITLE}'!B{start+link_index}"],
            fields='sheets(data(rowData(values(userEnteredFormat(textFormat(link)),hyperlink))))').execute(num_retries=0)
        cell=linked['sheets'][0]['data'][0]['rowData'][0]['values'][0]
        uri=cell.get('userEnteredFormat',{}).get('textFormat',{}).get('link',{}).get('uri') or cell.get('hyperlink','')
        return {'identity':identity,'token':token,'rows':[[r[5],r[0],r[1] if len(r)>1 else ''] for r in rows], 'original_link':uri}

    def publish_cards(self, projected):
        old=self._rows()
        # Caller supplies ALL Drive-backed cards; never reconstruct a view from
        # hidden spreadsheet cells, or strip links/validation from other cards.
        previous={(r[3],r[5]):r[1] for r in old[1:] if len(r)>5 and r[2]==SCHEMA}
        rows=[];requests=[]
        for card in projected:
            identity=card['identity'];first=identity['page_numbers'][0]
            for field,label,default in card['rows']:
                value=previous.get((card['token'],field),default) if field in EDITABLE else default
                rows.append([label,value,SCHEMA,card['token'],json.dumps(identity,separators=(',',':')),field]+['']*8)
                n=len(rows)
                region={'sheetId':SHEET_ID,'startRowIndex':n,'endRowIndex':n+1,'startColumnIndex':1,'endColumnIndex':2}
                lines=max(len(str(value).splitlines()),(len(str(value))+17)//18,1)
                requests.append({'updateDimensionProperties':{'range':{'sheetId':SHEET_ID,'dimension':'ROWS','startIndex':n,'endIndex':n+1},'properties':{'pixelSize':min(600,max(36,lines*20+10))},'fields':'pixelSize'}})
                requests.append({'repeatCell':{'range':region,'cell':{'userEnteredFormat':{'backgroundColor':({'red':1,'green':.98,'blue':.88} if field in EDITABLE else {'red':1,'green':1,'blue':1})}},'fields':'userEnteredFormat.backgroundColor'}})
                if field=='original':
                    uri=original_uri(identity['source_file_id'],first)
                    if uri:
                        requests.append({'repeatCell':{'range':region,'cell':{'userEnteredFormat':{'textFormat':{'link':{'uri':uri},'foregroundColor':{'red':.1,'green':.32,'blue':.73},'underline':True}}},'fields':'userEnteredFormat.textFormat.link,userEnteredFormat.textFormat.foregroundColor,userEnteredFormat.textFormat.underline'}})
                choices=None
                if field=='kind_choice':choices=['未選択',*KINDS]
                if field=='kind_action':choices=['種別を確定','保留']
                if field=='group_action':choices=GROUP_ACTIONS
                if field=='group_target':
                    eligible=[int(n) for f,l,v in card['rows'] if f=='groups' for n in re.findall(r'候補([0-9]+):',v)]
                    choices=[str(n) for n in eligible]+[str(a)+'+'+str(b) for a,b in zip(eligible,eligible[1:]) if b==a+1]
                if field=='category':choices=list(category_choices(self.categories))
                if field=='payment':
                    from .receipt_confirmation_ui import PAYMENTS
                    choices=PAYMENTS
                if field=='date':
                    requests.extend([{'setDataValidation':{'range':region,'rule':{'condition':{'type':'DATE_IS_VALID'},'strict':True,'showCustomUi':True}}},
                        {'repeatCell':{'range':region,'cell':{'userEnteredFormat':{'numberFormat':{'type':'DATE','pattern':'yyyy/mm/dd'}}},'fields':'userEnteredFormat.numberFormat'}}])
                if field=='amount':
                    requests.extend([{'setDataValidation':{'range':region,'rule':{'condition':{'type':'NUMBER_GREATER','values':[{'userEnteredValue':'0'}]},'strict':True,'showCustomUi':True}}},
                        {'repeatCell':{'range':region,'cell':{'userEnteredFormat':{'numberFormat':{'type':'NUMBER','pattern':'#,##0.########'}}},'fields':'userEnteredFormat.numberFormat'}}])
                if field=='medical_action':
                    values={f:previous.get((card['token'],f),v) for f,_,v in card['rows']}
                    # Real validator governs whether confirm appears in dropdown.
                    try:
                        validate_manual_values(manual_values(values,self.categories),self.categories)
                        choices=['保留','医療費を確定','既存支出と重複（紐付け）','重複候補と別の支出として確定']
                    except ValueError:choices=['保留']
                if choices:
                    requests.append({'setDataValidation':{'range':region,'rule':{'condition':{'type':'ONE_OF_LIST','values':[{'userEnteredValue':v} for v in choices]},'strict':field!='payment','showCustomUi':True}}})
            rows.append(['']*14)
        if len(rows)>1000:raise StateError('pdf_review_capacity')
        all_rows=rows
        self._write([{'range':f"'{TITLE}'!A2:N1001",'values':all_rows+[['']*14]*(1000-len(all_rows))}])
        # Removed cards must not leave stale links or validators behind.
        clear={'range':{'sheetId':SHEET_ID,'startRowIndex':1,'endRowIndex':1001,'startColumnIndex':0,'endColumnIndex':2}}
        self.service.spreadsheets().batchUpdate(spreadsheetId=self.sid,body={'requests':[
            {'setDataValidation':clear}, {'repeatCell':{**clear,'cell':{'userEnteredFormat':{'textFormat':{'foregroundColor':{'red':.15,'green':.15,'blue':.15},'underline':False}}},'fields':'userEnteredFormat.textFormat'}},*requests]}).execute(num_retries=0)

    def finish_card(self, request_id, snapshot, result, now):
        n,current=self.request(request_id)
        if current!=snapshot:raise StateError('grouping_request_replaced')
        self._write([{'range':f"'{QUEUE}'!B{n}",'values':[['complete']]},
            {'range':f"'{QUEUE}'!E{n}:F{n}",'values':[[result,now]]}])


def process_page_request(kinds, sheet, request_id, *, medical_factory=None):
    try:
        return _process_page_request(kinds,sheet,request_id,medical_factory=medical_factory)
    except StateError as error:
        # Refused/stale input must not remain a busy request forever. Unknown
        # Drive/accounting writes keep the existing recovery path untouched.
        if str(error) in {'stale_proposal','page_kind_source_changed','page_kind_page_changed',
                'pdf_review_presentation_changed','pdf_review_original_link_changed',
                'pdf_medical_owner_inputs_changed','medical_manual_input_required',
                'pdf_grouping_medical_target_forbidden','confirmation_source_changed'}:
            _,captured=sheet.request(request_id)
            sheet.finish_card(request_id,captured,str(error),kinds.grouping.clock())
        raise


def _process_page_request(kinds, sheet, request_id, *, medical_factory=None):
    if not re.fullmatch(UUID,request_id):raise StateError('pdf_review_request_invalid')
    _,snapshot=sheet.request(request_id)
    if not isinstance(snapshot,dict) or 'identity' not in snapshot:
        raise StateError('pdf_review_request_invalid')
    identity=snapshot['identity'];g=kinds.grouping
    value,proposal,answers=kinds.current(identity['source_file_id'])
    record=value['records'][_digest(proposal['source_file_id'])]
    view=g.view(record)
    projected=cards(view,answers,sheet.categories)
    previous=next((e for e in value['audit'] if e['request_id']==request_id),None)
    if previous:
        if previous['request_digest']!=_digest(snapshot):raise StateError('grouping_request_replaced')
        if (previous['source_content_hash']!=proposal['source_content_hash'] or
                previous['proposal_digest']!=proposal['proposal_digest'] or previous['after_revision']!=proposal['grouping_version']):
            raise StateError('stale_proposal')
        if previous['operation']=='page_kind':
            n=identity['page_numbers'][0]
            if n not in answers or _digest([answers[n]['confirmation_digest']])!=previous['confirmation_digest']:
                raise StateError('stale_proposal')
        else:
            from .pdf_general_grouping import scope_current
            authority=record['confirmation']
            if (not scope_current(value,proposal) or
                    (authority['confirmation_digest'] if authority else '')!=previous['confirmation_digest']):
                raise StateError('stale_proposal')
        publish_current(kinds,sheet)
        sheet.finish_card(request_id,snapshot,previous['result'],g.clock())
        return previous['result']
    expected=next((c for c in projected if c['token']==snapshot.get('token')),None)
    if not expected:raise StateError('stale_proposal')
    fields=check_snapshot(snapshot,expected)
    result='保留'
    medical_results={}
    if identity['kind']=='page_kind' and fields['kind_action']=='種別を確定':
        kinds.confirm(proposal,{identity['page_numbers'][0]:fields['kind_choice']},request_id=request_id,intent_digest=_digest(snapshot))
        result='ページ種別確認済み'
    elif identity['kind']=='grouping':
        if fields['group_action'] in {'分割','結合'}:
            selected=str(fields['group_target']).split('+')
            if any(not n.isdigit() or not 1<=int(n)<=len(proposal['groups']) or
                   not all(p in identity['page_numbers'] for p in proposal['groups'][int(n)-1]['page_numbers']) for n in selected):
                raise StateError('pdf_grouping_medical_target_forbidden')
        legacy=project(view);legacy[7:9]=[fields['group_action'],fields['group_target']]
        request=captured_request(legacy,request_id,proposal)
        view=g.review(request,intent_digest=_digest(snapshot));result=view['result']
    elif identity['kind']=='medical':
        if fields['medical_action'] not in {'保留','医療費を確定','既存支出と重複（紐付け）','重複候補と別の支出として確定'}:
            raise StateError('medical_manual_input_required')
        if medical_factory is None:raise StateError('pdf_medical_manual_backend_required')
        page=proposal['pages'][identity['page_numbers'][0]-1]
        source=manual_source(page,answers.get(page['page_number']))
        def owner_inputs():
            fresh_value,fresh_proposal,fresh_answers=kinds.current(identity['source_file_id'])
            fresh_view=g.view(fresh_value['records'][_digest(identity['source_file_id'])])
            fresh_card=next((c for c in cards(fresh_view,fresh_answers,sheet.categories) if c['identity'].get('review_id')==identity['review_id']),None)
            if not fresh_card:raise StateError('confirmation_source_changed')
            current=sheet.read_card(snapshot['token'])
            current_fields=check_snapshot(current,fresh_card)
            # Result/status are projection, not input authority. A capture sets
            # "受付中" after saving the request. Compare ONLY owner fields.
            if any(current_fields.get(f)!=fields.get(f) for f in EDITABLE):
                raise StateError('pdf_medical_owner_inputs_changed')
            return manual_values(current_fields,sheet.categories)
        submitted=manual_values(fields,sheet.categories)
        result=medical_factory(source,owner_inputs).confirm(submitted_inputs=submitted)
        medical_results[identity['review_id']]=result
    elif identity['kind']!='page_kind':raise StateError('pdf_review_request_invalid')
    _,proposal,answers=kinds.current(identity['source_file_id'])
    latest=g.display(identity['source_file_id'])
    publish_current(kinds,sheet,medical_results)
    sheet.finish_card(request_id,snapshot,result,g.clock())
    return result


def publish_current(kinds,sheet,medical_results=None):
    value=kinds.grouping.store.load()
    projected=[]
    medical_results={**sheet.load_medical_results(),**(medical_results or {})}
    for record in sorted(value['records'].values(),key=lambda r: (r['proposal'] or {}).get('source_file_id','').startswith('synthetic-')):
        p=record['proposal']
        if p is None:continue
        answers={page['page_number']:a for page in p['pages'] if (a:=current_answer(value,page))}
        projected.extend(cards(kinds.grouping.view(record),answers,sheet.categories,medical_results))
    sheet.publish_cards(projected)
