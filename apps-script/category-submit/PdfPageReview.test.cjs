const {test}=require('node:test');
const assert=require('node:assert/strict');
const vm=require('node:vm');
const fs=require('node:fs');
const source=fs.readFileSync(__dirname+'/PdfGrouping.gs','utf8');

function harness({kind='medical', complete=false, dispatch=null, throws=false}={}) {
  const identity={schema:'pdf-page-review-v1',kind,source_file_id:'synthetic-source'};
  const fields=kind==='human_general' ? ['target','automatic','state','original','notice','human_general_kind','result'] : kind==='medical' ? ['target','kind','state','original','notice','date','facility','amount','category','payment','memo','medical_action','duplicate_target','result'] : kind==='general_manual' ? ['target','kind','state','original','notice','date','amount','category','merchant','payment','memo','manual_action','result'] : ['target','kind','state','original','kind_choice','kind_action','result'];
  const values={target:'p1',kind:'医療',state:'医療入力待ち',original:'原本を開く',notice:'完全手入力',date:complete?'2026/09/01':'',facility:complete?'Synthetic manual clinic':'',amount:complete?'1,200':'',category:kind==='general_manual' ? (complete?'食費｜外食':'') : '医療費',medical_action:'',manual_action:'',kind_choice:'未選択',kind_action:'',result:''};
  const rows=fields.map(f=>[f,values[f]||'','pdf-page-review-v1','token',JSON.stringify(identity),f,...Array(8).fill('')]);
  const queue=[],calls=[],validations=[];
  const range=(n,c,h=1,w=1)=>({
    getDisplayValue:()=>n===1&&c===3 ? 'pdf-page-review-v1' : rows[n-2]?.[c-1]||'',
    getDisplayValues:()=>rows.slice(n-2,n-2+h).map(r=>r.slice(c-1,c-1+w)),
    getRichTextValue:()=>({getLinkUrl:()=>''}),
    setDataValidation:v=>validations.push(v),
    setValue:v=>{rows[n-2][c-1]=v;},getSheet:()=>sheet,
    getNumRows:()=>h,getNumColumns:()=>w,getRow:()=>n,getColumn:()=>c,
  });
  const sheet={getName:()=> 'PDFページ確認',getLastRow:()=> rows.length+1,getRange:range};
  const inbox={getLastRow:()=>queue.length+1,appendRow:r=>queue.push(r),getRange:()=>({getValues:()=>queue,setValue:v=>{queue[queue.length-1][1]=v;}})};
  const ctx=vm.createContext({SpreadsheetApp:{flush:()=>{},newDataValidation:()=>({requireValueInList(v){this.choices=v;return this;},setAllowInvalid(v){this.strict=!v;return this;},build(){return {choices:this.choices,strict:this.strict};}})},
    PropertiesService:{getScriptProperties:()=>({getProperty:()=>dispatch}),getUserProperties:()=>({getProperty:()=> 'github_pat_TEST'})},
    LockService:{getScriptLock:()=>({tryLock:()=>true,releaseLock:()=>{}})},Utilities:{getUuid:()=> '12345678-1234-1234-1234-123456789abc'},Date,JSON});
  vm.runInContext(source,ctx);
  ctx.categorySpreadsheet_=()=>({getSheetByName:n=>n==='_PDF確認受付'?inbox:sheet});
  ctx.categoryFetch_=(url,method,body)=>{calls.push({url,method,body});if(throws)throw Error('unknown response');return {getResponseCode:()=>204};};
  function edit(field,value,h=1) {const n=fields.indexOf(field)+2;rows[n-2][1]=value;ctx.pdfGroupingEdited({range:range(n,2,h,1),value});}
  return {ctx,rows,queue,calls,validations,edit};
}

test('phone typing only updates confirm validation, never captures or dispatches',()=>{
  const h=harness();h.edit('facility','Synthetic human input');
  assert.equal(h.queue.length,0);assert.equal(h.calls.length,0);
  assert.deepEqual(Array.from(h.validations.at(-1).choices),['保留']);
});
test('incomplete medical explicit action is held without request or writer',()=>{
  const h=harness();h.edit('medical_action','医療費を確定');
  assert.equal(h.queue.length,0);assert.equal(h.calls.length,0);
  assert.equal(h.rows.find(r=>r[5]==='medical_action')[1],'保留');
});
test('four manual fields enable confirmation; captures current snapshot and no authority label',()=>{
  const h=harness({complete:true});h.edit('medical_action','医療費を確定');
  assert.equal(h.queue.length,1);assert.equal(h.queue[0][1],'accepted');assert.equal(h.calls.length,0);
  const snap=JSON.parse(h.queue[0][2]);
  assert.equal(snap.identity.kind,'medical');assert.equal(snap.rows.find(r=>r[0]==='facility')[2],'Synthetic manual clinic');
  assert.ok(!h.rows.find(r=>r[5]==='result')[1].includes('確定済み'));
  h.edit('medical_action','医療費を確定');assert.equal(h.queue.length,1);
});
test('dispatch opt-in sends UUID only, does not resend ambiguous delivery',()=>{
  const h=harness({complete:true,dispatch:'true',throws:true});h.edit('medical_action','医療費を確定');h.edit('medical_action','医療費を確定');
  assert.equal(h.calls.length,1);assert.equal(h.queue.length,1);
  assert.deepEqual(JSON.parse(JSON.stringify(h.calls[0].body)),{ref:'main',inputs:{mode:'review',request_id:'12345678-1234-1234-1234-123456789abc'}});
});
test('unknown kind requires a deliberate selection; normal typing cannot confirm a page',()=>{
  const h=harness({kind:'page_kind'});h.edit('kind_action','種別を確定');assert.equal(h.queue.length,0);
  h.edit('kind_choice','医療');assert.equal(h.queue.length,0);
  h.edit('kind_action','種別を確定');assert.equal(h.queue.length,1);
});
test('bulk paste cannot submit medical confirmation',()=>{
  const h=harness({complete:true});h.edit('medical_action','医療費を確定',2);
  assert.equal(h.queue.length,0);assert.equal(h.calls.length,0);
});
test('invalid calendar day never offers confirm',()=>{
  const h=harness({complete:true});h.edit('date','2026/02/30');
  assert.deepEqual(Array.from(h.validations.at(-1).choices),['保留']);
});

test('general typing does not capture; three required inputs enable explicit confirmation with optional blanks',()=>{
  const h=harness({kind:'general_manual'});h.edit('date','2026/09/24');h.edit('amount','500');h.edit('category','食費｜外食');
  assert.equal(h.queue.length,0);assert.equal(h.calls.length,0);
  assert.deepEqual(Array.from(h.validations.at(-1).choices),['保留','一般支出を確定']);
  h.edit('manual_action','一般支出を確定');assert.equal(h.queue.length,1);assert.equal(h.calls.length,0);
  const snap=JSON.parse(h.queue[0][2]);assert.equal(snap.identity.kind,'general_manual');
  assert.equal(snap.rows.find(r=>r[0]==='merchant')[2],'');assert.equal(snap.rows.find(r=>r[0]==='payment')[2],'');
});
test('incomplete general input and invalid date cannot capture confirmation',()=>{
  const h=harness({kind:'general_manual'});h.edit('manual_action','一般支出を確定');assert.equal(h.queue.length,0);
  const full=harness({kind:'general_manual',complete:true});full.edit('date','2026/02/30');full.edit('manual_action','一般支出を確定');assert.equal(full.queue.length,0);
});
test('general hold is intent only and bulk paste does not confirm',()=>{
  const h=harness({kind:'general_manual'});h.edit('manual_action','保留');assert.equal(h.queue.length,1);assert.equal(h.calls.length,0);
  const full=harness({kind:'general_manual',complete:true});full.edit('manual_action','一般支出を確定',2);assert.equal(full.queue.length,0);
});

test('new human general intent is never legacy authority or auto-dispatched; no count or inferred actor',()=>{
  const h=harness({kind:'human_general',dispatch:'true'});
  h.edit('human_general_kind','未選択');assert.equal(h.queue.length,0);
  h.edit('human_general_kind','一般レシート');assert.equal(h.queue.length,1);
  assert.equal(h.queue[0][1],'accepted');assert.equal(h.calls.length,0);
  const snapshot=JSON.parse(h.queue[0][2]);assert.equal(snapshot.identity.kind,'human_general');
  assert.equal(snapshot.rows.find(r=>r[0]==='human_general_kind')[2],'一般レシート');
  assert.equal(snapshot.actor,undefined);assert.ok(!JSON.stringify(snapshot).includes('枚数'));
  h.edit('human_general_kind','一般レシート');assert.equal(h.queue.length,1);
});

test('new general bulk paste cannot create a confirmation request',()=>{
  const h=harness({kind:'human_general'});h.edit('human_general_kind','一般レシート',2);
  assert.equal(h.queue.length,0);assert.equal(h.calls.length,0);
});
