const {readFileSync} = require('node:fs');
const vm = require('node:vm');
const {createHash} = require('node:crypto');
const assert = require('node:assert/strict');
const test = require('node:test');
const source = readFileSync(__dirname + '/Code.gs','utf8');
const ctx = vm.createContext({});
vm.runInContext(source,ctx);
const RID = '11111111-1111-4111-8111-111111111111';
const hash = s => createHash('sha256').update(s).digest('hex');
const clone = x => JSON.parse(JSON.stringify(x));
const original = {source_id:'f'.repeat(33),sha256:'e'.repeat(64),version:'7',mime_type:'image/png',classification:'normal',transaction_kind:'purchase'};
const text = '案件｜synthetic-case\n原本｜' + original.source_id + '｜' + original.sha256 + '｜7｜image/png\n基準｜' + 'a'.repeat(64) +
  '\n変更｜synthetic-item｜｜2｜868｜｜食費｜食品｜｜原本を確認済み';
function harness(candidate = {case_id:'synthetic-case',source:original,draft:text}) {
  const state = {candidate:clone(candidate),requests:[],sent:[],fault:null,concurrent:false};
  state.ports = {
    hash,uuid:()=>RID,isRegisterCell:r=>r === 'synthetic-register-cell',
    readCaseAtEvent:()=>clone(state.candidate),
    reserve:(request,seen)=>{
      if (state.concurrent || JSON.stringify(seen) !== JSON.stringify(state.candidate)) throw Error('CAS');
      if (state.requests.length) throw Error('duplicate');
      state.requests.push(clone(request)); // fake atomic private-store reservation
    },
    dispatch:payload=>{
      state.sent.push(clone(payload));
      if (state.fault) throw Error('lost response');
      return 204;
    }
  };
  return state;
}
const event = {range:'synthetic-register-cell',value:'TRUE',user:{email:'unverified@synthetic.invalid'}};
if (process.argv.includes('--capture')) {
  const h = harness(JSON.parse(readFileSync(0,'utf8')));
  ctx.receiptValidationEdited(event,h.ports);
  process.stdout.write(JSON.stringify(h.requests[0]));
} else {
  test('one explicit registration sends UUID only; editor never asserted',()=>{
    const h=harness();
    const result=ctx.receiptValidationEdited(event,h.ports);
    assert.equal(result.state,'受付済み');
    assert.deepEqual(h.sent,[{inputs:{request_id:RID}}]);
    assert.deepEqual(Object.keys(h.requests[0]).sort(),['purpose','request_id','snapshot']);
    assert.equal(h.requests[0].snapshot.operations[0].changes.amount,868);
    assert.throws(()=>ctx.receiptValidationEdited(event,h.ports),/duplicate/);
    assert.equal(h.sent.length,1);
  });
  test('ordinary edits and wrong checkbox do not submit',()=>{
    const h=harness();
    ctx.receiptValidationEdited({...event,value:'FALSE'},h.ports);
    ctx.receiptValidationEdited({...event,range:'other'},h.ports);
    assert.equal(h.sent.length,0);
  });
  test('missing live ports fail closed and there is no installer',()=>{
    assert.throws(()=>ctx.receiptValidationEdited(event),/not_connected/);
    assert.equal(/ScriptApp|PropertiesService|UrlFetchApp|SpreadsheetApp/.test(source),false);
  });
  test('network unknown retains request and never sends again',()=>{
    const h=harness();h.fault=true;
    assert.equal(ctx.receiptValidationEdited(event,h.ports).state,'結果不明・照合待ち');
    assert.equal(h.requests.length,1);
    assert.throws(()=>ctx.receiptValidationEdited(event,h.ports),/duplicate/);
    assert.equal(h.sent.length,1);
  });
  test('CAS or same-column sort prevents send',()=>{
    const h=harness();h.concurrent=true;
    assert.throws(()=>ctx.receiptValidationEdited(event,h.ports),/CAS/);
    assert.equal(h.sent.length,0);
    h.concurrent=false;h.candidate.case_id='other-case';
    assert.throws(()=>ctx.receiptValidationEdited(event,h.ports),/case_mismatch/);
    assert.equal(h.sent.length,0);
  });
  test('source version mismatch prevents send',()=>{
    const h=harness();h.candidate.source.version='8';
    assert.throws(()=>ctx.receiptValidationEdited(event,h.ports),/case_mismatch/);
    assert.equal(h.requests.length,0);
  });
  test('source/item/identity strings never enter dispatch payload',()=>{
    const h=harness();ctx.receiptValidationEdited(event,h.ports);
    const serialized=JSON.stringify(h.sent);
    for(const word of ['synthetic-case','synthetic-item','確認済み',original.source_id,original.sha256,'unverified'])
      assert.equal(serialized.includes(word),false);
  });
  test('invalid draft fields and UUID are rejected before reservation',()=>{
    for(const replacement of ['NaN','Infinity','1e2','-1']) {
      const h=harness();h.candidate.draft=text.replace('｜2｜868','｜'+replacement+'｜868');
      assert.throws(()=>ctx.receiptValidationEdited(event,h.ports));
      assert.equal(h.requests.length,0);
    }
    const h=harness();h.ports.uuid=()=> '$(secret)';
    assert.throws(()=>ctx.receiptValidationEdited(event,h.ports),/uuid_invalid/);
    assert.equal(h.requests.length,0);
  });
}
