// Execute the real PR-B Apps Script with fake storage/services. No network.
const fs=require('node:fs'),vm=require('node:vm'),{createHash}=require('node:crypto');
const input=JSON.parse(fs.readFileSync(0,'utf8')),clone=x=>JSON.parse(JSON.stringify(x));
const config={enabled:true,folder_id:'synthetic-folder-b',owner_email:'owner@synthetic.invalid',
  repository:'tmoriuchi1401-source/kakeibo-ai',script_id:'synthetic-script-b',spreadsheet_id:'synthetic-sheet-b',...input.config};
const properties={RECEIPT_REVIEW_CONFIG_JSON:JSON.stringify(config),RECEIPT_GITHUB_TOKEN:'github_pat_DUMMY',...input.properties};
if(input.config)properties.RECEIPT_REVIEW_CONFIG_JSON=JSON.stringify(config);
const props={getProperty:k=>properties[k]??null,setProperty:(k,v)=>{properties[k]=v;},deleteProperty:k=>{delete properties[k];}};
let locked=false,writes=0,gateGets=0,captures=0,decorations=0,validationCount=0;
const formulas=[],batches=[];
let cases=clone(input.cases || [['case_id','source_id','source_version','source_sha256','case_json','case_digest','request_id']]);
let requests=clone(input.requests || [['request_id','receipt_id','source_id','source_version','source_sha256','snapshot_json','snapshot_digest','status','validation_result','updated_at']]);
let ui=clone(input.ui || []);
function put(changes) {
  writes++;
  if(input.fault==='before')throw Error('lost before write');
  for(const change of changes){
    const table=change.tab==='_receipt_cases'?cases:change.tab==='_receipt_requests'?requests:ui;
    const offset=change.tab==='確認'?2:1,index=change.row-offset;
    if(index>table.length)throw Error('gap');
    if(index===table.length)table.push([]);
    table[index].splice(change.column||0,change.values.length,...clone(change.values));
  }
  if(input.fault==='after')throw Error('lost after write');
}
const names=['確認','_receipt_cases','_receipt_requests'];
function sheet(name) {
  const table=()=>name==='確認'?[Array(98).fill('header'),...ui]:name==='_receipt_cases'?cases:requests;
  return {getSheetId:()=>names.indexOf(name)+1,getLastRow:()=>table().length,
    getRange:(r,c,n=1,w=1)=>({getValues:()=>Array.from({length:n},(_,i)=>Array.from({length:w},(_,j)=>table()[r+i-1]?.[c+j-1]??'')),
      setDataValidation:()=>{validationCount++;},setFormulas:values=>formulas.push(...values[0])})};
}
const validation={requireValueInList(){return this;},setAllowInvalid(){return this;},requireCheckbox(){return this;},build(){return {};}};
const ctx=vm.createContext({PropertiesService:{getScriptProperties:()=>props},
  Session:{getEffectiveUser:()=>({getEmail:()=>input.owner_override || config.owner_email})},ScriptApp:{getScriptId:()=>config.script_id,
    getOAuthToken:()=> 'DUMMY',EventType:{ON_EDIT:'ON_EDIT'},getProjectTriggers:()=>input.no_trigger?[]:[{getHandlerFunction:()=> 'receiptReviewEdited',
      getTriggerSourceId:()=>config.spreadsheet_id,getEventType:()=> 'ON_EDIT'}]},
  Utilities:{DigestAlgorithm:{SHA_256:'sha256'},Charset:{UTF_8:'utf8'},computeDigest:(_a,text)=>Array.from(createHash('sha256').update(text).digest())},
  LockService:{getScriptLock:()=>({tryLock:()=>{if(locked || input.busy)return false;locked=true;return true;},releaseLock:()=>{locked=false;}})},
  SpreadsheetApp:{openById:()=>({getSheetByName:sheet}),newDataValidation:()=>validation,flush:()=>{}},
  UrlFetchApp:{fetch:(url,options)=>{
    if(url==='https://sheets.googleapis.com/v4/spreadsheets/'+config.spreadsheet_id+':batchUpdate') {
      if(options.method!=='post' || options.followRedirects!==false)throw Error('unsafe HTTP');
      const batch=JSON.parse(options.payload);batches.push(batch);
      put(batch.requests.map(({updateCells:u})=>({tab:names[u.range.sheetId-1],row:u.range.startRowIndex+1,column:u.range.startColumnIndex,
        values:u.rows[0].values.map(x=>{
          const v=x.userEnteredValue;
          if('formulaValue' in v)throw Error('unapproved formula injection');
          return v.stringValue??v.numberValue??v.boolValue;
        })})));
      return {getResponseCode:()=>200};
    }
    gateGets++;
    if(url!=='https://api.github.com/repos/'+config.repository+'/actions/variables/RECEIPT_VALIDATION_ENABLED' || options.method!=='get')throw Error('forbidden IO');
    if(input.fault==='gate')throw Error('private token');
    return {getResponseCode:()=>input.gate_status??200,getContentText:()=>JSON.stringify({name:'RECEIPT_VALIDATION_ENABLED',value:input.gate??'true'})};
  }}});
for(const name of ['Code.gs','Sheets.gs','Review.gs','ReviewSetup.gs'])vm.runInContext(fs.readFileSync('apps-script/receipt-validation/'+name,'utf8'),ctx);
const ports={readCases:()=>clone(cases),readRequests:()=>{
  if(writes && input.fault==='readback')throw Error('private');return clone(requests);
},readUI:()=>{
  captures++;
  const result=clone(ui);
  if(input.fault==='capture' && captures%2===0 && result.length)result[0][7]='competing edit';
  return result;
},decorate:()=>{decorations++;},write:put};
if(!input.http_ports)ctx.receiptReviewPorts_=()=>ports;
const errors=[],outputs=[];
for(const command of input.commands || []) {
  try {
    if(command.action==='register') {
      const range={getSheet:()=>({getName:()=> '確認',getParent:()=>({getId:()=>config.spreadsheet_id})}),
        getRow:()=>2,getColumn:()=>9,getNumRows:()=>1,getNumColumns:()=>1};
      outputs.push(ctx.receiptReviewEdited({range,value:'register:'+command.request_id,oldValue:'ready:'+command.request_id}));
    } else if(command.action==='start')outputs.push(ctx.receiptReviewStart());
    else if(command.action==='stop')outputs.push(ctx.receiptReviewStop());
    else if(command.action==='snapshot')outputs.push(ctx.receiptReviewSnapshot_(command.row,command.entry));
    else if(command.action==='formulas')outputs.push(ctx.receiptReviewFormulas_());
    else outputs.push(ctx.receiptReviewBackend(command));
  } catch(error){errors.push(error.message);outputs.push(null);}
}
process.stdout.write(JSON.stringify({cases,requests,ui,outputs,errors,writes,gateGets,decorations,properties,formulas,batches,validationCount}));
