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

// Stateful setup model: merge/freeze constraints and destructive merge semantics
// are intentional, so a mock cannot silently accept the original regression.
const marker = '■ 1. カテゴリを選ぶ・今後の自動分類';
const explanation = 'F列チェック＋上部の実行で、全月の未分類に反映。分類済みは変更せず、完了分は一覧から除きます。';
const queueHeaders = ['request_id','state','submitted_at','started_at','finished_at',
  'result','run_id','snapshot_rows','snapshot_sha256','version'];

function setupHarness({fresh=false, legacy=false, staged=false, saved=true, state='complete'}={}) {
  let mutation=0, failAt=0, triggerId=0, prompts=0, locked=false;
  const mutations=[], calls=[], logs=[], triggers=[];
  const scriptProps={CATEGORY_SPREADSHEET_ID:'synthetic-sheet'};
  const userProps=saved?{CATEGORY_GITHUB_TOKEN:'github_pat_SYNTHETIC',CATEGORY_SPREADSHEET_ID:'synthetic-sheet'}:{};
  if(staged) scriptProps.CATEGORY_GITHUB_TOKEN='github_pat_SYNTHETIC';
  const clone=v=>JSON.parse(JSON.stringify(v));
  function changed(name) {
    mutations.push(name);
    if(++mutation===failAt) throw Error('injected setup failure');
  }
  function bounds(a1) {
    const match=/^([A-Z]+)(\d+)(?::([A-Z]+)(\d+))?$/.exec(a1);
    assert.ok(match, a1);
    const col=s=>[...s].reduce((n,c)=>n*26+c.charCodeAt(0)-64,0);
    return {r:+match[2],c:col(match[1]),er:+(match[4]||match[2]),ec:col(match[3]||match[1])};
  }
  function sheet(name) {
    const cells=new Map(), merges=new Set();
    const s={name,cells,merges,frozenRows:4,frozenColumns:1,inserted:0,hidden:false};
    s.cell=(r,c)=>{const key=r+':'+c;if(!cells.has(key))cells.set(key,{});return cells.get(key);};
    s.put=(a1,data)=>{const b=bounds(a1);Object.assign(s.cell(b.r,b.c),clone(data));};
    s.snapshot=()=>clone([...cells].filter(([,v])=>Object.keys(v).length).sort());
    s.getRange=a1=> {
      const b=bounds(a1), each=fn=>{for(let r=b.r;r<=b.er;r++)for(let c=b.c;c<=b.ec;c++)fn(s.cell(r,c),r,c);};
      const rng={
        getA1Notation:()=>a1, getNumColumns:()=>b.ec-b.c+1,
        getCell:(r,c)=>s.getRange(String.fromCharCode(64+b.c+c-1)+(b.r+r-1)),
        getValue:()=>s.cell(b.r,b.c).value??'',
        getFormula:()=>s.cell(b.r,b.c).formula||'',
        getNote:()=>s.cell(b.r,b.c).note||'',
        getDataValidation:()=>s.cell(b.r,b.c).validation||null,
        isBlank:()=>{let empty=true;each(cell=>{if(cell.formula || (cell.value!==undefined && cell.value!==''))empty=false;});return empty;},
        getValues:()=>Array.from({length:b.er-b.r+1},(_,r)=>Array.from({length:b.ec-b.c+1},(_,c)=>s.cell(b.r+r,b.c+c).value??'')),
        getMergedRanges:()=>[...merges].filter(m=>{const q=bounds(m);return b.r<=q.er&&b.er>=q.r&&b.c<=q.ec&&b.ec>=q.c;}).map(m=>s.getRange(m)),
        isPartOfMerge:()=>rng.getMergedRanges().length>0,
        setValue:v=>{each(cell=>{cell.value=v;delete cell.formula;delete cell.link;});changed(name+':'+a1+':setValue');return rng;},
        setValues:values=>{each((cell,r,c)=>{cell.value=values[r-b.r][c-b.c];delete cell.formula;delete cell.link;});changed(name+':'+a1+':setValues');return rng;},
        setDataValidation:v=>{each(cell=>cell.validation=clone(v));changed(name+':'+a1+':validation');return rng;},
        copyTo:dest=>{dest.replace(clone(s.cell(b.r,b.c)));changed(name+':'+a1+':copyTo');},
        replace:v=>{const cell=s.cell(b.r,b.c);for(const k of Object.keys(cell))delete cell[k];Object.assign(cell,v);},
        merge:()=>{
          assert.equal(rng.isPartOfMerge(),false,'must not merge overlapping ranges');
          assert.ok(!(b.c<=s.frozenColumns && b.ec>s.frozenColumns),'merge crosses frozen column');
          assert.ok(!(b.r<=s.frozenRows && b.er>s.frozenRows),'merge crosses frozen row');
          each((cell,r,c)=>{if(r!==b.r||c!==b.c){for(const k of Object.keys(cell))delete cell[k];}});
          merges.add(a1);changed(name+':'+a1+':merge');return rng;
        },
        breakApart:()=>{for(const m of rng.getMergedRanges())merges.delete(m.getA1Notation());changed(name+':'+a1+':breakApart');return rng;}
      };
      for(const method of ['setWrap','setVerticalAlignment','setFontSize','setFontColor','setFontWeight','setBackground','setHorizontalAlignment'])
        rng[method]=()=>{changed(name+':'+a1+':'+method);return rng;};
      return rng;
    };
    s.insertRowsBefore=(_,n)=>{const old=[...cells];cells.clear();for(const [key,v] of old){const [r,c]=key.split(':').map(Number);cells.set((r+n)+':'+c,v);}s.inserted+=n;changed(name+':insertRows');};
    s.getFrozenRows=()=>s.frozenRows;s.getFrozenColumns=()=>s.frozenColumns;
    for(const [method,key] of [['setFrozenRows','frozenRows'],['setFrozenColumns','frozenColumns']])
      s[method]=n=>{assert.ok(n>0,'unfreezing forbidden');for(const m of merges){const b=bounds(m);assert.ok(key==='frozenRows'?!(b.r<=n&&b.er>n):!(b.c<=n&&b.ec>n));}s[key]=n;changed(name+':'+method);};
    s.setRowHeights=s.setRowHeight=()=>changed(name+':rowHeight');
    s.hideSheet=()=>{s.hidden=true;changed(name+':hideSheet');};
    return s;
  }
  const surface=sheet('カテゴリ操作');
  surface.put(fresh?'A1':'A6',{value:marker});
  surface.put(fresh?'C3':'C8',{value:'synthetic draft',note:'retain',link:'https://example.invalid/draft'});
  surface.put(fresh?'D3':'D8',{formula:'=1+2',value:3});
  surface.put(fresh?'F3':'F8',{value:true,validation:{checkbox:true}});
  let queue=fresh?null:sheet('_カテゴリ実行受付');
  if(queue) {
    queue.getRange('A1:J1').setValues([queueHeaders]);
    queue.getRange('A2:J2').setValues([['synthetic-request',state,'date','start','end','done','run',1,'digest','1']]);
    queue.put('A4',{value:'["synthetic captured input"]'});
    surface.put('A1',{value:'入力内容を処理する'});
    surface.put('B1',{value:true,validation:{checkbox:true}});
    surface.put('C1',{value:'完了'});
    surface.put('A2',{value:'処理結果'});
    surface.put('B2',{formula:'="retained result"',value:'retained result',link:'https://example.invalid/result'});
    surface.put('A3',{value:'最終更新'});
    surface.put('B3',{value:'retained timestamp'});
    surface.put('A4',{value:legacy?explanation:'操作の説明'});
    if(legacy) surface.merges.add('A4:F4');
    else surface.put('B4',{value:'custom explanation',link:'https://example.invalid/help'});
    for(const a1 of ['C1:F1','B2:F2','B3:F3'])surface.merges.add(a1);
  }
  const ss={getId:()=> 'synthetic-sheet',getSheetByName:n=>n==='カテゴリ操作'?surface:n==='_カテゴリ実行受付'?queue:null,
    insertSheet:()=>{assert.equal(queue,null);queue=sheet('_カテゴリ実行受付');changed('insertSheet');return queue;}};
  function props(data) {return {
    getProperty:key=>data[key]??null,
    setProperty:(key,v)=>{data[key]=v;changed('property:'+key);},
    setProperties:values=>{Object.assign(data,values);changed('properties');},
    deleteProperty:key=>{delete data[key];changed('deleteProperty:'+key);}
  };}
  function addTrigger(handler,event,sourceId) {
    const t={id:++triggerId,getHandlerFunction:()=>handler,getEventType:()=>event,getTriggerSourceId:()=>sourceId};triggers.push(t);return t;
  }
  const context=vm.createContext({
    SpreadsheetApp:{getActive:()=>ss,openById:()=>ss,flush:()=>changed('flush'),
      newDataValidation:()=>({requireCheckbox(){return this;},build:()=>({checkbox:true})}),
      getUi:()=>{prompts++;throw Error('unexpected credential prompt');}},
    PropertiesService:{getScriptProperties:()=>props(scriptProps),getUserProperties:()=>props(userProps)},
    LockService:{getScriptLock:()=>({tryLock:()=>{if(locked)return false;locked=true;return true;},releaseLock:()=>{locked=false;}})},
    ScriptApp:{EventType:{ON_EDIT:'edit',CLOCK:'clock'},getProjectTriggers:()=>triggers,
      deleteTrigger:()=>{throw Error('must retain existing trigger IDs');},
      newTrigger:handler=>{let event,sourceId;return {
        forSpreadsheet(){sourceId=ss.getId();return this;},onEdit(){event='edit';return this;},
        timeBased(){event='clock';return this;},everyMinutes(n){assert.equal(n,5);return this;},
        create(){const t=addTrigger(handler,event,sourceId);changed('trigger:'+handler);return t;}
      };}},
    UrlFetchApp:{fetch:(url,options)=>{assert.equal(options.method,'get','setup must never dispatch');calls.push({url,method:options.method});return {getResponseCode:()=>200};}},
    console:{log:v=>logs.push(v)},JSON
  });
  vm.runInContext(source,context);
  mutation=0;mutations.length=0;
  return {context,surface,get queue(){return queue;},triggers,userProps,scriptProps,calls,logs,mutations,addTrigger,
    arm:n=>{mutation=0;failAt=n;}, get prompts(){return prompts;}, get locked(){return locked;}};
}

test('existing installation preserves cells, records, settings and trigger IDs over two runs',()=>{
  const h=setupHarness();
  h.addTrigger('categorySubmitEdited','edit','synthetic-sheet');
  h.addTrigger('categoryCheckActive','clock');
  h.addTrigger('unrelated','edit','synthetic-sheet');
  const before=h.surface.snapshot(), queue=h.queue.snapshot(), props={...h.userProps}, ids=h.triggers.map(t=>t.id);
  h.context.configureCategorySubmit();h.context.configureCategorySubmit();
  assert.deepEqual(h.surface.snapshot(),before);
  assert.deepEqual(h.queue.snapshot(),queue);assert.deepEqual(h.userProps,props);
  assert.deepEqual(h.triggers.map(t=>t.id),ids);
  assert.ok(h.surface.merges.has('B4:F4'));assert.ok(!h.surface.merges.has('A4:F4'));
  assert.equal(h.surface.frozenRows,4);assert.equal(h.surface.frozenColumns,1);
  assert.equal(h.surface.inserted,0);assert.equal(h.calls.length,0);assert.equal(h.prompts,0);
});

test('known legacy merge moves its rich explanation while retaining the heading and freezes',()=>{
  const h=setupHarness({legacy:true});
  h.surface.put('A4',{link:'https://example.invalid/legacy',note:'legacy note'});
  h.context.installCategorySubmit();h.context.installCategorySubmit();
  assert.equal(h.surface.getRange('A4').getValue(),'操作の説明');
  assert.deepEqual(h.surface.cell(4,2),{value:explanation,link:'https://example.invalid/legacy',note:'legacy note'});
  assert.ok(h.surface.merges.has('B4:F4'));assert.equal(h.triggers.length,2);
});

test('unsafe merges, custom legacy text and occupied trailing cells fail without changes',()=>{
  for(const conflict of ['formula','value','link','note','validation','unexpectedMerge','customLegacy']) {
    const h=setupHarness({legacy:conflict==='customLegacy'});
    if(conflict==='unexpectedMerge')h.surface.merges.add('A3:B3');
    else if(conflict==='customLegacy')h.surface.put('A4',{value:'custom legacy input'});
    else h.surface.put('D4',conflict==='formula'?{formula:'=""',value:''}:conflict==='link'?{value:'link',link:'https://example.invalid'}:
      conflict==='note'?{note:'preserve'}:conflict==='validation'?{validation:{checkbox:true}}:{value:false});
    const before=h.surface.snapshot(),queue=h.queue.snapshot(),merges=[...h.surface.merges];
    assert.throws(()=>h.context.installCategorySubmit());
    assert.deepEqual(h.surface.snapshot(),before);assert.deepEqual(h.queue.snapshot(),queue);
    assert.deepEqual([...h.surface.merges],merges);assert.equal(h.mutations.length,0);assert.equal(h.locked,false);
  }
});

test('busy states and duplicate matching triggers fail before any setup mutation',()=>{
  for(const state of ['dispatching','accepted','running']) {
    const h=setupHarness({state});assert.throws(()=>h.context.installCategorySubmit());assert.equal(h.mutations.length,0);
  }
  const h=setupHarness();h.addTrigger('categorySubmitEdited','edit','synthetic-sheet');h.addTrigger('categorySubmitEdited','edit','synthetic-sheet');
  assert.throws(()=>h.context.installCategorySubmit());assert.equal(h.mutations.length,0);
});

test('blank-result formulas in queue headers and mismatched saved targets are never overwritten',()=>{
  const h=setupHarness();h.queue.put('A1',{value:'',formula:'=""'});
  const before=h.queue.snapshot();
  assert.throws(()=>h.context.installCategorySubmit());assert.deepEqual(h.queue.snapshot(),before);
  assert.equal(h.mutations.length,0);
  const other=setupHarness();other.userProps.CATEGORY_SPREADSHEET_ID='other-synthetic-sheet';
  assert.throws(()=>other.context.installCategorySubmit());assert.equal(other.mutations.length,0);
});

test('fresh install and every applied-mutation failure recover without double rows, records or triggers',t=>{
  let faultPoints=0;
  for(const options of [{fresh:true},{legacy:true},{}]) {
    const baseline=setupHarness(options);baseline.context.installCategorySubmit();
    const expected=baseline.surface.snapshot(),queue=baseline.queue.snapshot(),count=baseline.mutations.length;
    faultPoints+=count;
    baseline.context.installCategorySubmit();
    assert.deepEqual(baseline.surface.snapshot(),expected);assert.deepEqual(baseline.queue.snapshot(),queue);
    for(let failure=1;failure<=count;failure++) {
      const h=setupHarness(options);h.arm(failure);
      assert.throws(()=>h.context.installCategorySubmit(),/injected setup failure/);
      assert.equal(h.locked,false);
      h.arm(0);h.context.installCategorySubmit();h.context.installCategorySubmit();
      assert.deepEqual(h.surface.snapshot(),expected,'surface after failure '+failure);
      assert.deepEqual(h.queue.snapshot(),queue,'queue after failure '+failure);
      assert.equal(h.surface.inserted,options.fresh?5:0);assert.equal(h.triggers.length,2);
      assert.equal(h.surface.frozenRows,4);assert.equal(h.surface.frozenColumns,1);
      assert.equal(h.calls.length,0);assert.equal(h.prompts,0);
    }
  }
  t.diagnostic('Recovered from '+faultPoints+' applied-mutation fault points, with two retries each.');
});

test('trigger matching preserves unrelated handlers and edit triggers for other spreadsheets',()=>{
  const h=setupHarness();
  const other=h.addTrigger('categorySubmitEdited','edit','other-synthetic-sheet');
  const unrelated=h.addTrigger('unrelated','clock');
  h.context.installCategorySubmit();h.context.installCategorySubmit();
  assert.ok(h.triggers.includes(other));assert.ok(h.triggers.includes(unrelated));
  assert.equal(h.triggers.length,4);
});

test('a migrated token survives installation failure and retry uses User Properties',()=>{
  const h=setupHarness({staged:true,saved:false});
  // Credential migration succeeds before the injected installation failure.
  h.arm(4);assert.throws(()=>h.context.configureCategorySubmit(),/injected setup failure/);
  assert.equal(Boolean(h.userProps.CATEGORY_GITHUB_TOKEN),true);
  assert.equal(Boolean(h.scriptProps.CATEGORY_GITHUB_TOKEN),false);
  h.arm(0);h.context.configureCategorySubmit();h.context.configureCategorySubmit();
  assert.equal(h.prompts,0);assert.equal(h.calls.length,1);assert.equal(h.triggers.length,2);
});

test('read-only diagnostics reveal only booleans/counts for each token storage state',()=>{
  for(const staged of [false,true])for(const saved of [false,true]) {
    const h=setupHarness({staged,saved});const before=[{...h.scriptProps},{...h.userProps}];
    const status=h.context.inspectCategorySubmitSetup();
    assert.equal(status.stagedTokenPresent,staged);assert.equal(status.userTokenPresent,saved);
    assert.ok(Object.values(status).every(v=>typeof v==='boolean'||typeof v==='number'));
    assert.deepEqual([{...h.scriptProps},{...h.userProps}],before);
    assert.equal(h.mutations.length,0);assert.equal(h.calls.length,0);
    assert.ok(h.logs.every(line=>!line.includes('github_pat_')&&!line.includes('synthetic-sheet')));
  }
});
