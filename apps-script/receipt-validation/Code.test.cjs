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
  const snapshot = clone(ctx.receiptSnapshot_(candidate,hash));
  const prepared = {request_id:RID,purpose:'receipt-sheet-validation-only-v1',snapshot:snapshot,state:'下書き準備済み'};
  const state = {candidate:clone(candidate),prepared:prepared,requests:[],sent:[],fault:null,concurrent:false,displayFault:false};
  state.ports = {
    hash,isRegisterCell:r=>r === 'synthetic-register-cell',
    readPrepared:()=>clone(prepared),
    readCaseById:key=>{
      assert.equal(key,prepared.snapshot.case_id);
      return clone(state.candidate);
    },
    acceptPrepared:(rid,digest)=>{
      if (state.concurrent) throw Error('CAS');
      assert.equal(rid,RID);assert.equal(digest,snapshot.digest);
      if (prepared.state !== '下書き準備済み') throw Error('replay');
      prepared.state='受付済み';
      const request={request_id:RID,purpose:prepared.purpose,snapshot:clone(snapshot)};
      state.requests.push(request); // ONLY a private CAS, no Sheets transaction
      return clone(prepared);
    },
    publishReceipt:()=>{if(state.displayFault) throw Error('display unavailable');},
    dispatch:payload=>{
      state.sent.push(clone(payload));
      if (state.fault) throw Error('lost response');
      return state.status || 204;
    }
  };
  return state;
}
const event = {range:'synthetic-register-cell',value:'register:'+RID,oldValue:'ready:'+RID,user:{email:'unverified@synthetic.invalid'}};
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
    assert.throws(()=>ctx.receiptValidationEdited(event,h.ports),/replay/);
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
    assert.throws(()=>ctx.receiptValidationEdited(event,h.ports),/replay/);
    assert.equal(h.sent.length,1);
  });
  test('CAS or same-column sort prevents send',()=>{
    const h=harness();h.concurrent=true;
    assert.equal(ctx.receiptValidationEdited(event,h.ports).state,'結果不明・照合待ち');
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
    const h=harness();
    assert.throws(()=>ctx.receiptValidationEdited({...event,value:'register:$(secret)'},h.ports),/identity_unproven/);
    assert.equal(h.requests.length,0);
  });
  test('plain TRUE or missing event identity stops even after a delayed row sort',()=>{
    const h=harness();h.candidate.case_id='other-case-after-sort';
    for(const e of [{...event,value:'TRUE'},{...event,oldValue:undefined}])
      assert.throws(()=>ctx.receiptValidationEdited(e,h.ports),/identity_unproven/);
    assert.equal(h.requests.length,0);assert.equal(h.sent.length,0);
  });
  test('sort between edit and handler never reads the stale event row',()=>{
    const h=harness();
    const rows=[h.candidate,{case_id:'another-case-after-sort'}];
    rows.reverse(); // The saved event coordinate now points at another case.
    assert.equal(rows[0].case_id,'another-case-after-sort');
    h.ports.readCaseAtEvent=()=>clone(rows[0]);
    h.ports.readCaseById=key=>clone(rows.find(row=>row.case_id===key));
    ctx.receiptValidationEdited(event,h.ports);
    assert.equal(h.requests[0].snapshot.case_id,'synthetic-case');
  });
  test('private save success and Sheets publication failure keeps the receipt',()=>{
    const h=harness();h.displayFault=true;
    const result=ctx.receiptValidationEdited(event,h.ports);
    assert.equal(result.display_pending,true);assert.equal(result.state,'受付済み');
    assert.equal(h.prepared.state,'受付済み');assert.equal(h.sent.length,1);
    assert.throws(()=>ctx.receiptValidationEdited(event,h.ports),/replay/);
  });
  test('private acceptance success and dispatch failure never unlocks or resends',()=>{
    const h=harness();h.status=403;
    assert.equal(ctx.receiptValidationEdited(event,h.ports).state,'結果不明・照合待ち');
    assert.equal(h.prepared.state,'受付済み');
    assert.throws(()=>ctx.receiptValidationEdited(event,h.ports),/replay/);
    assert.equal(h.sent.length,1);
  });
  test('preparation publishes no checkbox on private-save uncertainty and never dispatches',()=>{
    const h=harness();let prepared;
    const ports={hash,uuid:()=>RID,prepare:r=>{prepared=clone(r);}};
    const rule=ctx.receiptPrepareDraft(h.candidate,ports);
    assert.equal(rule.checkedValue,'register:'+RID);
    assert.equal(rule.uncheckedValue,'ready:'+RID);
    assert.equal(prepared.snapshot.digest,h.prepared.snapshot.digest);
    assert.equal(h.sent.length,0);
    ports.prepare=()=>{throw Error('private save unknown');};
    assert.throws(()=>ctx.receiptPrepareDraft(h.candidate,ports),/private save unknown/);
  });
}
