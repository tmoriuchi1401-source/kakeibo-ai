const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const crypto = require('node:crypto');
const source = fs.readFileSync(__dirname+'/Code.gs','utf8');
const id = '12345678-1234-1234-1234-123456789abc';

function harness({state='', response=204, throws=false}={}) {
  const data = {'A2:J2':[[id,state,'2026-09-23T10:00:00Z','','','','',0,'','1']]};
  const ui = {};
  const rows = ['■ 1. カテゴリを選ぶ・今後の自動分類','header','private purchase',
    '■ 2. 過去分の固定プレビュー','header','■ 3. 固定プレビューを確認して反映','header']
    .map(s=>[s,...Array(11).fill('')]);
  const calls=[];
  function sheet(values) {
    return {
      getRange: (...args) => {
        const key=args.join(':');
        return {
          getValues:()=>values[key]||[['']],
          getValue:()=>key==='B2' && values===data ? data['A2:J2'][0][1] : (values[key]||[['']])[0][0],
          getDisplayValues:()=>rows,
          setValues: v=>{values[key]=v;},
          setValue: v=>{ if(key==='B2' && values===data) data['A2:J2'][0][1]=v; else values[key]=[[v]]; },
        };
      }, getLastRow:()=>rows.length+5, getMaxRows:()=>1000
    };
  }
  const queue = sheet(data), surface=sheet(ui);
  const ss={getSheetByName:n=>n==='カテゴリ操作'?surface:queue};
  const context = vm.createContext({
    SpreadsheetApp:{getActive:()=>ss,flush:()=>{}},
    PropertiesService:{getUserProperties:()=>({getProperty:()=> 'github_pat_TEST'})},
    LockService:{getScriptLock:()=>({tryLock:()=>true,releaseLock:()=>{}})},
    Utilities:{getUuid:()=>id,DigestAlgorithm:{SHA_256:'sha256'},Charset:{UTF_8:'utf8'},
      computeDigest:(_,v)=>Array.from(crypto.createHash('sha256').update(v).digest())},
    UrlFetchApp:{fetch:(url,options)=>{calls.push({url,options});if(throws)throw Error('network');
      return {getResponseCode:()=>response};}},
    Date,JSON,encodeURIComponent,
  });
  vm.runInContext(source,context);
  return {run:()=>context.submitCategoryInput(),context,data,ui,rows,calls};
}

test('checkbox accepts only the exact B1 TRUE edit',()=>{
  const h=harness(); let calls=0;h.context.submitCategoryInput=()=>calls++;
  for(const [tab,cell,value] of [['カテゴリ操作','C1','TRUE'],['別','B1','TRUE'],['カテゴリ操作','B1','FALSE']])
    h.context.categorySubmitEdited({range:{getSheet:()=>({getName:()=>tab}),getA1Notation:()=>cell},value});
  assert.equal(calls,0);
  h.context.categorySubmitEdited({range:{getSheet:()=>({getName:()=> 'カテゴリ操作'}),getA1Notation:()=> 'B1'},value:'TRUE'});
  assert.equal(calls,1);
});

test('captures input before dispatch and sends only UUID',()=>{
  const h=harness();h.run();
  assert.equal(h.calls.length,1);
  assert.deepEqual(JSON.parse(h.calls[0].options.payload),{ref:'main',inputs:{request_id:id}});
  assert.ok(!h.calls[0].options.payload.includes('private purchase'));
  assert.equal(h.data['A2:J2'][0][1],'dispatching');
  assert.equal(h.data['4:1:7:1'][2][0],JSON.stringify(h.rows[2]));
  assert.equal(h.data['A2:J2'][0][8],crypto.createHash('sha256').update(JSON.stringify(h.rows)).digest('hex'));
  assert.equal(h.ui.C1[0][0],'受付済み・開始待ち');
});

test('repeated clicks cannot replace a busy snapshot',()=>{
  for(const state of ['dispatching','accepted','running']) {
    const h=harness({state});h.run();assert.equal(h.calls.length,0);
    assert.equal(h.data['A2:J2'][0][1],state);
  }
});

test('uncertain network response holds captured request without retry',()=>{
  const h=harness({throws:true});h.run();h.run();
  assert.equal(h.calls.length,1);
  assert.equal(h.data['A2:J2'][0][1],'dispatching');
  assert.equal(h.ui.C1[0][0],'受付確認中');
});

test('definitive dispatch failure allows repair and preserves source edits',()=>{
  const h=harness({response:403});h.run();
  assert.equal(h.data['A2:J2'][0][1],'dispatch_failed');
  assert.equal(h.ui.B1[0][0],false);
  assert.equal(h.rows[2][0],'private purchase');
});
