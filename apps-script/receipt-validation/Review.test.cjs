// Execute the real PR-B Apps Script with fake storage/services. No network.
const fs=require('node:fs'),vm=require('node:vm'),{createHash}=require('node:crypto');
const input=JSON.parse(fs.readFileSync(0,'utf8')),clone=x=>JSON.parse(JSON.stringify(x));
const config={enabled:true,folder_id:'synthetic-folder-b',owner_email:'owner@synthetic.invalid',
  repository:'tmoriuchi1401-source/kakeibo-ai',script_id:'synthetic-script-b',spreadsheet_id:'synthetic-sheet-b',...input.config};
const properties={RECEIPT_REVIEW_CONFIG_JSON:JSON.stringify(config),
  RECEIPT_REVIEW_REGISTER_ENABLED:'true',...input.properties};
if(input.config)properties.RECEIPT_REVIEW_CONFIG_JSON=JSON.stringify(config);
const props={getProperty:k=>properties[k]??null,setProperty:(k,v)=>{properties[k]=v;},deleteProperty:k=>{delete properties[k];}};
let locked=false,writes=0,gateGets=0,driveGets=0,captures=0,decorations=0,validationCount=0,restGets=0,spreadsheetReads=0;
const diagnostics=[],cache=new Map();
const formulas=[],batches=[],hiddenColumns=[],clearedValidations=[],notes=[],checkboxes=[];
let cases=clone(input.cases || [['case_id','source_id','source_version','source_sha256','case_json','case_digest','request_id']]);
let requests=clone(input.requests || [['request_id','receipt_id','source_id','source_version','source_sha256','snapshot_json','snapshot_digest','status','validation_result','updated_at']]);
let ui=clone(input.ui || []);
let tax=clone(input.tax || [['correction_id','request_id','snapshot_digest','correction_no','event','payload_json','payload_digest','created_at']]);
let taxExists=!!input.tax;
let policy=clone(input.policy || [['request_id','snapshot_digest','validator_revision','original_result_digest','result_json','result_digest','created_at']]);
let policyExists=!!input.policy;
function put(changes) {
  writes++;
  if(input.fault==='before')throw Error('lost before write');
  for(const change of changes){
    const table=change.tab==='_receipt_cases'?cases:change.tab==='_receipt_requests'?requests:change.tab==='_receipt_tax_corrections'?tax:change.tab==='_receipt_policy_reviews'?policy:ui;
    const offset=change.tab==='確認'?2:1,index=change.row-offset;
    if(index>table.length)throw Error('gap');
    if(index===table.length)table.push([]);
    table[index].splice(change.column||0,change.values.length,...clone(change.values));
  }
  if(input.tax_recalc && (tax.length>1 || policy.length>1) && ui.length)ui[0][6]='calculated-tax-result';
  if(input.fault==='after')throw Error('lost after write');
}
const names=['確認','_receipt_cases','_receipt_requests','_receipt_tax_corrections','_receipt_policy_reviews'];
function sheet(name) {
  if(name==='_receipt_tax_corrections' && !taxExists)return null;
  if(name==='_receipt_policy_reviews' && !policyExists)return null;
  const table=()=>name==='確認'?[Array(98).fill('header'),...ui]:name==='_receipt_cases'?cases:name==='_receipt_tax_corrections'?tax:name==='_receipt_policy_reviews'?policy:requests;
  const cached=()=>{
    spreadsheetReads++;
    if(!input.stale_spreadsheet)return table();
    if(!cache.has(name))cache.set(name,clone(table()));
    return cache.get(name);
  };
  return {getSheetId:()=>names.indexOf(name)+1,getLastRow:()=>cached().length,
    hideSheet:()=>{},protect:()=>({getEditors:()=>[],removeEditors:()=>{},canDomainEdit:()=>false}),
    hideColumns:(start,count=1)=>hiddenColumns.push([start,count]),
    getRange:(r,c,n=1,w=1)=>({getValues:()=>Array.from({length:n},(_,i)=>Array.from({length:w},(_,j)=>cached()[r+i-1]?.[c+j-1]??'')),
      setDataValidation:()=>{validationCount++;},setFormulas:values=>formulas.push(...values[0]),
      setFormula:value=>{if(input.fault==='setup')throw Error('setup interrupted');formulas.push(value);},setValue:()=>{},
      setValues:values=>{if(name==='_receipt_tax_corrections' && r===1)tax=clone(values);
        else if(name==='_receipt_policy_reviews' && r===1)policy=clone(values);else throw Error('unexpected setup write');},
      clearDataValidations:()=>clearedValidations.push([r,c]),setNote:value=>notes.push([r,c,value])})};
}
const validation={requireValueInList(){return this;},setAllowInvalid(){return this;},requireCheckbox(...args){checkboxes.push(args);return this;},build(){return {};}};
const ctx=vm.createContext({console:{warn:message=>diagnostics.push(JSON.parse(message))},PropertiesService:{getScriptProperties:()=>props},
  Session:{getEffectiveUser:()=>({getEmail:()=>input.owner_override || config.owner_email})},ScriptApp:{getScriptId:()=>config.script_id,
    getOAuthToken:()=> 'DUMMY',EventType:{ON_EDIT:'ON_EDIT'},getProjectTriggers:()=>input.no_trigger?[]:[{getHandlerFunction:()=> 'receiptReviewEdited',
      getTriggerSourceId:()=>config.spreadsheet_id,getEventType:()=> 'ON_EDIT',getUniqueId:()=> 'synthetic-trigger'}]},
  Utilities:{DigestAlgorithm:{SHA_256:'sha256'},Charset:{UTF_8:'utf8'},computeDigest:(_a,text)=>Array.from(createHash('sha256').update(Buffer.isBuffer(text)?text:typeof text==='string'?text:Buffer.from(text)).digest())},
  LockService:{getScriptLock:()=>({tryLock:()=>{if(locked || input.busy)return false;locked=true;return true;},releaseLock:()=>{locked=false;}})},
  SpreadsheetApp:{openById:()=>({getId:()=>config.spreadsheet_id,
    getOwner:()=>({getEmail:()=>input.sheet_owner || config.owner_email}),
    getEditors:()=> (input.sheet_editors || [config.owner_email]).map(email=>({getEmail:()=>email})),
    getSheetByName:sheet,insertSheet:name=>{
    if(name==='_receipt_tax_corrections' && !taxExists){taxExists=true;return sheet(name);}
    if(name==='_receipt_policy_reviews' && !policyExists){policyExists=true;return sheet(name);}
    throw Error('unexpected sheet create');
  }}),newDataValidation:()=>validation,flush:()=>{}},
  UrlFetchApp:{fetch:(url,options)=>{
    if(url.startsWith('https://www.googleapis.com/drive/v3/files/')) {
      driveGets++;
      const parsed=new URL(url),sid=decodeURIComponent(parsed.pathname.split('/').pop());
      const source=cases.slice(1).map(row=>JSON.parse(row[4]).source).find(s=>s.source_id===sid);
      if(!source || options.method!=='get' || options.followRedirects!==false)throw Error('forbidden source read');
      const fault=input.drive_fault;
      if(fault==='network')return {getResponseCode:()=>503};
      if(parsed.searchParams.get('alt')==='media')return {getResponseCode:()=>200,
        getContent:()=>fault==='hash'?Array.from(Buffer.from('changed')):(input.drive_bytes || Array.from(Buffer.from(sid)))};
      const meta={id:sid,version:fault==='version' && driveGets>1?'13':input.drive_current_version || source.version,
        mimeType:source.mime_type,trashed:false,parents:[fault==='parent'?'other-folder':config.folder_id],
        owners:[{emailAddress:fault==='owner'?'other@synthetic.invalid':config.owner_email}]};
      return {getResponseCode:()=>200,getContentText:()=>JSON.stringify(meta)};
    }
    if(url.startsWith('https://sheets.googleapis.com/v4/spreadsheets/'+config.spreadsheet_id+'?')) {
      restGets++;
      if(options.method!=='get' || options.followRedirects!==false)throw Error('unsafe read HTTP');
      const query=new URL(url).searchParams,range=query.get('ranges');
      const match=/^'([^']+)'!A1:([A-Z]+)1000$/.exec(range || '');
      if(!match || !names.includes(match[1]))throw Error('unbounded or foreign range');
      const name=match[1],width=Array.from(match[2]).reduce((n,c)=>n*26+c.charCodeAt(0)-64,0);
      let table=clone(name==='確認'?[Array(98).fill('header'),...ui]:name==='_receipt_cases'?cases:name==='_receipt_tax_corrections'?tax:name==='_receipt_policy_reviews'?policy:requests);
      if(writes && input.fault==='readback')throw Error('private');
      if(input.stale_rest && writes)table=clone(input.stale_rest[name] || table);
      return {getResponseCode:()=>input.rest_status??200,getContentText:()=>JSON.stringify({spreadsheetId:config.spreadsheet_id,
        sheets:[{properties:{title:name,gridProperties:{rowCount:input.grid_rows??1000,columnCount:name==='確認'?98:26}},
          data:[{rowData:table.map(row=>({values:row.slice(0,width).map(value=>({effectiveValue:value===''?{}:
            typeof value==='boolean'?{boolValue:value}:typeof value==='number'?{numberValue:value}:{stringValue:value}}))}))}]}]})};
    }
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
for(const name of ['Code.gs','Sheets.gs','Review.gs','ReviewSetup.gs','TaxCorrection.gs','PolicyRecheck.gs','Hosted.gs'])vm.runInContext(fs.readFileSync('apps-script/receipt-validation/'+name,'utf8'),ctx);
if(input.legacy_spreadsheet_reader) {
  // The PR205 reader, over the real REST writer and real command/comparison.
  const real=ctx.receiptReviewPorts_;
  ctx.receiptReviewPorts_=c=>{
    const p=real(c),read=(name,width)=>{
      const s=sheet(name);return s.getRange(1,1,Math.max(1,s.getLastRow()),width).getValues();
    };
    return {...p,readCases:()=>read('_receipt_cases',7),readRequests:()=>read('_receipt_requests',10),readUI:()=>read('確認',98).slice(1)};
  };
}
const ports={readCases:()=>clone(cases),readTax:()=>clone(tax),readTaxIfPresent:()=>clone(tax),readPolicy:()=>clone(policy),readRequests:()=>{
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
    if(command.action==='hosted')outputs.push(ctx.receiptHostedBackend(command.command));
    else if(command.action==='hosted_start')outputs.push(ctx.receiptHostedStart());
    else if(command.action==='register') {
      const range={getSheet:()=>({getName:()=> '確認',getParent:()=>({getId:()=>config.spreadsheet_id})}),
        getRow:()=>2,getColumn:()=>9,getNumRows:()=>1,getNumColumns:()=>1};
      outputs.push(ctx.receiptReviewEdited({range,source:{getId:()=>input.event_book || config.spreadsheet_id},
        triggerUid:input.event_trigger || 'synthetic-trigger',
        user:input.editor_missing?undefined:{getEmail:()=>input.editor_override || config.owner_email},
        value:'register:'+command.request_id,oldValue:'ready:'+command.request_id}));
    } else if(command.action==='start')outputs.push(ctx.receiptReviewStart());
    else if(command.action==='registration_enable')outputs.push(ctx.receiptReviewRegistrationEnable());
    else if(command.action==='registration_disable')outputs.push(ctx.receiptReviewRegistrationDisable());
    else if(command.action==='registration_status')outputs.push(ctx.receiptReviewRegistrationStatus(command.request_id,
      command.verify_source));
    else if(command.action==='tax_setup')outputs.push(ctx.receiptReviewPrepareTaxCorrections());
    else if(command.action==='policy_setup')outputs.push(ctx.receiptReviewPreparePolicyRecheck());
    else if(command.action==='stop')outputs.push(ctx.receiptReviewStop());
    else if(command.action==='snapshot')outputs.push(ctx.receiptReviewSnapshot_(command.row,command.entry));
    else if(command.action==='formulas')outputs.push(ctx.receiptReviewFormulas_(!!command.tax,!!command.policy));
    else if(command.action==='layout')outputs.push(ctx.receiptReviewHideConfirmationColumns_(sheet('確認')));
    else outputs.push(ctx.receiptReviewBackend(command));
  } catch(error){errors.push(error.message);outputs.push(null);}
}
process.stdout.write(JSON.stringify({cases,requests,ui,tax,policy,outputs,errors,writes,gateGets,driveGets,decorations,properties,formulas,batches,validationCount,
  hiddenColumns,clearedValidations,notes,checkboxes,diagnostics,restGets,spreadsheetReads}));
