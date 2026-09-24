const {test}=require('node:test');
const assert=require('node:assert/strict');
const {readFileSync}=require('node:fs');
const vm=require('node:vm');
const script=readFileSync(__dirname+'/Code.gs','utf8');
const id='12345678-1234-1234-1234-123456789abc';
function harness() {
  const queue=[['request_id','state','payload_json','submitted_at','expense_id','message']];
  const cats=[['その他','未分類'],['食費','外食']];
  const calls=[];
  const queueSheet={
    appendRow:r=>queue.push(r),getLastRow:()=>queue.length,
    getRange:(row,col)=>({
      createTextFinder:needle=>({matchEntireCell(){return this},findNext:()=>{
        const index=queue.findIndex((r,i)=>i>0&&r[0]===needle);
        return index<0?null:{getRow:()=>index+1};
      }}),
      getValues:()=>[queue[row-1].slice(col-1,col+5)],
      setValue:()=>{throw Error('unexpected status update')},
    })};
  const catSheet={getRange:()=>({getDisplayValues:()=>cats})};
  const ss={getSheetByName:name=>({'_手入力受付':queueSheet,'カテゴリ':catSheet,'支出明細':{}})[name]};
  const ctx=vm.createContext({
    SpreadsheetApp:{openById:()=>ss,flush:()=>{}},
    PropertiesService:{getScriptProperties:()=>({getProperty:key=>key==='MANUAL_SPREADSHEET_ID'?'sheet':'token'})},
    LockService:{getScriptLock:()=>({waitLock:()=>{},releaseLock:()=>{}})},
    UrlFetchApp:{fetch:(url,options)=>{calls.push({url,options});return {getResponseCode:()=>204}}},
    Utilities:{getUuid:()=>id,formatDate:()=> '2026-09-24'},Date,JSON,Number,String,Set,
  });
  vm.runInContext(script,ctx);
  const input={id,amount:'500',date:'2026-09-24',merchant:'お祭り',major:'食費',minor:'外食',note:''};
  return {ctx,input,calls,queue};
}
test('capture private content and send only UUID; repeated click is idempotent',()=>{
  const h=harness();
  assert.equal(h.ctx.manualSubmit(h.input).state,'dispatching');
  assert.equal(h.queue.length,2);
  assert.equal(JSON.parse(h.queue[1][2]).merchant,'お祭り');
  assert.deepEqual(JSON.parse(h.calls[0].options.payload),{ref:'main',inputs:{request_id:id}});
  assert.ok(!h.calls[0].options.payload.includes('お祭り'));
  h.ctx.manualSubmit(h.input);
  assert.equal(h.queue.length,2);
  assert.equal(h.calls.length,1);
});
test('category pair must exist in master; no dispatch on invalid selection',()=>{
  const h=harness();
  assert.throws(()=>h.ctx.manualSubmit({...h.input,minor:'未分類'}),/カテゴリ/);
  assert.equal(h.queue.length,1);
  assert.equal(h.calls.length,0);
});
