// Actual receiver, fake Google services only. stdout is consumed by pytest.
const fs=require('node:fs'),vm=require('node:vm'),{createHash}=require('node:crypto');
const input=JSON.parse(fs.readFileSync(0,'utf8')), clone=x=>JSON.parse(JSON.stringify(x));
const state={RECEIPT_VALIDATION_CONFIG_JSON:JSON.stringify(input.config),RECEIPT_GITHUB_TOKEN:'github_pat_SYNTHETIC'};
const props={getProperty:k=>state[k]||null,setProperty:(k,v)=>state[k]=v,deleteProperty:k=>delete state[k]};
let rows=clone(input.table),ui=clone(input.ui),writes=0,dispatches=[],calls=[],errors=[],captures=0;
const ss={getId:()=>input.config.spreadsheet_id,getSheetByName:n=>n==='確認'?sheet:null};
const sheet={getName:()=> '確認',getParent:()=>ss,getLastRow:()=>ui.length+1,
  getRange:()=>({getValues:()=>{
    captures++;
    const result=clone(ui);
    if(input.fault==='capture' && captures%2===0)result[0][18]='途中変更';
    return result;
  }})};
const range={getSheet:()=>sheet,getRow:()=>2,getColumn:()=>9,getNumRows:()=>1,getNumColumns:()=>1};
const reply=(status,data)=>({getResponseCode:()=>status,getContentText:()=>JSON.stringify(data)});
const globals={PropertiesService:{getUserProperties:()=>props},
  Session:{getEffectiveUser:()=>({getEmail:()=>input.config.owner_email})},
  Utilities:{DigestAlgorithm:{SHA_256:'sha256'},Charset:{UTF_8:'utf8'},
    computeDigest:(_a,text)=>Array.from(createHash('sha256').update(text).digest())},
  SpreadsheetApp:{openById:()=>ss},
  ScriptApp:{getOAuthToken:()=> 'SYNTHETIC'},
  LockService:{getScriptLock:()=>({tryLock:()=>true,releaseLock:()=>{}})},
  UrlFetchApp:{fetch:(url,options)=>{
    calls.push({url,method:options.method});
    if(options.followRedirects!==false || options.muteHttpExceptions!==true)throw Error('unsafe fetch');
    if(url.startsWith('https://api.github.com/')){
      dispatches.push(JSON.parse(options.payload));
      if(input.fault==='dispatch')throw Error('synthetic lost dispatch response');
      return reply(input.fault==='dispatch_status'?500:204,{});
    }
    if(!url.startsWith('https://sheets.googleapis.com/') || !decodeURIComponent(url).includes("'_receipt_requests'!"))
      throw Error('forbidden endpoint');
    if(options.method==='get')return reply(200,{values:clone(rows)});
    if(options.method!=='put')throw Error('forbidden mutation');
    writes++;
    if(input.fault==='insert_before')throw Error('synthetic before commit');
    const m=/!A(\d+):J(\d+)\?/.exec(decodeURIComponent(url));
    if(!m || Number(m[1])!==rows.length+1 || m[1]!==m[2])throw Error('overwrite existing row');
    rows.push(JSON.parse(options.payload).values[0]);
    if(input.fault==='insert_after')throw Error('synthetic after commit response lost');
    if(input.fault==='readback')rows[rows.length-1][6]='a'.repeat(64);
    return reply(200,{});
  }}
};
const ctx=vm.createContext(globals);
for(const name of ['Code.gs','Connection.gs','Sheets.gs'])vm.runInContext(fs.readFileSync(__dirname+'/'+name,'utf8'),ctx);
const event={range,value:'register:'+input.request_id,oldValue:'ready:'+input.request_id};
function call(){try{return ctx.receiptConnectionEdited(event);}catch(e){errors.push(e.message);return null;}}
const output=call();
const repeated=input.repeat?call():null;
process.stdout.write(JSON.stringify({rows,ui,writes,dispatches,calls,errors,output,repeated,
  stopped:!!state.RECEIPT_SHEETS_STOPPED}));
