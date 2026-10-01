const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const crypto = require('node:crypto');
const source = fs.readFileSync(__dirname+'/Code.gs','utf8');
const pdfSource = fs.readFileSync(__dirname+'/PdfGrouping.gs','utf8');
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
      formatDate:date=>new Date(date.getTime()+9*60*60*1000).toISOString().slice(0,19).replace('T',' '),
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

function pdfHarness({response=204, throws=false, target='', operation='確定'}={}) {
  const snapshot = ['pdf-review-token','未確定','2','Group 1: p1\nGroup 2: p2','一般候補','reason',
    'PRIVATE_SOURCE_LINK',operation,target,'','PRIVATE_SOURCE_ID','a'.repeat(64),'b'.repeat(64),'1','row-token',''];
  const rows=[], results=[], calls=[];
  const queue={getLastRow:()=>rows.length+1,
    getRange:()=>({getValues:()=>rows, setValue:v=>{rows[rows.length-1][1]=v;}}),
    appendRow:r=>rows.push(r)};
  const surface={getRange:(...args)=>({getDisplayValues:()=>[snapshot],setValue:v=>results.push(v)})};
  const ctx=vm.createContext({
    categorySpreadsheet_:()=>({getSheetByName:n=>n==='PDFページ確認'?surface:queue}),
    categoryFetch_:(path,method,payload)=>{calls.push({path,method,payload});if(throws)throw Error('network');
      return {getResponseCode:()=>response};},
    LockService:{getScriptLock:()=>({tryLock:()=>true,releaseLock:()=>{}})},
    PropertiesService:{getUserProperties:()=>({getProperty:()=> 'github_pat_TEST'})},
    SpreadsheetApp:{flush:()=>{}}, Utilities:{getUuid:()=>id}, Date,JSON,
  });
  vm.runInContext(pdfSource,ctx);
  return {ctx,snapshot,rows,results,calls,run:()=>ctx.submitPdfGroupingRow_(2)};
}

test('PDF confirmation captures before dispatch, sends UUID only, never marks confirmed',()=>{
  const h=pdfHarness();h.run();
  assert.equal(h.rows[0][2],JSON.stringify(h.snapshot));
  assert.deepEqual(JSON.parse(JSON.stringify(h.calls[0].payload)),{ref:'main',inputs:{mode:'review',request_id:id}});
  assert.ok(!JSON.stringify(h.calls).includes('PRIVATE_SOURCE'));
  assert.ok(h.results.every(v=>!v.includes('確定済み')));
  h.run();assert.equal(h.calls.length,1);
});

test('PDF ambiguous dispatch cannot resend, definitive error remains unconfirmed',()=>{
  const h=pdfHarness({throws:true});h.run();h.run();
  assert.equal(h.calls.length,1);assert.equal(h.rows[0][1],'dispatching');
  const failed=pdfHarness({response:403});failed.run();
  assert.equal(failed.rows[0][1],'error');
  assert.ok(failed.results.every(v=>!v.includes('確定済み')));
});

test('PDF edit requires selected group; confirm needs no partition input',()=>{
  const missing=pdfHarness({operation:'分割'});missing.run();assert.equal(missing.calls.length,0);
  const merged=pdfHarness({operation:'結合',target:'1+2'});merged.run();assert.equal(merged.calls.length,1);
  const confirmed=pdfHarness();confirmed.run();assert.equal(confirmed.calls.length,1);
});

test('PDF only accepts a single operation dropdown edit, never bulk/other edits',()=>{
  const h=pdfHarness();let count=0;h.ctx.submitPdfGroupingRow_=()=>count++;
  const event=(tab,col,nRows=1)=>({value:'確定',range:{getSheet:()=>({getName:()=>tab}),
    getColumn:()=>col,getRow:()=>2,getNumRows:()=>nRows,getNumColumns:()=>1}});
  h.ctx.pdfGroupingEdited(event('PDFページ確認',9));
  h.ctx.pdfGroupingEdited(event('支出明細',8));
  h.ctx.pdfGroupingEdited(event('PDFページ確認',8,2));
  assert.equal(count,0);h.ctx.pdfGroupingEdited(event('PDFページ確認',8));assert.equal(count,1);
});
