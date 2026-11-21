/** PR-B: same one-tab ordinary-cell UI, immutable catalog and request ledger.
 * Load with Code.gs and Sheets.gs. No web app, financial writer, AI or Drive write.
 * Backend uses the Apps Script execution API with approved owner OAuth; no SA key.
 * All cooperating writers use this script lock, including backend claim/finish.
 */
const REVIEW_CASE_HEADERS=['case_id','source_id','source_version','source_sha256','case_json','case_digest','request_id'];
const REVIEW_CASE_TAB='_receipt_cases';
const REVIEW_WIDTH=98, REVIEW_MACHINE=81, REVIEW_LIMIT=8;
const REVIEW_STOP='RECEIPT_REVIEW_STOPPED';

function receiptReviewConfig_() {
  const c=JSON.parse(PropertiesService.getScriptProperties().getProperty('RECEIPT_REVIEW_CONFIG_JSON') || 'null');
  if(!c || Object.keys(c).sort().join(',')!=='enabled,folder_id,owner_email,repository,script_id,spreadsheet_id' ||
     typeof c.enabled!=='boolean' || !c.owner_email || Session.getEffectiveUser().getEmail()!==c.owner_email ||
     c.script_id!==ScriptApp.getScriptId() || c.repository!=='tmoriuchi1401-source/kakeibo-ai' ||
     ![c.spreadsheet_id,c.folder_id].every(x=>typeof x==='string' && /^[A-Za-z0-9_-]{10,150}$/.test(x)) ||
     c.spreadsheet_id===c.folder_id) throw Error('review_configuration_invalid');
  return c;
}
function receiptReviewEnabled_(c) {
  if(!c.enabled || PropertiesService.getScriptProperties().getProperty(REVIEW_STOP)) throw Error('review_disabled_or_unknown');
}
function receiptReviewStart() {
  return receiptSheetsLocked_(()=>{
    const props=PropertiesService.getScriptProperties(),c=receiptReviewConfig_();
    if(c.enabled || props.getProperty(REVIEW_STOP))throw Error('review_start_not_ready');
    const triggers=ScriptApp.getProjectTriggers();
    if(triggers.length!==1 || triggers[0].getHandlerFunction()!=='receiptReviewEdited' ||
       triggers[0].getTriggerSourceId()!==c.spreadsheet_id || triggers[0].getEventType()!==ScriptApp.EventType.ON_EDIT)
      throw Error('review_existing_trigger_required');
    const ports=receiptReviewPorts_(c),catalog=receiptReviewCatalog_(ports);
    receiptReviewRequests_(ports,catalog.entries);
    receiptSheetsAcceptance_(c,props);
    c.enabled=true;props.setProperty('RECEIPT_REVIEW_CONFIG_JSON',JSON.stringify(c));
    return {state:'受付ON・金融write OFF'};
  });
}
function receiptReviewStop() {
  return receiptSheetsLocked_(()=>{
    const props=PropertiesService.getScriptProperties(),c=receiptReviewConfig_();
    c.enabled=false;props.setProperty('RECEIPT_REVIEW_CONFIG_JSON',JSON.stringify(c));
    props.deleteProperty('RECEIPT_GITHUB_TOKEN');
    return {state:'受付OFF・金融write OFF'}; // Keep requests, drafts and uncertainty latch.
  });
}
function receiptReviewSource_(s) {
  receiptSheetsSource_(s);
  if(!/^[1-9][0-9]*$/.test(s.version || '')) throw Error('source_version_required');
}
function receiptReviewCase_(e) {
  if(!e || Object.keys(e).sort().join(',')!==
      'baseline_digest,candidate_rows,case_id,categories,import_row,request_id,rules,schema,slots,source' ||
     e.schema!=='receipt-review-b1' || e.case_id!=='receipt:'+e.source.source_id ||
     !/^[a-f0-9]{8}-[a-f0-9]{4}-4[a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}$/.test(e.request_id) ||
     !Array.isArray(e.slots) || e.slots.length!==8 || new Set(e.slots).size!==8 ||
     !Array.isArray(e.candidate_rows) || !e.candidate_rows.length || e.candidate_rows.length>100 ||
     !Array.isArray(e.categories) || !e.categories.length ||
     e.categories.some(x=>!Array.isArray(x) || x.length!==2 || x.some(v=>typeof v!=='string' || !v)) ||
     !Array.isArray(e.rules) || e.rules.some(x=>!Array.isArray(x) || x.length!==18) ||
     !Array.isArray(e.import_row) || e.import_row.length!==12 || e.import_row[0]!==e.case_id ||
     e.import_row[2]!=='receipt' || e.import_row[3]!==e.source.source_id ||
     !Number.isSafeInteger(e.import_row[6]) || e.import_row[6]<=0 || e.import_row[6]>99999999 ||
     !/^[a-f0-9]{64}$/.test(e.import_row[10])) throw Error('case_evidence_invalid');
  receiptReviewSource_(e.source);
  const original={...e};delete original.baseline_digest;delete original.request_id;
  if(receiptSheetsHash_(receiptCanonical_(original))!==e.baseline_digest) throw Error('case_binding_invalid');
  e.candidate_rows.forEach((r,i)=>{
    if(r.length!==11 || r[0]!==e.case_id+':'+String(i+1).padStart(3,'0') || r[1]!==e.case_id ||
       r[2]!==e.import_row[10] || r[3]!==i+1 || r[10]!==e.import_row[6] ||
       !Number.isSafeInteger(r[6]) || !r[4] || typeof r[5]!=='number') throw Error('case_candidate_mismatch');
  });
  const slots=e.candidate_rows.slice(0,8).map(r=>r[0]);
  for(let i=slots.length;i<8;i++)slots.push('new:'+receiptSheetsHash_(receiptCanonical_([e.case_id,i])).slice(0,24));
  if(receiptCanonical_(slots)!==receiptCanonical_(e.slots))throw Error('case_slot_mismatch');
  return e;
}
function receiptReviewMachine_(e) {
  return [e.case_id,e.source.source_id,e.source.version,e.source.sha256,...e.slots,e.request_id,'1',e.baseline_digest];
}
function receiptReviewOriginals_(e) {
  return e.candidate_rows.map(r=>({item_id:r[0],name:r[4],quantity:r[5],amount:r[6],category:[r[7],r[8]],
    kind:r[6]<0?'discount':/外税|消費税|税額/.test(r[4])?'tax':'product',target_item_id:''}));
}
function receiptReviewSnapshot_(row,e) {
  receiptReviewCase_(e);
  const same=(a,b)=>receiptCanonical_(a)===receiptCanonical_(b);
  if(row.length!==98 || row[0]!==e.case_id || row[1]!=='一般' ||
     row[2]!=='https://drive.google.com/file/d/'+e.source.source_id+'/view' || row[3]!==e.import_row[6] ||
     !same(row.slice(81,96),receiptReviewMachine_(e)) || row[8]!=='register:'+e.request_id ||
     typeof row[7]!=='string' || row[7].length>2000) throw Error('row_binding_invalid');
  const originals=receiptReviewOriginals_(e),items=[];
  if(originals.length>8)throw Error('capacity_exceeded');
  const binding=receiptSheetsHash_(receiptCanonical_([e.case_id,e.source]));
  for(let i=0;i<8;i++) {
    let [name,quantity,amount,major,minor,kind,target,mode,confirmed]=row.slice(9+9*i,18+9*i);
    const key=e.slots[i],old=originals.find(x=>x.item_id===key),blank=v=>v==='' || v===null;
    if(!old && blank(name) && blank(quantity) && blank(amount)) {
      if(!blank(major) || !blank(minor) || ![null,'','商品'].includes(kind) ||
         ![null,'','指定なし'].includes(target) || ![null,'','採用'].includes(mode) ||
         ![null,'',false].includes(confirmed))throw Error('empty_addition_invalid');
      name='';quantity='';amount='';major='';minor='';kind='商品';target='';mode='採用';confirmed=false;
    } else if(confirmed!==true)throw Error('original_check_required');
    if(!['商品','外税','値引き'].includes(kind) || ![null,'','指定なし',...Array.from({length:8},(_,j)=>'商品'+(j+1))].includes(target))
      throw Error('item_controls_invalid');
    if(name)quantity=receiptQuantity_(quantity);
    items.push({_item_id:key,_binding:binding,name,quantity,amount,category:major || minor?[major,minor]:null,
      kind:{'商品':'product','外税':'tax','値引き':'discount'}[kind],
      target:target && target.startsWith('商品')?Number(target.slice(2)):'',mode,confirmed});
  }
  const total=items.filter(x=>x.name && x.mode==='採用').reduce((s,x)=>s+x.amount,0);
  if(!Number.isSafeInteger(total) || row[4]!==total || row[5]!==row[3]-total)throw Error('display_total_mismatch');
  if(!['税込','税別','不明'].includes(row[96]) || typeof row[97]!=='boolean')throw Error('total_controls_invalid');
  const draft={case_id:e.case_id,source:e.source,items};
  const categories=e.categories.map(x=>x.slice());
  for(const item of items)if(item.category && item.category.every(x=>typeof x==='string' && x) &&
    !categories.some(x=>same(x,item.category)))categories.push(item.category);
  const converted=receiptSnapshot_({draft,original_items:originals,categories,context_digest:e.baseline_digest},receiptSheetsHash_);
  for(const op of converted.operations)if(op.action==='edit' && 'name' in op.changes)
    op.changes.category=items.find(i=>i._item_id===op.item_id).category;
  const inputs={items,receipt_total:row[3],tax_mode:row[96],total_confirmed:row[97],note:row[7]};
  return {schema:e.schema,case_id:e.case_id,source:e.source,baseline_digest:e.baseline_digest,
    input_version:receiptSheetsHash_(receiptCanonical_(inputs)),inputs,operations:converted.operations};
}

function receiptReviewPorts_(c) {
  const ss=SpreadsheetApp.openById(c.spreadsheet_id);
  function read(name,width,headers) {
    const sheet=ss.getSheetByName(name);
    if(!sheet || sheet.getLastRow()>1000)throw Error('review_table_invalid');
    const rows=sheet.getRange(1,1,Math.max(1,sheet.getLastRow()),width).getValues();
    if(headers && receiptCanonical_(rows[0])!==receiptCanonical_(headers))throw Error('review_header_invalid');
    return rows;
  }
  return {
    readCases:()=>read(REVIEW_CASE_TAB,7,REVIEW_CASE_HEADERS),
    readRequests:()=>read(RECEIPT_SHEETS_TAB,10,RECEIPT_SHEETS_HEADERS),
    readUI:()=>read('確認',98).slice(1),
    decorate:(number,e)=>receiptReviewDecorate_(ss,number,e),
    // One batch, one attempt. stringValue prevents names/notes becoming formulas.
    write:changes=>{
      const requests=changes.map(x=>{
        const sheet=ss.getSheetByName(x.tab);
        if(!sheet || x.row<2 || x.row>1000)throw Error('review_range_invalid');
        return {updateCells:{range:{sheetId:sheet.getSheetId(),startRowIndex:x.row-1,endRowIndex:x.row,
          startColumnIndex:x.column || 0,endColumnIndex:(x.column || 0)+x.values.length},
          rows:[{values:x.values.map(v=>({userEnteredValue:typeof v==='boolean'?{boolValue:v}:
            typeof v==='number'?{numberValue:v}:{stringValue:String(v)}}))}],fields:'userEnteredValue'}};
      });
      const reply=UrlFetchApp.fetch('https://sheets.googleapis.com/v4/spreadsheets/'+c.spreadsheet_id+':batchUpdate',
        {method:'post',contentType:'application/json',followRedirects:false,muteHttpExceptions:true,
         headers:{Authorization:'Bearer '+ScriptApp.getOAuthToken()},payload:JSON.stringify({requests})});
      if(reply.getResponseCode()!==200)throw Error('review_write_unknown');
    }
  };
}
function receiptReviewCatalog_(ports) {
  const rows=ports.readCases(),seen=[new Set(),new Set(),new Set(),new Set()],entries=[];
  for(const r of rows.slice(1)) {
    const e=receiptReviewCase_(JSON.parse(r[4]));
    if(receiptCanonical_(e)!==r[4] || receiptSheetsHash_(r[4])!==r[5] ||
       receiptCanonical_(r.slice(0,4))!==receiptCanonical_([e.case_id,e.source.source_id,e.source.version,e.source.sha256]) ||
       r[6]!==e.request_id)throw Error('catalog_binding_invalid');
    [r[0],r[1],r[3],r[6]].forEach((key,i)=>{if(seen[i].has(key))throw Error('catalog_duplicate');seen[i].add(key);});
    entries.push(e);
  }
  return {rows,entries};
}
function receiptReviewRequests_(ports,entries) {
  const rows=ports.readRequests(),seen=[new Set(),new Set(),new Set(),new Set()];
  for(const r of rows.slice(1)) {
    receiptSheetsCheckRow_(r,receiptSheetsHash_);
    [r[0],r[1],r[2],r[4]].forEach((key,i)=>{if(seen[i].has(key))throw Error('request_duplicate');seen[i].add(key);});
    const e=entries.find(x=>x.request_id===r[0]),s=JSON.parse(r[5]);
    if(!e || s.case_id!==e.case_id || !s.inputs || s.baseline_digest!==e.baseline_digest ||
       receiptCanonical_(s.source)!==receiptCanonical_(e.source) ||
       receiptSheetsHash_(receiptCanonical_(s.inputs))!==s.input_version)throw Error('request_binding_invalid');
  }
  return rows;
}
function receiptReviewChange_(ports,changes,verify) {
  const props=PropertiesService.getScriptProperties();
  props.setProperty(REVIEW_STOP,'outcome_unconfirmed');
  // If *any* response is lost the durable stop stays set, including process death.
  ports.write(changes);
  verify();
  props.deleteProperty(REVIEW_STOP);
}

function receiptReviewRegister_(rid,ports) {
  const {entries}=receiptReviewCatalog_(ports),table=receiptReviewRequests_(ports,entries);
  const e=entries.find(x=>x.request_id===rid);
  if(!e || table.slice(1).some(r=>r[0]===rid || r[1]===e.case_id || r[2]===e.source.source_id || r[4]===e.source.sha256))
    throw Error('review_replay_or_duplicate');
  const capture=()=>{
    const rows=ports.readUI(),found=rows.filter(r=>r[8]==='register:'+rid);
    if(found.length!==1 || rows.filter(r=>r[0]===e.case_id).length!==1 || found[0][0]!==e.case_id)throw Error('review_ui_duplicate_or_mismatch');
    return receiptReviewSnapshot_(found[0],e);
  };
  const snap=capture(),text=receiptCanonical_(snap),hash=receiptSheetsHash_(text);
  if(text.length>40000 || table.length>=1000)throw Error('review_capacity');
  if(text!==receiptCanonical_(capture()) || receiptCanonical_(table)!==receiptCanonical_(ports.readRequests()))throw Error('review_capture_changed');
  // Local enabled was re-read under this lock by the caller. Existing remote gate.
  const c=receiptReviewConfig_();receiptReviewEnabled_(c);
  receiptSheetsAcceptance_(c,PropertiesService.getScriptProperties());
  const s=e.source,row=[rid,e.case_id,s.source_id,s.version,s.sha256,text,hash,'受付済み','',new Date().toISOString()];
  receiptReviewChange_(ports,[{tab:RECEIPT_SHEETS_TAB,row:table.length+1,values:row}],()=>{
    const actual=receiptReviewRequests_(ports,entries);
    if(receiptCanonical_(actual)!==receiptCanonical_([...table,row]))throw Error('review_readback_unknown');
  });
  return {request_id:rid,state:'受付済み'};
}

function receiptReviewEdited(e) {
  if(!e || !e.range || e.range.getSheet().getName()!=='確認' || e.range.getRow()<2 ||
     e.range.getColumn()!==9 || e.range.getNumRows()!==1 || e.range.getNumColumns()!==1)return null;
  const match=/^register:([a-f0-9-]{36})$/.exec(e.value);
  if(!match || e.oldValue!=='ready:'+match[1])return null;
  return receiptSheetsLocked_(()=>{
    const c=receiptReviewConfig_();receiptReviewEnabled_(c);
    if(e.range.getSheet().getParent().getId()!==c.spreadsheet_id)throw Error('review_book_mismatch');
    return receiptReviewRegister_(match[1],receiptReviewPorts_(c));
  });
}

/** Execution API entry, NOT doGet/doPost. Owner OAuth only; no HTTP endpoint. */
function receiptReviewBackend(command) {
  return receiptSheetsLocked_(()=>{
    const c=receiptReviewConfig_(),ports=receiptReviewPorts_(c);
    return receiptReviewCommand_(command,ports,c);
  });
}
function receiptReviewCommand_(command,ports,c) {
  const {rows,entries}=receiptReviewCatalog_(ports),table=receiptReviewRequests_(ports,entries);
  if(command.action==='lookup')return entries.find(e=>e.source.source_id===command.source_id || e.source.sha256===command.sha256) || null;
  if(command.action==='pending')return table.slice(1).filter(r=>r[7]==='受付済み').map(r=>r[0]);
  if(command.action==='read') {
    const matches=table.slice(1).filter(r=>r[0]===command.request_id);
    if(matches.length!==1)throw Error('review_request_missing');
    return {row:matches[0],entry:entries.find(e=>e.request_id===command.request_id)};
  }
  receiptReviewEnabled_(c);
  if(command.action==='intake') {
    const e=receiptReviewCase_(command.entry),row=command.row;
    if(entries.some(x=>x.case_id===e.case_id || x.request_id===e.request_id || x.source.source_id===e.source.source_id || x.source.sha256===e.source.sha256))
      throw Error('review_intake_duplicate');
    if(row.length!==98 || receiptCanonical_(row.slice(81,96))!==receiptCanonical_(receiptReviewMachine_(e)) ||
       row[0]!==e.case_id || row[1]!=='一般' || row[3]!==e.import_row[6] || row[8]!=='ready:'+e.request_id ||
       ports.readUI().some(r=>r[0]===e.case_id) || rows.length>=1000)throw Error('review_intake_binding_invalid');
    const text=receiptCanonical_(e);
    if(text.length>40000)throw Error('review_case_capacity');
    const stored=[e.case_id,e.source.source_id,e.source.version,e.source.sha256,text,receiptSheetsHash_(text),e.request_id];
    const uiBefore=ports.readUI();
    receiptReviewChange_(ports,[{tab:REVIEW_CASE_TAB,row:rows.length+1,values:stored},
      {tab:'確認',row:uiBefore.length+2,values:row}],()=>{
      if(receiptCanonical_(ports.readCases())!==receiptCanonical_([...rows,stored]) ||
         receiptCanonical_(ports.readUI())!==receiptCanonical_([...uiBefore,row]))throw Error('review_intake_readback_unknown');
    });
    // Decoration failures stop intake; no automatic rerender or overwrite of drafts.
    const props=PropertiesService.getScriptProperties();
    props.setProperty(REVIEW_STOP,'decoration_unconfirmed');
    ports.decorate(uiBefore.length+2,e);
    props.deleteProperty(REVIEW_STOP);
    return {case_id:e.case_id};
  }
  const index=table.findIndex(r=>r[0]===command.request_id),before=table[index];
  if(index<1 || receiptCanonical_(before)!==receiptCanonical_(command.before))throw Error('review_stale_request');
  let tail;
  if(command.action==='claim' && before[7]==='受付済み') {
    receiptSheetsAcceptance_(c,PropertiesService.getScriptProperties());
    tail=['検証中','',new Date().toISOString()];
  } else if(command.action==='finish' && before[7]==='検証中') {
    const result=command.result;
    if(!['検証済み・未記帳','修正が必要','保留'].includes(result.state) || result.request_id!==before[0] ||
       result.snapshot_digest!==before[6] || result.posting_authority!==false || result.financial_write_count!==0 ||
       !Array.isArray(result.issues) || result.issues.some(x=>typeof x!=='string' || !/^[a-z0-9_:.-]+$/.test(x)))
      throw Error('review_result_invalid');
    // Match the existing authority's ensure_ascii=False result format. G's
    // state binding compares Japanese text; snapshots remain ASCII canonical.
    const text=JSON.stringify(Object.fromEntries(Object.keys(result).sort().map(k=>[k,result[k]])));
    tail=[result.state,text,new Date().toISOString()];
  } else throw Error('review_replay_rejected');
  const after=[...before.slice(0,7),...tail];
  receiptSheetsCheckRow_(after,receiptSheetsHash_);
  receiptReviewChange_(ports,[{tab:RECEIPT_SHEETS_TAB,row:index+1,column:7,values:tail}],()=>{
    if(receiptCanonical_(ports.readRequests()[index])!==receiptCanonical_(after))throw Error('review_transition_unknown');
  });
  return after;
}
