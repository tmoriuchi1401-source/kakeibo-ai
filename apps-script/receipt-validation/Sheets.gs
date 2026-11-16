/** Review-only Sheets authority receiver. No installer or resource creation.
 * Load Code.gs (pure decoder/snapshot helpers) and this file for the new path.
 * Connection.gs routes the existing handler and Configure/Start/Stop names.
 * All data writes are single-attempt Sheets RAW values. No Drive journal IO.
 */
const RECEIPT_SHEETS_HEADERS = ['request_id','receipt_id','source_id','source_version','source_sha256',
  'snapshot_json','snapshot_digest','status','validation_result','updated_at'];
const RECEIPT_SHEETS_TAB = '_receipt_requests';
const RECEIPT_SHEETS_STOP = 'RECEIPT_SHEETS_STOPPED'; // Circuit breaker, never a request record.

function receiptSheetsCheckRow_(r,hash) {
  if(!Array.isArray(r) || r.length!==10 || !r.every(x=>typeof x==='string' && x.length<=40000) ||
     !/^[a-f0-9]{8}-[a-f0-9]{4}-4[a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}$/.test(r[0]) ||
     !/^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$/.test(r[9]))
    throw Error('receipt_sheets_row_invalid');
  const s=JSON.parse(r[5]);
  if(receiptCanonical_(s)!==r[5] || hash(r[5])!==r[6] || s.case_id!==r[1] ||
     !s.source || s.source.source_id!==r[2] || s.source.version!==r[3] || s.source.sha256!==r[4] ||
     !['受付済み','検証中','検証済み・未記帳','修正が必要','保留'].includes(r[7]))
    throw Error('receipt_sheets_row_mismatch');
  if(['受付済み','検証中'].includes(r[7])) {
    if(r[8]!=='') throw Error('receipt_sheets_result_invalid');
  } else {
    const result=JSON.parse(r[8]);
    if(result.request_id!==r[0] || result.snapshot_digest!==r[6] || result.state!==r[7] ||
       result.posting_authority!==false || result.financial_write_count!==0 || !Array.isArray(result.issues))
      throw Error('receipt_sheets_result_invalid');
  }
}

function receiptSheetsHash_(text) {
  return Utilities.computeDigest(Utilities.DigestAlgorithm.SHA_256,text,Utilities.Charset.UTF_8)
    .map(b=>('0'+((b+256)%256).toString(16)).slice(-2)).join('');
}

function receiptSheetsLocked_(work) {
  const lock=LockService.getScriptLock();
  if(!lock.tryLock(1000)) throw Error('receipt_sheets_busy');
  try {return work();} finally {lock.releaseLock();}
}

function receiptSheetsConfig_(props,allowDisabled=false) {
  const c=JSON.parse(props.getProperty('RECEIPT_VALIDATION_CONFIG_JSON') || 'null');
  if(!c || c.mode!=='sheets-authority-v1' || typeof c.enabled!=='boolean' ||
     !allowDisabled && c.enabled!==true || c.synthetic_only!==true ||
     c.repository!=='tmoriuchi1401-source/kakeibo-ai' || c.workflow!=='category-sheet-request.yml' ||
     !/^review\/receipt-sheets-[A-Za-z0-9/_-]+$/.test(c.ref) ||
     !['spreadsheet_id','folder_id'].every(k=>/^[A-Za-z0-9_-]{10,150}$/.test(c[k])) ||
     c.spreadsheet_id===c.folder_id || !c.owner_email || !c.service_email || c.owner_email===c.service_email ||
     !c.cases || !Object.keys(c.cases).length || JSON.stringify(c).length>9000 ||
     Session.getEffectiveUser().getEmail()!==c.owner_email)
    throw Error('receipt_sheets_configuration_invalid');
  for(const [cid, item] of Object.entries(c.cases)) {
    const entry=JSON.parse(item.context_json), expected=JSON.parse(item.expected_json);
    if(expected.case_id!==cid || receiptCanonical_(expected.source)!==receiptCanonical_(entry.anchor.source) ||
       expected.context_digest!==receiptSheetsHash_(receiptCanonical_(entry.anchor)) ||
       receiptCanonical_(expected.machine)!==receiptCanonical_(entry.decoder.machine) ||
       receiptCanonical_(expected.categories)!==receiptCanonical_(entry.categories))
      throw Error('receipt_sheets_context_invalid');
  }
  return c;
}

function receiptSheetsConfigure() {
  return receiptSheetsLocked_(()=>{
    // Explicit invocation only after approval. Reuse an existing token; no setup IO.
    const staged=PropertiesService.getScriptProperties(), props=PropertiesService.getUserProperties();
    const c=receiptSheetsConfig_(staged,true);
    if(c.enabled) throw Error('receipt_sheets_stage_disabled_configuration');
    const previous=JSON.parse(props.getProperty('RECEIPT_VALIDATION_CONFIG_JSON') || 'null');
    // Never overwrite an old journal trial's binding, even when it is disabled.
    if(previous && (previous.mode!=='sheets-authority-v1' || previous.enabled ||
       receiptCanonical_(previous)!==receiptCanonical_(c)))
      throw Error('receipt_sheets_existing_binding_conflict');
    const token=props.getProperty('RECEIPT_GITHUB_TOKEN') || staged.getProperty('CATEGORY_GITHUB_TOKEN') ||
      props.getProperty('CATEGORY_GITHUB_TOKEN');
    if(!token || !token.startsWith('github_pat_')) throw Error('receipt_sheets_token_missing');
    props.setProperty('RECEIPT_VALIDATION_CONFIG_JSON',JSON.stringify(c));
    props.setProperty('RECEIPT_GITHUB_TOKEN',token);
    staged.deleteProperty('RECEIPT_VALIDATION_CONFIG_JSON');
    staged.deleteProperty('CATEGORY_GITHUB_TOKEN');
  });
}

function receiptSheetsStart() {
  return receiptSheetsLocked_(()=>{
    const props=PropertiesService.getUserProperties(), c=receiptSheetsConfig_(props,true);
    if(c.enabled || props.getProperty(RECEIPT_SHEETS_STOP) || props.getProperty('RECEIPT_VALIDATION_STOPPED') ||
       !props.getProperty('RECEIPT_GITHUB_TOKEN')) throw Error('receipt_sheets_start_not_ready');
    const triggers=ScriptApp.getProjectTriggers();
    if(triggers.length!==1 || triggers[0].getHandlerFunction()!=='receiptConnectionEdited' ||
       triggers[0].getTriggerSourceId()!==c.spreadsheet_id ||
       triggers[0].getEventType()!==ScriptApp.EventType.ON_EDIT)
      throw Error('receipt_sheets_existing_trigger_required');
    const rows=receiptSheetsPorts_(c,props).read(), seen=new Set();
    if(!rows.length || rows.length>1000 || receiptCanonical_(rows[0])!==receiptCanonical_(RECEIPT_SHEETS_HEADERS))
      throw Error('receipt_sheets_table_invalid');
    for(const row of rows.slice(1)) {
      receiptSheetsCheckRow_(row,receiptSheetsHash_);
      if(seen.has(row[0])) throw Error('receipt_sheets_row_missing_or_duplicate');
      seen.add(row[0]);
    }
    c.enabled=true;props.setProperty('RECEIPT_VALIDATION_CONFIG_JSON',JSON.stringify(c));
    // No trigger creation, Google data write or GitHub flag change.
  });
}

function receiptSheetsStop() {
  return receiptSheetsLocked_(()=>{
    const props=PropertiesService.getUserProperties(), c=receiptSheetsConfig_(props,true);
    c.enabled=false;props.setProperty('RECEIPT_VALIDATION_CONFIG_JSON',JSON.stringify(c));
    props.deleteProperty('RECEIPT_GITHUB_TOKEN'); // Only the trial copy, never the source PAT.
    // Existing trigger, request rows and uncertainty latch remain untouched.
  });
}

function receiptSheetsPorts_(c,props) {
  const root='https://sheets.googleapis.com/v4/spreadsheets/'+c.spreadsheet_id+'/values/';
  function fetch_(url, options) {
    const response=UrlFetchApp.fetch(url,{...options,followRedirects:false,muteHttpExceptions:true,
      headers:{Authorization:'Bearer '+ScriptApp.getOAuthToken()}});
    if(response.getResponseCode()!==200) throw Error('receipt_sheets_io_unknown');
    return JSON.parse(response.getContentText());
  }
  return {
    hash:receiptSheetsHash_,
    read:()=>fetch_(root+encodeURIComponent("'_receipt_requests'!A1:J1001")+'?valueRenderOption=UNFORMATTED_VALUE',
      {method:'get'}).values || [],
    // Only the receiver can create a new row, always below all occupied rows.
    // Never insert, delete, sort, move, overwrite an existing row or append a result cache.
    insert:(number,row)=>fetch_(root+encodeURIComponent("'_receipt_requests'!A"+number+':J'+number)+'?valueInputOption=RAW',
      {method:'put',contentType:'application/json',payload:JSON.stringify({values:[row]})}),
    caseForRequest:rid=>{
      const ss=SpreadsheetApp.openById(c.spreadsheet_id), sheet=ss.getSheetByName('確認');
      if(!sheet || sheet.getLastRow()>1000) throw Error('receipt_sheets_ui_invalid');
      const rows=sheet.getRange(2,1,Math.max(1,sheet.getLastRow()-1),46).getValues();
      const found=rows.filter(r=>r[8]==='register:'+rid);
      if(found.length!==1 || rows.filter(r=>r[0]===found[0][0]).length!==1 || !c.cases[found[0][0]])
        throw Error('receipt_sheets_case_missing_or_duplicate');
      const item=c.cases[found[0][0]], expected=JSON.parse(item.expected_json);
      return {candidate:receiptConnectionDecodeRow_(found[0],expected,receiptSheetsHash_), context_json:item.context_json};
    },
    dispatch:rid=>{
      const token=props.getProperty('RECEIPT_GITHUB_TOKEN');
      if(!token || !token.startsWith('github_pat_')) throw Error('receipt_sheets_token_missing');
      return UrlFetchApp.fetch('https://api.github.com/repos/'+c.repository+'/actions/workflows/'+c.workflow+'/dispatches',
        {method:'post',contentType:'application/json',followRedirects:false,muteHttpExceptions:true,
         headers:{Authorization:'Bearer '+token,Accept:'application/vnd.github+json','X-GitHub-Api-Version':'2022-11-28'},
         payload:JSON.stringify({ref:c.ref,inputs:{request_id:rid}})}).getResponseCode();
    }
  };
}

function receiptSheetsRegister_(e,ports,props) {
  if(e.value==='FALSE' || String(e.value).startsWith('ready:')) return null;
  const m=/^register:([a-f0-9]{8}-[a-f0-9]{4}-4[a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12})$/.exec(e.value);
  if(!m || e.oldValue!=='ready:'+m[1]) throw Error('receipt_sheets_event_invalid');
  if(props.getProperty(RECEIPT_SHEETS_STOP)) throw Error('receipt_sheets_stopped');
  const rid=m[1], same=(a,b)=>receiptCanonical_(a)===receiptCanonical_(b);
  const table=ports.read();
  if(!Array.isArray(table) || !table.length || table.length>=1000 || !same(table[0],RECEIPT_SHEETS_HEADERS))
    throw Error('receipt_sheets_table_invalid');
  const seen=new Set();
  for(const r of table.slice(1)) {
    receiptSheetsCheckRow_(r,ports.hash);
    if(seen.has(r[0]))
      throw Error('receipt_sheets_row_missing_or_duplicate');
    seen.add(r[0]);
  }
  if(seen.has(rid)) throw Error('receipt_sheets_replay_rejected');
  const capture=ports.caseForRequest(rid), candidate=capture.candidate;
  const draft=receiptSnapshot_(candidate,ports.hash);
  const snapshot={case_id:draft.case_id,source:draft.source,operations:draft.operations,context_json:capture.context_json};
  const source=snapshot.source, text=receiptCanonical_(snapshot), hash=ports.hash(text);
  if(text.length>40000) throw Error('receipt_sheets_snapshot_capacity');
  // The product assumption excludes registering the same receipt twice.
  // An already accepted receipt is still rejected rather than silently reused.
  if(table.slice(1).some(r=>r[1]===snapshot.case_id || r[2]===source.source_id))
    throw Error('receipt_sheets_receipt_already_registered');
  const row=[rid,snapshot.case_id,source.source_id,source.version,source.sha256,text,hash,'受付済み','',new Date().toISOString()];
  // Freeze two reads of the current editable case before the single insert.
  // UI edits AFTER this capture are drafts; the worker never reads them.
  if(!same(capture,ports.caseForRequest(rid)) || !same(table,ports.read()))
    throw Error('receipt_sheets_capture_changed');
  props.setProperty(RECEIPT_SHEETS_STOP,'outcome_unconfirmed');
  try {
    ports.insert(table.length+1,row);
    const actual=ports.read(), matching=actual.slice(1).filter(r=>r[0]===rid);
    if(actual.length!==table.length+1 || matching.length!==1 || !same(matching[0],row) ||
       !same(actual[actual.length-1],row)) throw Error('receipt_sheets_readback_unknown');
    if(ports.dispatch(rid)!==204) throw Error('receipt_sheets_dispatch_unknown');
    props.deleteProperty(RECEIPT_SHEETS_STOP);
    return {request_id:rid,state:'受付済み'};
  } catch (_) {
    // Retain the stop on timeout, process death or non-204. Never repair/resend.
    return {request_id:rid,state:'結果不明・照合待ち'};
  }
}

function receiptSheetsEdited(e) {
  const props=PropertiesService.getUserProperties(), c=receiptSheetsConfig_(props);
  if(!e || !e.range || e.range.getSheet().getParent().getId()!==c.spreadsheet_id ||
     e.range.getSheet().getName()!=='確認' || e.range.getRow()<2 ||
     e.range.getColumn()!==9 || e.range.getNumRows()!==1 || e.range.getNumColumns()!==1) return null;
  return receiptSheetsLocked_(()=>{
    // Stop may have run after the scope check but before acquiring this lock.
    const active=receiptSheetsConfig_(props);
    if(receiptCanonical_(active)!==receiptCanonical_(c)) throw Error('receipt_sheets_configuration_changed');
    return receiptSheetsRegister_(e,receiptSheetsPorts_(active,props),props);
  });
}
