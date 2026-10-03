const {test}=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const ctx=vm.createContext({JSON,Date,Error,Number,String});
vm.runInContext(fs.readFileSync(__dirname+'/CategoryConfirmation.gs','utf8'),ctx);

test('month summary and historical examples cannot mix dates',()=>{
  const expense=(date,amount,category=['その他','未分類'])=>['id',date,'長い摘要','自動計上',amount,...category];
  const result=ctx.ccSummary_([expense('2025-10-16',10846),expense('2026-02-03',8980),expense('2026-08-21',6240),expense('2026-09-12',500),expense('2026-09-13',300,['食費','外食']),expense('2026-10-01',700)],'2026-09');
  assert.equal(result.monthCount,1);assert.equal(result.monthAmount,500);
  assert.equal(result.allCount,5);assert.equal(result.allAmount,27266);
  assert.deepEqual(Array.from(result.samples,e=>e[1]),['2026-08-21','2026-02-03','2025-10-16']);
});

test('exact evidence binding excludes unrelated source/account/target/inactive rows',()=>{
  const e=(id,importId,status='active')=>[id,'2026-09-01','原文','自動計上',100,'その他','未分類','','','',''+importId,'',status];
  const tx=(id,source,merchant,target)=>[id,'',source,'','',merchant,100,'','auto_expense',target];
  const proof={kind:'service',source:'PayPay',account_alias:'',merchant:'ABC STORE'};
  const expenses=[['header'],e('a','i1'),e('b','i2'),e('c','i3'),e('d','i4'),e('x','i5','inactive')];
  const imports={i1:tx('i1','PayPay','ABC STORE','a'),i2:tx('i2','au PAY','ABC STORE','b'),i3:tx('i3','PayPay','ABC','c'),i4:tx('i4','PayPay','ABC STORE','other'),i5:tx('i5','PayPay','ABC STORE','x')};
  assert.deepEqual(Array.from(ctx.ccMembers_(proof,expenses,imports),e=>e[0]),['a']);
});

test('capture clears every other action including bank group approval',()=>{
  const row=()=>Array(12).fill('');const marker=m=>[m,...Array(11).fill('')];
  const selected=row();selected[6]='chosen';const other=row();other[6]='other';other[4]='登録する';other[5]='TRUE';
  const bank=row();bank[5]='TRUE';bank[8]='group';
  const rows=[marker('■ 1. カテゴリを選ぶ・今後の自動分類'),row(),selected,other,marker('■ 2. 過去分の固定プレビュー'),row(),marker('■ 3. 固定プレビューを確認して反映'),row(),marker('■ 4. 銀行取引をまとめて確認'),row(),bank];
  const output=ctx.ccCapture_(rows,'chosen','食費｜外食','confirm',{scope:'反映しない',future:'OFF'});
  assert.equal(output[2][4],'登録しない');assert.equal(output[2][5],'FALSE');
  assert.equal(output[3][4],'未選択');assert.equal(output[3][5],'FALSE');assert.equal(output[10][5],'FALSE');
  assert.equal(rows[3][4],'登録する');assert.equal(rows[10][5],'TRUE');
});

test('visible unsaved settings stop confirmation before ledger access',()=>{
  const saved={category:'食費 ＞ 外食',future:'OFF',scope:'反映しない'};
  const sheet={getRange:a1=>({getValue:()=>JSON.stringify(saved),getDisplayValue:()=>a1==='A15'?'食費 ＞ 食料品':a1==='A17'?'OFF':'反映しない'})};
  assert.throws(()=>ctx.ccPrepare_({getSheetByName:()=>sheet},'この内容で確定'),/選択の保存/);
});

test('only correlated complete requests hide a candidate, once',()=>{
  ctx.CATEGORY_BUSY=['dispatching','running'];ctx.CATEGORY_QUEUE='queue';
  let state={pending:'uuid',key:'key',sig:'before',category:'食費 ＞ 外食',future:'OFF',scope:'反映しない'},queue=['uuid','running','','','','result'];
  const logged=[];
  const panel={getRange:()=>({getValue:()=>JSON.stringify(state),setValue:v=>{if(typeof v==='string' && v.startsWith('{'))state=JSON.parse(v);},getDisplayValue:()=>''}),setRowHeight:()=>{}};
  const log={getLastRow:()=>1,getRange:()=>({getDisplayValues:()=>[['header']]}),appendRow:r=>logged.push(r)};
  const ss={getSheetByName:name=>name==='カテゴリ確認'?panel:name==='queue'?{getRange:()=>({getDisplayValues:()=>[queue]})}:log};
  ctx.ccModel_=()=>({candidates:[{key:'key',sig:'after'}]});ctx.ccRefresh_=()=>{};ctx.ccPaint_=()=>{};
  ctx.ccSync_(ss);assert.equal(logged.length,0);assert.equal(state.pending,'uuid');
  queue[1]='error';ctx.ccSync_(ss);assert.equal(logged.length,0);assert.equal(state.pending,undefined);
  state.pending='uuid';queue[1]='complete';ctx.ccSync_(ss);assert.equal(logged.length,1);assert.equal(logged[0][0],'after');
  ctx.ccSync_(ss);assert.equal(logged.length,1);
});
