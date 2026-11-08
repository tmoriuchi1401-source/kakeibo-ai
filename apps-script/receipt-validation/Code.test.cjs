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
function preparationHarness(candidate = {case_id:'synthetic-case',source:original,draft:text}, tag='"0"') {
  const state={candidate:clone(candidate),tag,trace:[],writes:0,request:null,options:null,fault:null};
  const journal={schema:'receipt-sheet-validation-journal-v2',generation:0,requests:{},active_prepared:{}};
  state.ports={
    readVersioned:()=>{state.trace.push('etag');return [clone(journal),state.tag];},
    readCaseById:caseId=>{state.trace.push('input');assert.equal(caseId,candidate.case_id);
      if(state.onRead)state.onRead();return clone(state.candidate);},
    hash:s=>{state.trace.push('snapshot');return hash(s);},
    uuid:()=>{state.trace.push('fixture_uuid');return RID;},
    prepare:(request,options)=>{
      state.trace.push('prepare');
      if(!options || Object.keys(options).join()!=='expected_tag' || options.expected_tag!==state.tag)
        throw Error('receipt_validation_preparation_stale');
      if(journal.requests[request.request_id])throw Error('receipt_validation_replay_rejected');
      if(state.fault==='before')throw Error('private save unknown');
      state.trace.push('cas');state.writes++;
      journal.generation++;
      journal.requests[request.request_id]={...clone(request),state:'下書き準備済み',result:null};
      journal.active_prepared[request.snapshot.case_id]=request.request_id;
      state.request=clone(request);state.options=clone(options);state.tag='"'+journal.generation+'"';
      if(state.fault==='after')throw Error('private save unknown');
      state.trace.push('readback');
      if(state.fault==='readback')throw Error('private readback unknown');
      assert.deepEqual(journal.requests[RID].snapshot,clone(request.snapshot));
    }
  };
  return state;
}
const event = {range:'synthetic-register-cell',value:'register:'+RID,oldValue:'ready:'+RID,user:{email:'unverified@synthetic.invalid'}};
if (process.argv.includes('--capture')) {
  const h = harness(JSON.parse(readFileSync(0,'utf8')));
  ctx.receiptValidationEdited(event,h.ports);
  process.stdout.write(JSON.stringify(h.requests[0]));
} else if (process.argv.includes('--prepare')) {
  const input=JSON.parse(readFileSync(0,'utf8'));
  const h=preparationHarness(input.candidate,input.tag);
  let rule=null,error=null;
  try {rule=ctx.receiptPrepareDraft(input.candidate.case_id,h.ports);}catch(e){error=e.message;}
  process.stdout.write(JSON.stringify({request:h.request,options:h.options,trace:h.trace,writes:h.writes,rule,error}));
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
    const h=preparationHarness();
    const rule=ctx.receiptPrepareDraft(h.candidate.case_id,h.ports);
    assert.equal(rule.checkedValue,'register:'+RID);
    assert.equal(rule.uncheckedValue,'ready:'+RID);
    assert.equal(h.request.snapshot.digest,harness().prepared.snapshot.digest);
    assert.deepEqual(h.options,{expected_tag:'"0"'});
    assert.deepEqual(h.trace,['etag','input','snapshot','fixture_uuid','prepare','cas','readback']);
    for(const fault of ['before','after','readback']){
      const f=preparationHarness();f.fault=fault;
      assert.throws(()=>ctx.receiptPrepareDraft(f.candidate.case_id,f.ports),/unknown/);
      assert.equal(f.writes,fault==='before'?0:1);
      assert.equal(f.trace.filter(x=>x==='prepare').length,1);
    }
  });
  test('missing or stale preparation ETag is rejected before save without reload',()=>{
    for(const tag of [undefined,null,'W/"0"','unquoted','""','"0"\n','"0"\r']){
      const h=preparationHarness();h.ports.readVersioned=()=>[{},tag];
      assert.throws(()=>ctx.receiptPrepareDraft(h.candidate.case_id,h.ports),/strong_cas/);
      assert.equal(h.writes,0);assert.deepEqual(h.trace,[]);
    }
    const h=preparationHarness();
    assert.throws(()=>h.ports.prepare({request_id:RID}),/preparation_stale/);
    h.trace=[];h.onRead=()=>{h.tag='"concurrent"';};
    assert.throws(()=>ctx.receiptPrepareDraft(h.candidate.case_id,h.ports),/preparation_stale/);
    assert.equal(h.writes,0);assert.equal(h.trace.filter(x=>x==='etag').length,1);
    assert.throws(()=>ctx.receiptPrepareDraft(h.candidate,h.ports),/case_invalid/);
  });
  test('preparation rejects retargeted input and asynchronous completion',()=>{
    const h=preparationHarness();h.candidate.case_id='other-case';
    assert.throws(()=>ctx.receiptPrepareDraft('synthetic-case',h.ports),/case_mismatch/);
    assert.equal(h.writes,0);assert.ok(!h.trace.includes('fixture_uuid'));
    const f=preparationHarness();f.ports.prepare=()=>({then:()=>{}});
    assert.throws(()=>ctx.receiptPrepareDraft(f.candidate.case_id,f.ports),/async_forbidden/);
  });
  test('receipt quantity and name boundaries are explicit and never rounded or folded',()=>{
    for(const value of [2,2.0,0.125,0.001,9999999.999])assert.equal(ctx.receiptQuantity_(value),value);
    for(const value of [0.00001,0.0000001,1e21,0,-1,NaN,Infinity,true,0.1234,10000000,2.0000000000000004])
      assert.throws(()=>ctx.receiptQuantity_(value),/quantity_invalid/);
    assert.equal(ctx.receiptName_(' \t商品🍎\r\n'),'商品🍎');
    assert.equal(ctx.receiptName_('\u3000日本語\u00a0'),'日本語');
    assert.equal(ctx.receiptName_('👩‍💻'),'👩‍💻');
    assert.notEqual(ctx.receiptName_('é'),ctx.receiptName_('e\u0301'));
    for(const value of ['\u0085商品','商品\ufeff','商品\u0000','\ud800','  '])
      assert.throws(()=>ctx.receiptName_(value),/name_invalid/);
  });
}
