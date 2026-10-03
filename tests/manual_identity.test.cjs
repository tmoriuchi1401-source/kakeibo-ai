const {test}=require('node:test');
const assert=require('node:assert/strict');
const vm=require('node:vm');
const fs=require('node:fs');
const path=require('node:path');
const root=path.join(__dirname,'../apps-script');
const owner='tmoriuchi1401@gmail.com', wife='wife@example.test';
const id='12345678-1234-1234-1234-123456789abc';
function backend(overrides={}) {
  const now=Math.floor(Date.now()/1000), rows=[]; let dispatches=0, accesses=0;
  const properties={MANUAL_IDENTITY_AUDIENCE:'pinned-audience',MANUAL_HOUSEHOLD_EMAILS:JSON.stringify([owner,wife]),MANUAL_SPREADSHEET_ID:'private',MANUAL_GITHUB_TOKEN:'private-token'};
  const claims={iss:'https://accounts.google.com',aud:'pinned-audience',exp:now+3600,iat:now,sub:'12345',email:wife,email_verified:'true',...overrides.claims};
  const sheet={getLastRow:()=>rows.length+1,appendRow:r=>rows.push(r),getRange:(row,col,n,m)=>({
    createTextFinder:needle=>({matchEntireCell:()=>({findNext:()=>{const i=rows.findIndex(r=>r[0]===needle);return i<0?null:{getRow:()=>i+2};}})}),
    getValues:()=>[rows[row-2]],setValue:value=>{rows[row-2][col-1]=value;}
  })};
  const c={PropertiesService:{getScriptProperties:()=>({getProperty:key=>properties[key]})},
    Session:{getActiveUser:()=>({getEmail:()=>overrides.active || ''}),getEffectiveUser:()=>({getEmail:()=>owner})},
    SpreadsheetApp:{openById:()=>{accesses++;return {getSheetByName:name=>name==='_手入力受付'?sheet:{getRange:()=>({getDisplayValues:()=>[['食費','食品']]})}};},flush:()=>{}},
    LockService:{getScriptLock:()=>({waitLock:()=>{},releaseLock:()=>{}})},Utilities:{getUuid:()=>id,formatDate:()=> '2026-10-02'},
    UrlFetchApp:{fetch:url=>url.startsWith('https://oauth2.googleapis.com/')?{getResponseCode:()=>overrides.invalid?400:200,getContentText:()=>JSON.stringify(claims)}:{getResponseCode:()=>{dispatches++;return 204;}}},
    ContentService:{createTextOutput:text=>({setMimeType:()=>JSON.parse(text)}),MimeType:{JSON:'json'}}};
  vm.createContext(c);vm.runInContext(fs.readFileSync(path.join(root,'manual-entry/Code.gs'),'utf8'),c);
  const post=fields=>c.doPost({postData:{type:'application/json',contents:JSON.stringify({identityToken:'a'.repeat(100),action:'bootstrap',...fields})}});
  return {c,post,rows,accesses:()=>accesses,dispatches:()=>dispatches,properties};
}
for (const [name,options] of Object.entries({forged:{invalid:true},audience:{claims:{aud:'attacker'}},issuer:{claims:{iss:'attacker'}},expired:{claims:{exp:1}},future:{claims:{iat:9999999999}},unverified:{claims:{email_verified:false}},outsider:{claims:{email:'other@example.test'}},missingSubject:{claims:{sub:''}}})) {
  test('reject '+name+' before spreadsheet access',()=>{const b=backend(options);assert.equal(b.post({action:'submit',input:{id}}).ok,false);assert.equal(b.accesses(),0);assert.equal(b.dispatches(),0);});
}
test('missing token and unset configuration fail closed',()=>{const b=backend();assert.equal(b.post({identityToken:''}).ok,false);delete b.properties.MANUAL_IDENTITY_AUDIENCE;assert.equal(b.post({}).ok,false);assert.equal(b.accesses(),0);});
test('anonymous public RPC cannot use owner execution identity',()=>{const b=backend();for(const f of ['manualBootstrap','manualSubmit','manualCancel','manualStatus','installManualEntry','doGet'])assert.throws(()=>b.c[f]({id}),/管理者/);assert.equal(b.accesses(),0);});
test('verified spouse recorded by server; replay dispatches and appends once',()=>{
  const b=backend(); const input={id,date:'2026-10-02',amount:1,major:'食費',minor:'食品',entered_by:'attacker',created_at:'fake',manual_entry_id:'fake'};
  assert.equal(b.post({action:'submit',input}).ok,true);assert.equal(b.post({action:'submit',input}).ok,true);
  assert.equal(b.rows.length,1);assert.equal(b.dispatches(),1);
  const payload=JSON.parse(b.rows[0][2]);assert.equal(payload.entered_by,wife);assert.equal(payload.manual_entry_id,id);assert.equal(payload.created_at,b.rows[0][3]);assert.notEqual(payload.created_at,'fake');
});
test('legacy owner UI remains usable',()=>{const b=backend({active:owner});assert.equal(b.c.manualBootstrap().categories.length,1);});
test('rejected response never contains supplied token',()=>{const b=backend({invalid:true});assert.equal(JSON.stringify(b.post({})).includes('a'.repeat(100)),false);});
test('identity frontend has no Sheets or Drive scopes and reuses the current UI',()=>{
  const m=JSON.parse(fs.readFileSync(path.join(root,'manual-identity/appsscript.json'),'utf8'));
  assert.deepEqual(m.oauthScopes,['openid','https://www.googleapis.com/auth/userinfo.email','https://www.googleapis.com/auth/script.external_request']);
  const src=fs.readFileSync(path.join(root,'manual-identity/Code.gs'),'utf8');assert.match(src,/createHtmlOutputFromFile\('Index'\)/);assert.doesNotMatch(src,/SpreadsheetApp|MANUAL_GITHUB_TOKEN/);
});
test('identity frontend sends the server token only to the configured backend',()=>{
  let email=wife;const calls=[];const properties={MANUAL_HOUSEHOLD_EMAILS:JSON.stringify([owner,wife]),MANUAL_BACKEND_URL:'https://script.google.com/macros/s/deployment-id/exec'};
  const c={PropertiesService:{getScriptProperties:()=>({getProperty:key=>properties[key]})},
    Session:{getEffectiveUser:()=>({getEmail:()=>email})},ScriptApp:{getIdentityToken:()=> 'verified-server-token'},
    UrlFetchApp:{fetch:(url,options)=>{calls.push({url,options});return {getResponseCode:()=>200,getContentText:()=>JSON.stringify({ok:true,result:{id,state:'dispatching'}})};}}};
  vm.createContext(c);vm.runInContext(fs.readFileSync(path.join(root,'manual-identity/Code.gs'),'utf8'),c);
  const result=c.manualSubmit({id,identityToken:'client-forgery'});assert.equal(result.state,'dispatching');
  const sent=JSON.parse(calls[0].options.payload);assert.equal(sent.identityToken,'verified-server-token');
  assert.equal(JSON.stringify(result).includes('verified-server-token'),false);
  email='outsider@example.test';assert.throws(()=>c.manualBootstrap(),/アクセス権/);assert.equal(calls.length,1);
  email=wife;properties.MANUAL_BACKEND_URL='https://attacker.example/exec';assert.throws(()=>c.manualBootstrap(),/接続先/);assert.equal(calls.length,1);
});
