// Synthetic Google services only. No network, token file or real resource.
const fs=require('node:fs'), vm=require('node:vm'), {createHash}=require('node:crypto');
const input=JSON.parse(fs.readFileSync(0,'utf8'));
const clone=x=>JSON.parse(JSON.stringify(x)), c=input.config;
let rawEnvelope=input.envelope_json, envelope=JSON.parse(rawEnvelope), generation=0, attempts=0,
  dispatched=[], history=[clone(envelope)], rule=null;
const rows=[clone(input.row)], errors=[], state={};
const props={getProperty:k=>state[k] || null,setProperty:(k,v)=>{state[k]=v;},deleteProperty:k=>{delete state[k];}};
const staged={}, stagedProps={getProperty:k=>staged[k] || null,deleteProperty:k=>{delete staged[k];}};
if(input.setup) {staged.RECEIPT_VALIDATION_CONFIG_JSON=JSON.stringify({...c,enabled:false});staged.CATEGORY_GITHUB_TOKEN='github_pat_OFFLINE_NOT_A_CREDENTIAL';}
else {state.RECEIPT_VALIDATION_CONFIG_JSON=JSON.stringify(c);state.RECEIPT_GITHUB_TOKEN='github_pat_OFFLINE_NOT_A_CREDENTIAL';}
const triggers=[];
const ss={getId:()=>c.spreadsheet_id,getSheetByName:name=>name==='確認'?sheet:null};
const sheet={getParent:()=>ss,getName:()=> '確認',getLastRow:()=>rows.length+1,
  getRange:(row,col,nr,nc)=>({getValues:()=>clone(rows),
    setDataValidation:function(value){rule=value;return this;},setValue:function(value){rows[row-2][col-1]=value;return this;}})};
const range=col=>({getSheet:()=>sheet,getColumn:()=>col,getNumColumns:()=>1,getNumRows:()=>1});
const reply=(status,value)=>({getResponseCode:()=>status,getContentText:()=>typeof value==='string'?value:JSON.stringify(value)});
function metadata(id){return {id,mimeType:id===c.folder_id?'application/vnd.google-apps.folder':'application/json',
  version:String(generation+1),etag:'"'+generation+'"',parents:[{id:c.folder_id}],labels:{trashed:false},
  owners:[{emailAddress:c.owner_email}],permissions:[{type:'user',role:'owner',emailAddress:c.owner_email},
    {type:'user',role:'writer',emailAddress:c.service_email}]};}
const globals={PropertiesService:{getUserProperties:()=>props,getScriptProperties:()=>stagedProps},Session:{getEffectiveUser:()=>({getEmail:()=>c.owner_email})},
  Utilities:{DigestAlgorithm:{SHA_256:'sha256'},Charset:{UTF_8:'utf8'},getUuid:()=>input.request_id,
    computeDigest:(_a,text)=>Array.from(createHash('sha256').update(text).digest())},
  ScriptApp:{getOAuthToken:()=> 'OFFLINE',getProjectTriggers:()=>triggers,
    newTrigger:handler=>({forSpreadsheet:id=>({onEdit:()=>({create:()=>{
      if(handler!=='receiptConnectionEdited' || id!==c.spreadsheet_id)throw Error('wrong trigger target');
      const t={getHandlerFunction:()=>handler,getTriggerSourceId:()=>id};triggers.push(t);return t;}})})}),
    deleteTrigger:t=>{const i=triggers.indexOf(t);if(i<0)throw Error('wrong trigger');triggers.splice(i,1);}},
  SpreadsheetApp:{openById:id=>{if(id!==c.spreadsheet_id)throw Error('wrong book');return ss;},
    flush:()=>{},newDataValidation:()=>({requireCheckbox:(checkedValue,uncheckedValue)=>({build:()=>({checkedValue,uncheckedValue})})})},
  LockService:{getScriptLock:()=>({tryLock:()=>true,releaseLock:()=>{}})},
  UrlFetchApp:{fetch:(url,options)=>{
    if(options.followRedirects!==false || options.muteHttpExceptions!==true)throw Error('unsafe fetch');
    if(url.startsWith('https://api.github.com/')) {dispatched.push(JSON.parse(options.payload));return reply(204,'');}
    if(options.method==='put') {
      attempts++;
      if(input.fault==='before')throw Error('synthetic lost PUT response before commit');
      if(options.headers['If-Match']!=='"'+generation+'"')return reply(412,'');
      rawEnvelope=options.payload;envelope=JSON.parse(rawEnvelope);generation++;history.push(clone(envelope));
      if(input.fault==='after')throw Error('synthetic lost PUT response after commit');
      return reply(200,{});
    }
    if(url.includes('?alt=media'))return reply(200,rawEnvelope);
    const id=url.split('/files/')[1].split('?')[0];return reply(200,metadata(id));
  }}
};
const ctx=vm.createContext(globals);
vm.runInContext(fs.readFileSync(__dirname+'/Code.gs','utf8'),ctx);
vm.runInContext(fs.readFileSync(__dirname+'/Connection.gs','utf8'),ctx);
function call(e){try{return ctx.receiptConnectionEdited(e);}catch(error){errors.push(error.message);return null;}}
if(input.setup) {ctx.receiptConnectionConfigure();ctx.receiptConnectionStart();}
call({range:range(27)}); // The ordinary product original-check edit prepares.
if(rule) {
  rows[0][8]=rule.checkedValue;
  call({range:range(9),value:rule.checkedValue,oldValue:rule.uncheckedValue});
  if(input.duplicate) {
    call({range:range(9),value:rule.uncheckedValue,oldValue:rule.checkedValue});
    call({range:range(9),value:rule.checkedValue,oldValue:rule.uncheckedValue});
  }
} else if(input.fault) {
  // A new trigger invocation must observe the persisted stop, never resend.
  call({range:range(27)});
}
let setup=null;
if(input.setup) {ctx.receiptConnectionStop();setup={trigger_count:triggers.length,
  enabled:JSON.parse(state.RECEIPT_VALIDATION_CONFIG_JSON).enabled,
  staged_token:!!staged.CATEGORY_GITHUB_TOKEN,user_token:!!state.RECEIPT_GITHUB_TOKEN};}
process.stdout.write(JSON.stringify({envelope,raw_envelope:rawEnvelope,generation,attempts,dispatched,history,rows,errors,
  stopped:!!state.RECEIPT_VALIDATION_STOPPED,rule,setup}));
