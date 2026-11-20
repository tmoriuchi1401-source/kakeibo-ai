// Actual generated bundle; fake services only. stdin/stdout belong to pytest.
const fs=require('node:fs'),vm=require('node:vm'),{createHash}=require('node:crypto');
const input=JSON.parse(fs.readFileSync(0,'utf8')), clone=x=>JSON.parse(JSON.stringify(x));
const state={RECEIPT_VALIDATION_CONFIG_JSON:JSON.stringify(input.config),RECEIPT_GITHUB_TOKEN:'github_pat_DUMMY_CANARY',...input.properties};
const staged={CATEGORY_GITHUB_TOKEN:'github_pat_UNRELATED_CANARY'};
let propertyWrites=[],errors=[],logs=[],calls=[],dispatches=[],gateGets=0,writes=0,captures=0,locked=false,runs=0;
let rows=clone(input.table),ui=clone(input.ui),triggerMutations=0,attempts=0;
const fault=input.fault;
const props={getProperty:k=>Object.hasOwn(state,k)?state[k]:null,
  setProperty:(k,v)=>{
    propertyWrites.push({key:k,operation:'set'});
    if(k==='RECEIPT_SHEETS_STOPPED' && fault==='latch_before')throw Error('private canary');
    if(k==='RECEIPT_VALIDATION_CONFIG_JSON' && fault==='stop_save')throw Error('private canary');
    state[k]=v;
    if(k==='RECEIPT_SHEETS_STOPPED' && fault==='latch_after')throw Error('private canary');
  },deleteProperty:k=>{
    propertyWrites.push({key:k,operation:'delete'});
    if(k==='RECEIPT_GITHUB_TOKEN' && fault==='stop_pat')throw Error('private canary');
    delete state[k];
  }};
const stagedProps={getProperty:k=>staged[k] || null,deleteProperty:k=>delete staged[k]};
if(input.setup){
  delete state.RECEIPT_VALIDATION_CONFIG_JSON;delete state.RECEIPT_GITHUB_TOKEN;
  staged.RECEIPT_VALIDATION_CONFIG_JSON=JSON.stringify({...input.config,enabled:false});
  staged.RECEIPT_P1_GITHUB_TOKEN='github_pat_DUMMY_CANARY';
  if(input.setup==='old_binding')state.RECEIPT_VALIDATION_CONFIG_JSON=JSON.stringify({mode:'old-journal',enabled:false});
  if(input.setup==='latch')state.RECEIPT_SHEETS_STOPPED='outcome_unconfirmed';
}
const trigger={getHandlerFunction:()=> 'receiptConnectionEdited',getTriggerSourceId:()=>input.config.spreadsheet_id,getEventType:()=> 'ON_EDIT'};
const triggers=input.setup==='missing'?[]:input.setup==='duplicate'?[trigger,trigger]:[trigger];
const ss={getId:()=>input.range_sheet_id || input.config.spreadsheet_id,getSheetByName:n=>n==='確認'?sheet:null};
const sheet={getName:()=>input.range_tab || '確認',getParent:()=>ss,getLastRow:()=>ui.length+1,
  getRange:()=>({getValues:()=>{
    captures++;const result=clone(ui);
    if(fault==='capture' && captures%2===0)result[0][18]='途中変更';
    return result;
  }})};
const range={getSheet:()=>sheet,getRow:()=>2,getColumn:()=>9,getNumRows:()=>input.multi_cell?2:1,getNumColumns:()=>1};
const reply=(status,data,raw)=>({getResponseCode:()=>status,getContentText:()=>raw===undefined?JSON.stringify(data):raw});
const globals={PropertiesService:{getUserProperties:()=>props,getScriptProperties:()=>stagedProps},
  Logger:{log:x=>logs.push(x)},
  Session:{getEffectiveUser:()=>({getEmail:()=>input.owner_override || input.config.owner_email})},
  Utilities:{DigestAlgorithm:{SHA_256:'sha256'},Charset:{UTF_8:'utf8'},computeDigest:(_a,text)=>Array.from(createHash('sha256').update(text).digest())},
  SpreadsheetApp:{openById:fid=>{if(fid!==input.config.spreadsheet_id)throw Error('forbidden sheet');return ss;}},
  ScriptApp:{getScriptId:()=>input.script_id,getOAuthToken:()=> 'DUMMY',EventType:{ON_EDIT:'ON_EDIT'},getProjectTriggers:()=>triggers,
    newTrigger:()=>{triggerMutations++;throw Error('forbidden trigger');},deleteTrigger:()=>{triggerMutations++;throw Error('forbidden trigger');}},
  LockService:{getScriptLock:()=>({tryLock:()=>{
    if(locked)throw Error('nested lock');
    if(fault==='stop_busy')return false;
    if(fault==='stop_before_lock'){
      const c=JSON.parse(state.RECEIPT_VALIDATION_CONFIG_JSON);c.enabled=false;
      state.RECEIPT_VALIDATION_CONFIG_JSON=JSON.stringify(c);delete state.RECEIPT_GITHUB_TOKEN;
    }
    locked=true;return true;
  },releaseLock:()=>{locked=false;if(fault==='unlock')throw Error('private canary');}})},
  UrlFetchApp:{fetch:(url,options)=>{
    calls.push({url,method:options.method});
    if(options.followRedirects!==false || options.muteHttpExceptions!==true)throw Error('unsafe fetch');
    const gh='https://api.github.com/repos/'+input.config.repository;
    if(url===gh+'/actions/variables/RECEIPT_VALIDATION_ENABLED' && options.method==='get'){
      gateGets++;
      if(fault==='gate_throw')throw Error('private token canary');
      return reply(input.gate_status ?? 200,{name:input.gate_name ?? 'RECEIPT_VALIDATION_ENABLED',value:input.gate_value ?? 'true'},input.gate_raw);
    }
    if(url===gh+'/actions/workflows/category-sheet-request.yml/dispatches' && options.method==='post'){
      dispatches.push(JSON.parse(options.payload));
      if(fault==='dispatch_before')throw Error('private canary');
      runs++;
      if(fault==='dispatch_after')throw Error('private canary');
      return reply(fault==='dispatch_status'?500:204,{});
    }
    const root='https://sheets.googleapis.com/v4/spreadsheets/'+input.config.spreadsheet_id+'/values/';
    if(!url.startsWith(root) || !decodeURIComponent(url).includes("'_receipt_requests'!"))throw Error('forbidden endpoint');
    if(options.method==='get'){
      if(writes && fault==='readback_throw')throw Error('private canary');
      return reply(200,{values:clone(rows)});
    }
    if(options.method!=='put')throw Error('forbidden mutation');
    writes++;
    if(fault==='insert_before')throw Error('private canary');
    const m=/!A(\d+):J(\d+)\?/.exec(decodeURIComponent(url));
    if(!m || Number(m[1])!==rows.length+1 || m[1]!==m[2])throw Error('overwrite existing row');
    rows.push(JSON.parse(options.payload).values[0]);
    if(fault==='insert_after')throw Error('private canary');
    if(fault==='readback')rows[rows.length-1][6]='a'.repeat(64);
    return reply(200,{});
  }}
};
const ctx=vm.createContext(globals);
for(const name of ['Code.gs','Profile.gs','Connection.gs','Sheets.gs','P1Audit.gs']){
  if(name==='Profile.gs' && input.missing_profile)continue;
  vm.runInContext(fs.readFileSync(input.bundle_dir+'/'+name,'utf8'),ctx);
}
const event={range,value:'register:'+input.request_id,oldValue:'ready:'+input.request_id,...input.event};
function invoke(name,...args){try{return ctx[name](...args);}catch(e){errors.push(e.message);return null;}}
let output=null,repeated=null,audit=null,ready=true;
if(input.setup){
  invoke('receiptConnectionConfigure');
  if(!errors.length)invoke('receiptConnectionStart');
  ready=!errors.length;
}
if(input.action==='stop'){
  invoke('receiptConnectionStop');
  audit=invoke('receiptConnectionAuditStateDisabled');
  invoke('receiptConnectionEdited',event);
}else if(input.action==='audit'){
  audit=invoke(input.config.enabled?'receiptConnectionAuditStateEnabled':'receiptConnectionAuditStateDisabled');
}else if(ready){
  attempts++;output=invoke('receiptConnectionEdited',event);
  if(input.repeat){attempts++;repeated=invoke('receiptConnectionEdited',event);}
}
if(input.setup && ready){
  invoke('receiptConnectionStop');
  audit=invoke('receiptConnectionAuditStateDisabled');
  invoke('receiptConnectionEdited',event);
}
const config=JSON.parse(state.RECEIPT_VALIDATION_CONFIG_JSON || '{}');
process.stdout.write(JSON.stringify({rows,ui,writes,dispatches,calls,errors,output,repeated,gateGets,runs,
  stopped:Object.hasOwn(state,'RECEIPT_SHEETS_STOPPED'),propertyWrites,logs,audit,attempts,
  enabled:config.enabled,token_copy:!!state.RECEIPT_GITHUB_TOKEN,triggerMutations,
  unrelated_token_unchanged:staged.CATEGORY_GITHUB_TOKEN==='github_pat_UNRELATED_CANARY',
  config_json:state.RECEIPT_VALIDATION_CONFIG_JSON}));
