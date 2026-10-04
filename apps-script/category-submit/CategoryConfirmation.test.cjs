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

test('month navigation separates historical and future-only candidates without deletion',()=>{
  const candidates=[{key:'past',summary:{monthCount:0,allCount:3}},{key:'current',summary:{monthCount:2,allCount:5}},{key:'future',summary:{monthCount:0,allCount:0}}];
  for(const [filter,key] of [['対象月の未分類','current'],['他月の未処理','past'],['その他の候補','future']]){
    const p=ctx.ccPartition_(candidates,filter);
    assert.deepEqual(Array.from(p.visible,c=>c.key),[key]);
    assert.equal(p.groups.reduce((n,g)=>n+g.length,0),3);
  }
  assert.equal(ctx.ccPartition_(candidates,'すべて').visible.length,3);
  assert.equal(candidates.length,3);
});

test('opening a fresh candidate never inherits all-period or future-ON choices',()=>{
  const candidate={key:'new',sig:'snapshot',proof:{merchant:'new'},physical:['','','食費｜外食']};
  const state=ctx.ccInitial_(candidate,'2026-09','対象月の未分類');
  assert.equal(state.scope,'反映しない');assert.equal(state.future,'OFF');
  assert.equal(state.category,'食費 ＞ 外食');assert.equal(state.fixed,undefined);assert.equal(state.pending,undefined);
});

test('completed transaction snapshot stays excluded after key regeneration, new transactions remain reviewable',()=>{
  const crypto=require('node:crypto');
  const modelCtx=vm.createContext({JSON,Date,Error,Number,String,Utilities:{DigestAlgorithm:{SHA_256:'sha'},Charset:{UTF_8:'utf8'},
    computeDigest:(_a,text)=>Array.from(crypto.createHash('sha256').update(text).digest())}});
  vm.runInContext(fs.readFileSync(__dirname+'/CategoryConfirmation.gs','utf8'),modelCtx);
  modelCtx.CATEGORY_UI='legacy';
  const proof={kind:'service',source:'PayPay',account_alias:'',merchant:'ABC'};
  const row=Array(12).fill('');row[6]='original-key';row[11]=JSON.stringify(proof);
  const rules=[['■ 1. カテゴリを選ぶ・今後の自動分類'],Array(12).fill(''),row,['■ 2. 過去分の固定プレビュー']];
  const expenses=[['支出ID'],['a','2026-09-01','ABC','自動計上',100,'その他','未分類','','','','i1','','active']];
  const imported=[['取込ID'],['i1','','PayPay','','2026-09-01','ABC',100,'','auto_expense','a']];
  const log=[['snapshot','key','uuid']];
  const sheet=(rows,month)=>({getLastRow:()=>rows.length,getRange:a1=>({getDisplayValues:()=>rows,getDisplayValue:()=>month})});
  const legacy=sheet(rules,'2026-09');
  const sheets={legacy,'支出明細':sheet(expenses),'取込データ':sheet(imported),'_カテゴリ確認ログ':sheet(log)};
  const ss={getSheetByName:name=>sheets[name]};
  const candidate=modelCtx.ccModel_(ss).candidates[0];assert.equal(candidate.key,'original-key');
  log.push([candidate.sig,candidate.key,'completed-uuid']);
  assert.equal(modelCtx.ccModel_(ss).candidates.length,0);
  row[6]='regenerated-key';assert.equal(modelCtx.ccModel_(ss).candidates.length,0);
  expenses.push(['b','2026-09-02','ABC','自動計上',200,'その他','未分類','','','','i2','','active']);
  imported.push(['i2','','PayPay','','2026-09-02','ABC',200,'','auto_expense','b']);
  assert.equal(modelCtx.ccModel_(ss).candidates.length,1);
  assert.equal(log.length,2);
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
  let state={pending:'uuid',key:'key',sig:'before',category:'食費 ＞ 外食',future:'OFF',scope:'反映しない',fixed:{id:'fixed',count:1}},queue=['uuid','running','','','','result'];
  const request=['fixed','','previewed'];
  const logged=[];
  const panel={getRange:()=>({getValue:()=>JSON.stringify(state),setValue:v=>{if(typeof v==='string' && v.startsWith('{'))state=JSON.parse(v);},getDisplayValue:()=>''}),setRowHeight:()=>{}};
  const log={getLastRow:()=>1,getRange:()=>({getDisplayValues:()=>[['header']]}),appendRow:r=>logged.push(r)};
  const ss={getSheetByName:name=>name==='カテゴリ確認'?panel:name==='queue'?{getRange:()=>({getDisplayValues:()=>[queue]})}:name==='カテゴリ過去反映要求'?{getLastRow:()=>2,getRange:()=>({getDisplayValues:()=>[['header'],request]})}:log};
  ctx.ccModel_=()=>({candidates:[{key:'key',sig:'after'}]});ctx.ccRefresh_=()=>{};ctx.ccPaint_=()=>{};
  ctx.ccSync_(ss);assert.equal(logged.length,0);assert.equal(state.pending,'uuid');
  queue[1]='error';ctx.ccSync_(ss);assert.equal(logged.length,0);assert.equal(state.pending,undefined);
  state.pending='uuid';queue[1]='complete';ctx.ccSync_(ss);assert.equal(logged.length,0); // Queue completion alone cannot hide an unapplied audit preview.
  state.pending='uuid';request[2]='complete';ctx.ccSync_(ss);assert.equal(logged.length,1);assert.equal(logged[0][0],'after');
  ctx.ccSync_(ss);assert.equal(logged.length,1);
});

function freshContext(){
  const c=vm.createContext({JSON,Date,Error,Number,String});
  vm.runInContext(fs.readFileSync(__dirname+'/CategoryConfirmation.gs','utf8'),c);return c;
}

test('no-history action requires preview before it offers confirmation',()=>{
  const c=freshContext(),values={},dropdowns={};
  c.ccDropdown_=(_s,a1,options)=>{dropdowns[a1]=options;};c.ccFit_=()=>{};
  const sheet={getRange:a1=>({setValue:value=>{values[a1]=value;},getDisplayValue:()=>values[a1]||''})};
  const state={scope:'反映しない',future:'OFF',category:'食費 ＞ 外食'};
  c.ccPaint_(sheet,state);
  assert.deepEqual(Array.from(dropdowns.A24),['操作を選択','対象件数を確認']);
  assert.match(values.A21,/対象月の未分類だけ/);assert.match(values.A21,/追加の過去反映は0件/);
  state.fixed={count:1,amount:101};c.ccPaint_(sheet,state);
  assert.deepEqual(Array.from(dropdowns.A24),['操作を選択','この内容で確定']);
  assert.match(values.A21,/固定対象 1件/);
});

test('no-history fixed audit binds month, fallback category, identity set and digest',()=>{
  const c=freshContext(),proof={kind:'service',source:'PayPay',account_alias:'',merchant:'ABC'};
  const payload={...proof,billing_name:'ABC',merchant:'',category:['食費','外食']};
  const row=['audit','','previewed',JSON.stringify(payload),'2026-08-01','2026-08-31','1','101','digest','FALSE'];
  const target=['audit','M-1',2,'2026-08-10',101,'その他','未分類','食費','外食','','previewed'];
  const sheet=rows=>({getLastRow:()=>rows.length,getRange:()=>({getDisplayValues:()=>rows})});
  const ss={getSheetByName:name=>sheet([['header'],name==='カテゴリ過去反映要求'?row:target])};
  const state={scope:'反映しない',month:'2026-08',category:'食費 ＞ 外食',proof,fixed:{id:'audit',count:1,amount:101,digest:'digest',ids:['M-1']}};
  assert.equal(c.ccFixedValid_(ss,state),true);
  target[1]='M-other';assert.equal(c.ccFixedValid_(ss,state),false);target[1]='M-1';
  target[3]='2026-07-31';assert.equal(c.ccFixedValid_(ss,state),false);target[3]='2026-08-10';
  target[5]='食費';assert.equal(c.ccFixedValid_(ss,state),false);target[5]='その他';
  row[8]='changed';assert.equal(c.ccFixedValid_(ss,state),false);row[8]='digest';
  row[9]='TRUE';assert.equal(c.ccFixedValid_(ss,state),false);row[9]='FALSE';
  row[4]='';row[5]='';assert.equal(c.ccFixedValid_(ss,state),false);
});

test('no-history confirmation without a fixed preview stops before any writes',()=>{
  const c=freshContext();
  const state={key:'key',sig:'sig',month:'2026-08',category:'食費 ＞ 外食',future:'OFF',scope:'反映しない'};
  const panel={getRange:a1=>({getValue:()=>JSON.stringify(state),getDisplayValue:()=>({A15:state.category,A17:state.future,A20:state.scope})[a1],getDisplayValues:()=>[['食費 ＞ 外食','食費｜外食']]})};
  c.ccModel_=()=>({month:state.month,candidates:[{key:state.key,sig:state.sig}],legacy:{getRange:()=>assert.fail('no writes')}});
  const ss={getSheetByName:name=>name==='カテゴリ確認'?panel:{getLastRow:()=>2}};
  assert.throws(()=>c.ccPrepare_(ss,'この内容で確定'),/先に対象件数/);
});
