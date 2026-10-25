"""Explicit Google/connector boundary around the unchanged memory-only #158.

No credentials, scheduler, native/financial factory, authority or new database.
Live identities and the fixture owner remain separate namespaces. Two uploaded
fixture images must have independently verified identical bytes. Everything
else stays held. This is an isolated integration experiment, not a ledger gate.
"""
from copy import deepcopy
from dataclasses import asdict

from app.drive_run_state import StateError
from app.iphone_sheet_reader import (RawReader, issue_contract, cell_content,
    raw_value, GoogleRawTransport)
from app.unified_receipt_review import digest
from app.iphone_persistent_drafts import PersistentJournal
from .sheets_only_surface import physical, literal, column, rule, IDLE
from .sheets_only_review import uid
from .sheets_registration import SheetsRegistration

SLOT = 'live_synthetic_sheet_outbox'
PIN_SLOT = 'live_synthetic_terminal_inputs'


def terminal_inputs(journal,slot):
    """Index verified historical per-case inputs without relying on row numbers.

    Explicit test archival preserves this index alongside the full old bytes.
    A terminal denial/partial save stays immutable after unrelated case edits.
    """
    state,_,_=journal._state();pins=deepcopy(state['details'].get(PIN_SLOT,{}))
    batches={b['batch']['snapshot_digest']:b['batch'] for b in state['history']+[state['legacy']]}
    for key,r in state['details'].get(slot,{}).get('requests',{}).items():
        if not r['sequence_accepted'] or r['status'] in {'ATTEMPTING','UNKNOWN'}:continue
        batch=batches.get(r['core_digest'])
        if batch:
            pins[key]=deepcopy(next(d for d in batch['drafts'].values() if d['case_id']==r['case_id']))
    return pins


def fail(reason):
    raise StateError('live_synthetic_sheet_'+reason)


def compact(raw):
    """Keep security metadata and raw values; ignore render/cache/defaults."""
    v=deepcopy(raw)
    if len(v.get('sheets',[]))!=1: fail('one_tab_required')
    s=v['sheets'][0]
    result=dict(spreadsheetId=v['spreadsheetId'],sheets=[{
        k:deepcopy(s[k]) for k in ('properties','protectedRanges','tables') if k in s}])
    blocks=s.get('data',[])
    if len(blocks)!=1 or blocks[0].get('startRow',0)!=0 or blocks[0].get('startColumn',0)!=0:
        fail('coverage')
    rows=[]
    for row in blocks[0].get('rowData',[]):
        cells=[cell_content(c) for c in row.get('values',[])]
        if any(cells[19:]): fail('unexpected_column')
        rows.append(dict(values=cells[:19]+[{}]*max(0,19-len(cells))))
    while rows and not any(rows[-1]['values']): rows.pop()
    if len(rows)!=9: fail('eight_cases_required')
    result['sheets'][0]['data']=[dict(rowData=rows)]
    return result


def core(raw):
    v=compact(raw);s=v['sheets'][0]
    for row in s['data'][0]['rowData']:
        values=row['values'];row['values']=values[:1]+values[3:]
    for p in s.get('protectedRanges',[]):
        opened=[]
        for r in p.get('unprotectedRanges',[]):
            if r['startColumnIndex']==1 and r['endColumnIndex']==2: continue
            if r['startColumnIndex']<3: fail('unexpected_protection')
            q=deepcopy(r);q['startColumnIndex']-=2;q['endColumnIndex']-=2;opened.append(q)
        p['unprotectedRanges']=opened
        p.pop('requestingUserCanEdit',None)
    for t in s.get('tables',[]):
        t['range']['endColumnIndex']-=2
        columns=[]
        for c in t.get('columnProperties',[]):
            i=c.get('columnIndex',0)
            if i in {1,2}: continue
            q=deepcopy(c)
            if i>2:q['columnIndex']=i-2
            columns.append(q)
        if 'columnProperties' in t:t['columnProperties']=columns
    return v


def principal(profile,acl,book):
    try:
        permissions=acl['permissions']
        if (acl['id']!=book or acl['shared'] is not False or len(permissions)!=1
                or permissions[0]['type']!='user' or permissions[0]['role']!='owner'
                or permissions[0]['emailAddress']!=profile['email']
                or not profile['id'] or acl.get('current_user_can_share') is not True
                or '完全合成' not in acl['title']):
            fail('private_owner_scope_required')
        return digest(['connected-google-drive-principal',profile['id'],profile['email']]),digest(permissions)
    except (KeyError,TypeError): fail('permission_evidence_missing')


class GoogleResultTransport:
    """Reuse the GET-only transport; add ONLY bounded result/validation writes.

    Inject an already authorized client. No ADC/env/credential discovery. The
    connector broker can execute the same structured requests without exporting
    its credentials to Python. Google Sheets has no asserted atomic input CAS.
    """
    def __init__(self,service,bridge,*,enabled=False):
        if enabled is not True or type(bridge) is not LiveSyntheticBridge:
            fail('explicit_synthetic_bridge_required')
        self.bridge=bridge;self.reader=GoogleRawTransport(service,bridge.contract)
        self.book=bridge.book;self.sid=bridge.sid;self.service=service

    def read(self):
        return self.reader.read()

    def publish(self,key,*,sources,profile,acl):
        requests=self.bridge.ready(key,self.read(),sources,profile,acl)
        validate_writes(requests,self.sid)
        return self.service.spreadsheets().batchUpdate(spreadsheetId=self.book,
            body=dict(requests=deepcopy(requests))).execute(num_retries=0)


def validate_writes(requests,sid):
    if type(requests) is not list or not 1<=len(requests)<=16: fail('write_scope')
    for request in requests:
        if set(request)!={'updateCells'}: fail('write_scope')
        r=request['updateCells'];p=r.get('range',{})
        if (set(r)!={'range','rows','fields'} or p.get('sheetId')!=sid
                or type(p.get('startRowIndex')) is not int or not 1<=p['startRowIndex']<=8
                or p.get('endRowIndex')!=p['startRowIndex']+1
                or len(r['rows'])!=1 or len(r['rows'][0]['values'])!=1): fail('write_scope')
        c=r['rows'][0]['values'][0]
        if p.get('startColumnIndex')==2 and p.get('endColumnIndex')==3:
            if (r['fields']!='userEnteredValue' or set(c)!={'userEnteredValue'}
                    or set(c['userEnteredValue'])!={'formulaValue'}): fail('write_scope')
        elif p.get('startColumnIndex')==1 and p.get('endColumnIndex')==2:
            if r['fields']!='dataValidation' or set(c)!={'dataValidation'}:fail('write_scope')
        else:fail('write_scope')


class LiveSyntheticBridge:
    def __init__(self,worker,baseline,sources,profile,acl,*,enabled=False,journal=None,display_cases=None):
        if enabled is not True or type(worker) not in {SheetsRegistration,SyntheticRegistrationPlanner}:
            fail('explicit_synthetic_worker_required')
        self.worker=worker;self.surface=worker.surface;self.journal=worker.journal
        self.baseline=compact(baseline);self.book=self.baseline['spreadsheetId']
        self.sid=self.baseline['sheets'][0]['properties']['sheetId']
        self.principal,self.acl=principal(profile,acl,self.book)
        self.sources=deepcopy(sources)
        self.contract=issue_contract(core(self.baseline),list(sources.values()))
        self.display_cases=set(self.contract['cases'] if display_cases is None else display_cases)
        if not self.display_cases or not self.display_cases<=set(self.contract['cases']):fail('display_scope')
        self.structure=digest({k:self.baseline['sheets'][0].get(k) for k in ('properties','protectedRanges','tables')})
        # Real typed table DROPDOWN validation lives in columnProperties, not
        # necessarily in CellData. Preserve that native rule/appearance. The
        # backend sequence check remains authoritative, not a dropdown choice.
        self.table_dropdown=any(c.get('columnIndex')==1 and c.get('columnType')=='DROPDOWN'
            for t in self.baseline['sheets'][0].get('tables',[]) for c in t.get('columnProperties',[]))
        self.scope=digest([self.book,self.sid,self.principal,self.acl,digest(self.contract)])
        if journal is not None:
            if (type(journal) is not PersistentJournal or journal.scope['owner']!=self.principal
                    or journal.binding!=digest(self.contract)
                    or getattr(journal.transport.io,'synthetic_memory_only',False) is not True):
                fail('isolated_delivery_journal_required')
            self.journal=journal
        self.latest_batch=RawReader(self.contract,lambda:core(self.baseline),lambda:deepcopy(sources)).read()
        for cid in worker.handoff.confirmation.cases:
            external=next(s for s in sources.values() if s['case_id']==cid)
            internal=self.surface.contract['cases'][cid]['identity']
            if external['sha256']!=internal['sha256']:fail('fixture_bytes_not_identical')

    def capture(self,raw,sources,profile,acl):
        if principal(profile,acl,self.book)!=(self.principal,self.acl):fail('permission_changed')
        v=compact(raw);s=v['sheets'][0]
        if v['spreadsheetId']!=self.book or digest({k:s.get(k) for k in ('properties','protectedRanges','tables')})!=self.structure:
            fail('structure_changed')
        batch=RawReader(self.contract,lambda:core(v),lambda:deepcopy(sources)).read()
        commands={}
        for cid,c in self.contract['cases'].items():
            command=s['data'][0]['rowData'][c['row_index']]['values'][1]
            if set(command.get('userEnteredValue',{}))!={'stringValue'}:fail('command_literal_required')
            commands[cid]=raw_value(command)
        return v,batch,commands,digest([asdict(batch),commands])

    def _journal(self,records,identity,stage):
        state,_,tag=self.journal._state();details=deepcopy(state['details'])
        current=deepcopy(details.get(SLOT,{}));wanted=records[identity];old=current.get(identity)
        if stage=='intent':
            if old is not None or wanted['state']!='ATTEMPTING':fail('outbox_read_only')
        else:
            expected={'sending':'SENDING','delivered':'DELIVERED','cancelled':'CANCELLED'}[stage]
            allowed={'ATTEMPTING','SENDING'} if stage=='delivered' else {'ATTEMPTING'}
            if not old or old['state'] not in allowed or dict(old,state=expected)!=wanted:
                fail('outbox_read_only')
        # Merge only this immutable message/allowed transition, then use the
        # existing journal CAS. A stale copy must not overwrite another sender.
        current[identity]=deepcopy(wanted);details[SLOT]=current
        batch=self.worker.handoff.reader.read() if self.journal is self.worker.journal else self.latest_batch
        result=self.journal.save(batch,tag,uid('live-sheet:'+identity+':'+stage),details=details)
        if result['status']!='draft_saved':fail('journal_read_only')

    def _records(self):
        state,_,_=self.journal._state();v=deepcopy(state['details'].get(SLOT,{}))
        if type(v) is not dict or len(v)>64:fail('outbox_quarantined')
        for key,r in v.items():
            payload=[self.scope,r['case_id'],r['input_digest'],r['formula'],r['rule']]
            if 'predecessor' in r:payload.append(r['predecessor'])
            if (r['scope']!=self.scope or key!=digest(payload)
                    or r['state'] not in {'ATTEMPTING','SENDING','DELIVERED','CANCELLED'}):fail('outbox_quarantined')
        return v

    def poll(self,first,second,sources,profile,acl):
        a=self.capture(first,sources,profile,acl);b=self.capture(second,sources,profile,acl)
        if a[3]!=b[3]:fail('concurrent_edit')
        raw,batch,commands,input_digest=b;self.latest_batch=batch
        # Publisher uncertainty is independent of native uncertainty. Resolve
        # exact read-back only, BEFORE any further backend input adoption.
        records=self._records();recoverable=[]
        for key,r in records.items():
            if r['state'] not in {'ATTEMPTING','SENDING'}:continue
            cells=raw['sheets'][0]['data'][0]['rowData'][self.contract['cases'][r['case_id']]['row_index']]['values']
            if (r['state']=='ATTEMPTING' and r.get('claim_required') is True
                    and (r['input_digest']!=input_digest or digest(cells[2])!=r['predecessor'])):
                # CAS permits cancellation ONLY before the single-use send
                # claim. An already claimed/uncertain write is never cancelled.
                r['state']='CANCELLED';self._journal(records,key,'cancelled');continue
            if (cells[2].get('userEnteredValue')=={'formulaValue':r['formula']}
                    and cells[1].get('dataValidation')==r['rule']):
                r['state']='DELIVERED';self._journal(records,key,'delivered')
            elif r['state']=='ATTEMPTING' and r.get('claim_required') is True:
                recoverable.append(dict(id=key,case_id=r['case_id'],requests=self.requests(r['case_id'],r['formula'],r['rule'])))
            else:
                return dict(results=[dict(status='UNKNOWN',case_id=r['case_id'],reason='result publication unresolved')],
                            pending=[],input_digest=input_digest,financial_writes=0,authority_calls=0,synthetic_only=True)
        if recoverable:
            return dict(results=[dict(status='WAITING',case_id=p['case_id'],reason='unsent display claim pending') for p in recoverable],
                        pending=recoverable,input_digest=input_digest,financial_writes=0,authority_calls=0,synthetic_only=True)
        native=self.worker._state()[1]
        previous=self.worker.handoff._state()[1]
        unresolved=(self.worker.uncertain or self.worker.handoff.uncertain or self.worker._other_pending()
            or any(r['status'] in {'ATTEMPTING','UNKNOWN'} for r in native['requests'].values())
            or self.worker.handoff._unresolved(previous))
        if unresolved:
            # Saved-result lookup only: do not even mirror new input/commands
            # into the memory-native surface once a write outcome is unknown.
            results=self.worker.poll()
        else:
            results=self._advance(batch,commands)
        records=self._records();pending=[]
        for cid,e in self.surface.expected.items():
            if cid not in self.display_cases:continue
            current=raw['sheets'][0]['data'][0]['rowData'][self.contract['cases'][cid]['row_index']]['values']
            r=next((r for r in results if r.get('case_id')==cid),None)
            status=r['status'] if r else 'UNKNOWN' if e['text'].startswith('UNKNOWN') else 'HELD'
            label=dict(VALIDATED_UNPOSTED='登録検証済み・未記帳',HELD='保留・入力を保持しています',
                SYNC_WAIT='受付待ち・同期を確認しています',WAITING='受付待ち・通信を確認しています',
                CONFLICT='競合・新しい入力を採用していません',PERMISSION_UNVERIFIED='権限未検証・未記帳',
                UNKNOWN='UNKNOWN・結果照合待ち／自動再送しません').get(status,'保留・未記帳')
            text=label+'\n案件 '+cid+'\n合成専用・記帳処理には接続していません'
            if r and r.get('request_id'):text+='\n受付 '+r['request_id'][:8]
            if status=='HELD' and cid in self.worker.handoff.confirmation.cases:
                ledger=self.worker._state()[1]
                if not any(r['case_id']==cid and r['status']=='VALIDATED_UNPOSTED' for r in ledger['requests'].values()):
                    text+='\n必須項目を入力後、次は「登録 '+str(self.worker._sequence(ledger,cid)+1)+'」'
            formula=self.formula(cid,commands[cid],text,batch)
            wanted=None if self.table_dropdown else e['options']
            predecessor=digest(current[2])
            key=digest([self.scope,cid,input_digest,formula,wanted,predecessor])
            exact=(current[2].get('userEnteredValue')=={'formulaValue':formula} and current[1].get('dataValidation')==wanted)
            if key in records:
                if exact and records[key]['state'] in {'ATTEMPTING','SENDING'}:
                    records[key]['state']='DELIVERED';self._journal(records,key,'delivered')
                continue  # Lost response/restart: NEVER resend this write.
            if exact:continue
            records[key]=dict(scope=self.scope,case_id=cid,input_digest=input_digest,formula=formula,
                              rule=deepcopy(wanted),state='ATTEMPTING',status=status,predecessor=predecessor,claim_required=True)
            self._journal(records,key,'intent')
            pending.append(dict(id=key,case_id=cid,requests=self.requests(cid,formula,wanted)))
        return dict(results=results,pending=pending,input_digest=input_digest,
                    financial_writes=0,authority_calls=0,synthetic_only=True)

    def _advance(self,batch,commands):
        for cid in self.worker.handoff.confirmation.cases:
            live=next(d for d in batch.drafts.values() if d['case_id']==cid)
            for i in self.contract['cases'][cid]['editable']:
                # Copy values only into a separate memory snapshot. NEVER copy
                # live source IDs, ACL, notes, revisions or a live IO capability.
                self.surface.row(cid)[physical(i)]['userEnteredValue']=deepcopy(live['cells'][i].get('userEnteredValue',{}))
        for cid,command in commands.items():
            self.surface.row(cid)[1]['userEnteredValue']={'stringValue':command}
        # The core fingerprints a whole-batch snapshot. A later edit to ANOTHER
        # case must not turn an unchanged completed request into a new duplicate
        # request. Reuse its immutable receipt, only when its exact saved value
        # AND source still match. Never suppress a changed own input/command.
        historical=[];suppressed=[]
        ledger=self.worker._state()[1]
        pins=terminal_inputs(self.worker.journal,self.worker.slot)
        current=self.worker.handoff.reader.read()
        for key,r in ledger['requests'].items():
            cid=r['case_id']
            if cid in suppressed or r['command']!=commands[cid]:continue
            draft=next(d for d in current.drafts.values() if d['case_id']==cid)
            if (r['sequence_accepted'] and r['status'] not in {'ATTEMPTING','UNKNOWN'}
                    and r['command']=='登録 '+str(self.worker._sequence(ledger,cid))
                    and key in pins and pins[key]==draft):
                self.surface.row(cid)[1]['userEnteredValue']={'stringValue':IDLE}
                suppressed.append(cid);historical.append(self.worker.inspect(key))
                self.worker._project(cid,r,current,ledger)
                continue
            if r['status']!='VALIDATED_UNPOSTED':continue
            try:
                _,frame=self.worker.handoff._capture(cid,'import_draft')
            except StateError:continue
            if frame['value']==r['frame']['value'] and frame['source']==r['frame']['source']:
                self.surface.row(cid)[1]['userEnteredValue']={'stringValue':IDLE}
                suppressed.append(cid);historical.append(self.worker.inspect(key))
        try:results=self.worker.poll()+historical
        finally:
            for cid in suppressed:self.surface.row(cid)[1]['userEnteredValue']={'stringValue':commands[cid]}
        return results

    def ready(self,key,raw,sources,profile,acl):
        view,batch,_,current=self.capture(raw,sources,profile,acl);self.latest_batch=batch
        r=self._records().get(key)
        if not r or r['state']!='ATTEMPTING' or current!=r['input_digest']:
            fail('changed_before_publish')
        cells=view['sheets'][0]['data'][0]['rowData'][self.contract['cases'][r['case_id']]['row_index']]['values']
        if 'predecessor' in r and digest(cells[2])!=r['predecessor']:fail('display_changed_before_publish')
        records=self._records()
        if records[key]['state']!='ATTEMPTING':fail('outbox_read_only')
        records[key]['state']='SENDING';self._journal(records,key,'sending')
        return self.requests(r['case_id'],r['formula'],r['rule'])

    def formula(self,cid,command,text,batch):
        c=self.contract['cases'][cid];n=c['row_index']+1
        draft=next(d for d in batch.drafts.values() if d['case_id']==cid)
        checks=['EXACT(A'+str(n)+','+literal(cid)+')']
        for i in sorted((set() if text.startswith('UNKNOWN') else set(c['editable']))|{15,16}):
            value=raw_value(draft['cells'][i]);ref=column(physical(i))+str(n)
            checks.append(ref+'='+str(value) if type(value) in (int,float) else 'EXACT('+ref+','+literal(value)+')')
        body=literal(text) if text.startswith('UNKNOWN') else 'IF(B'+str(n)+'='+literal(command)+','+literal(text)+','+literal('受付待ち・次の処理を待ってください')+')'
        changed=('UNKNOWN・入力／原本／案件対応も変化。自動再送せず照合します。' if text.startswith('UNKNOWN')
                 else '競合・入力／原本／案件対応が変化しました。入力を保持しています。')
        return '=IF(AND('+','.join(checks)+'),'+body+','+literal(changed)+')'

    def requests(self,cid,formula,validation):
        n=self.contract['cases'][cid]['row_index'];requests=[]
        writes=[(2,dict(userEnteredValue=dict(formulaValue=formula)),'userEnteredValue')]
        if validation is not None:writes.append((1,dict(dataValidation=validation),'dataValidation'))
        for col,value,field in writes:
            requests.append(dict(updateCells=dict(range=dict(sheetId=self.sid,startRowIndex=n,endRowIndex=n+1,
                startColumnIndex=col,endColumnIndex=col+1),rows=[dict(values=[value])],fields=field)))
        validate_writes(requests,self.sid)
        return requests


class SyntheticRegistrationPlanner(SheetsRegistration):
    """Same native import/validation/CAS; pure presentation planning only.

    The #158 publisher protects a MEMORY surface. The live bridge has its own
    acknowledged encrypted delivery journal and single-use send claim instead.
    No production owner gate or native state-machine condition is replaced.
    """
    def _project(self,cid,record,batch,ledger):
        text=self._display(cid,record)
        options=rule(list(dict.fromkeys([IDLE,record['command']]+self._options(ledger,cid,record))))
        self.surface.expected[cid]=dict(command=record['command'],text=text,batch=deepcopy(batch),
            formula={'userEnteredValue':{'formulaValue':self.surface.formula(cid,record['command'],text,batch)}},
            options=options)
