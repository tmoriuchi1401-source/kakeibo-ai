'use strict';
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname,'Code.gs'),'utf8');
const context = {module:{exports:{}}};
vm.runInNewContext(source,context);
const core = context.module.exports;
const uuid='11111111-1111-4111-8111-111111111111'; // Offline fixture, not a live UUID.
const other='22222222-2222-4222-8222-222222222222';
function fixture() {
  const config={enabled:true,bookId:'synthetic_book_0001',sheetId:10,logSheetId:11,runId:'GATE-B-TEST',
    ticket:{uuid,caseId:'SYN-GATEB-TEST',machine:['SYN-GATEB-TEST','synthetic_source','1','a'.repeat(64),'a','b','new:test0001',uuid,'GATE-B-TEST','b'.repeat(64)]}};
  const row=Array(46).fill('');row[0]=config.ticket.caseId;row.splice(36,10,...config.ticket.machine);
  const sentinel=Array(46).fill('');sentinel[0]='SYN-SORT';
  const state={bindings:[row,sentinel],history:[],writes:[],failAppend:false,failFlush:false,lockAvailable:true};
  const log={getSheetId:()=>11,getName:()=>'_gate_b_events',isSheetHidden:()=>true,getLastRow:()=>state.history.length+1,
    getRange:(r)=>({getValues:()=>r===1?[Array.from(core.RECEIPT_GATE_B_HEADERS)]:state.history}),
    appendRow:(r)=>{state.writes.push(r);if(state.failAppend)throw Error('private payload must not leak');state.history.push(r);}};
  const review={getSheetId:()=>10,getName:()=>'確認',isSheetHidden:()=>false,getParent:()=>book,
    getLastRow:()=>state.bindings.length+1,getRange:()=>({getValues:()=>state.bindings})};
  const book={getId:()=>config.bookId,getSheets:()=>[review,log]};
  const event={source:book,range:{getSheet:()=>review,getColumn:()=>9,getNumColumns:()=>1,getNumRows:()=>1,getRow:()=>2},
    triggerUid:'12345',value:'register:'+uuid,oldValue:'ready:'+uuid};
  Object.defineProperty(event,'user',{get(){throw Error('editor identity must not be read');}});
  const locks={getScriptLock:()=>({tryLock:()=>state.lockAvailable,releaseLock:()=>{}})};
  const sheets={flush:()=>{if(state.failFlush)throw Error('lost response');}};
  return {config,state,event,locks,sheets,run:()=>core.receiptGateBObserve_(event,config,locks,sheets)};
}
test('known UUID pair is observation only; no input or memo mutation',()=>{
  const f=fixture();f.state.bindings[0][7]='preserve memo';const before=JSON.stringify(f.state.bindings);
  assert.equal(f.run(),'OBSERVED_UNAPPROVED');assert.equal(f.state.writes.length,1);
  assert.equal(f.state.writes[0][11],'SYN-GATEB-TEST');assert.equal(JSON.stringify(f.state.bindings),before);
  assert.ok(!JSON.stringify(f.state.writes).includes('preserve memo'));
});
test('sort after event maps UUID to case, never current event row',()=>{
  const f=fixture();f.state.bindings.reverse();assert.equal(f.run(),'OBSERVED_UNAPPROVED');
  assert.equal(f.state.writes[0][11],'SYN-GATEB-TEST');
});
test('normal TRUE rejected and arbitrary old value redacted',()=>{
  const f=fixture();f.event.value='TRUE';f.event.oldValue='private@example.invalid';
  assert.equal(f.run(),'REJECT_EVENT_IDENTITY');assert.ok(!JSON.stringify(f.state.writes).includes('@'));
});
test('missing oldValue rejected',()=>{const f=fixture();delete f.event.oldValue;assert.equal(f.run(),'REJECT_EVENT_IDENTITY');});
test('different UUID rejected without storing unknown UUID',()=>{
  const f=fixture();f.event.value='register:'+other;f.event.oldValue='ready:'+other;
  assert.equal(f.run(),'REJECT_EVENT_IDENTITY');assert.ok(!JSON.stringify(f.state.writes).includes(other));
});
test('mismatched old UUID rejected',()=>{const f=fixture();f.event.oldValue='ready:'+other;assert.equal(f.run(),'REJECT_EVENT_IDENTITY');});
test('multi-cell edit crossing registration column rejected',()=>{
  const f=fixture();f.event.range.getColumn=()=>8;f.event.range.getNumColumns=()=>2;
  assert.equal(f.run(),'REJECT_MULTI_CELL');
});
test('outside column, other book, other sheet never write',()=>{
  for(const mode of ['column','book','sheet','range-book']){const f=fixture();
    if(mode==='column')f.event.range.getColumn=()=>20;
    if(mode==='book')f.event.source={getId:()=>'other_book'};
    if(mode==='sheet')f.event.range.getSheet=()=>({getSheetId:()=>99});
    if(mode==='range-book')f.event.range.getSheet=()=>({getSheetId:()=>10,getParent:()=>({getId:()=>'other_book'})});
    assert.equal(f.run(),null);assert.equal(f.state.writes.length,0);
  }
});
test('same UUID repeated is rejected; uncheck never re-arms',()=>{
  const f=fixture();assert.equal(f.run(),'OBSERVED_UNAPPROVED');assert.equal(f.run(),'REJECT_DUPLICATE');
  f.event.value='ready:'+uuid;f.event.oldValue='register:'+uuid;assert.equal(f.run(),'REJECT_EVENT_IDENTITY');
  f.event.value='register:'+uuid;f.event.oldValue='ready:'+uuid;assert.equal(f.run(),'REJECT_DUPLICATE');
});
test('duplicated, deleted, moved partial binding never selects another case',()=>{
  for(const mode of ['duplicate','delete','partial']){const f=fixture();
    if(mode==='duplicate')f.state.bindings.push([...f.state.bindings[0]]);
    if(mode==='delete')f.state.bindings.shift();
    if(mode==='partial')f.state.bindings[0][0]='SYN-WRONG-CASE';
    assert.equal(f.run(),'REJECT_CASE_BINDING');
  }
});
test('later input/memo edits survive event and replay checks',()=>{
  const f=fixture();f.state.bindings[0][20]=868;f.state.bindings[0][7]='later draft';
  const before=JSON.stringify(f.state.bindings);f.run();f.run();assert.equal(JSON.stringify(f.state.bindings),before);
});
test('unknown append response never retries and redacts error',()=>{
  const f=fixture();f.state.failAppend=true;assert.throws(f.run,/^Error: GATE_B_LOG_UNKNOWN_STOP$/);
  assert.equal(f.state.writes.length,1);
});
test('flush response loss never repeats append; later event rejects duplicate',()=>{
  const f=fixture();f.state.failFlush=true;assert.throws(f.run,/GATE_B_LOG_UNKNOWN_STOP/);assert.equal(f.state.writes.length,1);
  f.state.failFlush=false;assert.equal(f.run(),'REJECT_DUPLICATE');
});
test('busy script lock stops without write, no retry',()=>{
  const f=fixture();f.state.lockAvailable=false;assert.throws(f.run,/GATE_B_BUSY_STOP/);assert.equal(f.state.writes.length,0);
});
test('disabled config and missing trigger identity stop without write',()=>{
  const f=fixture();f.config.enabled=false;assert.throws(f.run,/GATE_B_DISABLED/);f.config.enabled=true;
  delete f.event.triggerUid;assert.throws(f.run,/GATE_B_INSTALLABLE_EVENT_REQUIRED/);assert.equal(f.state.writes.length,0);
});
test('unconfigured public template cannot access a live service',()=>{
  assert.throws(()=>core.receiptGateBTargets_({},{}),/GATE_B_NOT_CONFIGURED/);
  assert.ok(!source.includes('UrlFetchApp'));assert.ok(!source.includes('DriveApp'));
  const manifest=JSON.parse(fs.readFileSync(path.join(__dirname,'appsscript.json'),'utf8'));
  assert.deepEqual(manifest.oauthScopes,['https://www.googleapis.com/auth/spreadsheets.currentonly']);
  assert.equal(manifest.exceptionLogging,'NONE');
});
test('tampered log header or visible log stops before append',()=>{
  const f=fixture();const log=f.event.source.getSheets()[1];log.isSheetHidden=()=>false;
  assert.throws(f.run,/GATE_B_LAYOUT_CHANGED/);assert.equal(f.state.writes.length,0);
});
