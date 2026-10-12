// Installed Chrome through its standard CDP pipe; Node built-ins only.
const fs=require('node:fs');
const {spawn}=require('node:child_process');
const assert=require('node:assert/strict');
const path=require('node:path');
let input='';process.stdin.setEncoding('utf8');process.stdin.on('data',s=>input+=s);
process.stdin.on('end',()=>main(JSON.parse(input)).catch(()=>{console.log('visual verification failed; no secrets logged');process.exitCode=1;}));

async function main(c){
  const browser=spawn(c.chrome,['--headless=new','--remote-debugging-pipe','--user-data-dir='+c.profile,
    '--disable-background-networking','--disable-component-update','--disable-default-apps','--disable-sync',
    '--no-first-run','--no-default-browser-check','--disable-domain-reliability','--metrics-recording-only',
    '--disable-client-side-phishing-detection','--safebrowsing-disable-auto-update',
    '--disable-features=OptimizationHints,MediaRouter,Translate,AutofillServerCommunication,NetworkTimeServiceQuerying',
    '--proxy-server='+c.proxy,'--proxy-bypass-list=127.0.0.1',
    '--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1',
    '--ignore-certificate-errors-spki-list='+c.spki,'--log-net-log='+c.netlog,'about:blank'],
    {stdio:['ignore','ignore','pipe','pipe','pipe']});
  // Chrome's sandbox and normal origin/cookie security stay enabled.
  browser.stderr.on('data',()=>{});
  let seq=0,buffer='';const pending=new Map(),waiters=[],network=[];
  let session, documentStatus;
  const call=(method,params={},sid=session)=>new Promise((resolve,reject)=>{
    const id=++seq,timer=setTimeout(()=>{pending.delete(id);reject(new Error('CDP timeout'));},20000);
    pending.set(id,{resolve:r=>{clearTimeout(timer);resolve(r);},reject:e=>{clearTimeout(timer);reject(e);}});
    browser.stdio[3].write(JSON.stringify({id,method,params,...(sid?{sessionId:sid}:{})})+'\0');
  });
  const wait=(method)=>new Promise((resolve,reject)=>{
    const timer=setTimeout(()=>reject(new Error('browser event timeout')),20000);
    waiters.push({method,resolve:p=>{clearTimeout(timer);resolve(p);}});
  });
  browser.stdio[4].on('data',chunk=>{
    buffer+=chunk.toString();let end;
    while((end=buffer.indexOf('\0'))>=0){
      const raw=buffer.slice(0,end);buffer=buffer.slice(end+1);if(!raw)continue;
      const m=JSON.parse(raw);
      if(m.id){const p=pending.get(m.id);if(p){pending.delete(m.id);m.error?p.reject(new Error('CDP command rejected')):p.resolve(m.result);}continue;}
      const i=waiters.findIndex(w=>w.method===m.method);if(i>=0)waiters.splice(i,1)[0].resolve(m.params);
      if(m.method==='Network.requestWillBeSent'){
        const u=new URL(m.params.request.url);if(['http:','https:','ws:','wss:'].includes(u.protocol)&&u.hostname!=='127.0.0.1')network.push('non-loopback request');
      }
      if(m.method==='Network.responseReceived'&&m.params.type==='Document')documentStatus=m.params.response.status;
      if(m.method==='Fetch.requestPaused'){
        const u=new URL(m.params.request.url),ok=u.origin===c.base;
        if(!ok)network.push('non-localhost request intercepted');
        call(ok?'Fetch.continueRequest':'Fetch.failRequest',{requestId:m.params.requestId,...(ok?{}:{errorReason:'BlockedByClient'})}).catch(()=>{});
      }
    }
  });
  browser.on('error',()=>{for(const p of pending.values())p.reject(new Error('existing browser could not start'));});
  browser.on('exit',()=>{for(const p of pending.values())p.reject(new Error('browser exited'));});
  const evaluate=async expression=>{
    const r=await call('Runtime.evaluate',{expression,returnByValue:true,awaitPromise:true});
    assert(!r.exceptionDetails,'DOM evaluation failed');return r.result.value;
  };
  const resize=async(w,h)=>{
    await call('Emulation.setDeviceMetricsOverride',{width:w,height:h,deviceScaleFactor:1,mobile:false});
    await evaluate('new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r)))');
  };
  const navigate=async url=>{const loaded=wait('Page.loadEventFired');documentStatus=undefined;await call('Page.navigate',{url});await loaded;};
  const measure=()=>evaluate(`(()=>{
    const box=e=>{const r=e.getBoundingClientRect();return {x:r.x,y:r.y,width:r.width,height:r.height,right:r.right,bottom:r.bottom};};
    const lines=e=>{const r=document.createRange();r.selectNodeContents(e);return [...r.getClientRects()].filter(x=>x.width>0).length;};
    return {scrollWidth:document.documentElement.scrollWidth,clientWidth:document.documentElement.clientWidth,
      scripts:document.scripts.length,editable:document.querySelectorAll('input:not([type=hidden]):not([readonly]):not([disabled]),textarea:not([readonly]):not([disabled]),select:not([disabled])').length,
      values:[...document.querySelectorAll('textarea')].map(e=>e.value),
      cases:[...document.querySelectorAll('article.case')].map(e=>{
        const head=e.querySelector('header'),h=head.querySelector('h2'),source=head.querySelector('p'),status=head.querySelector('.status'),action=e.querySelector('.action');
        const reason=[...e.querySelectorAll('.facts dt')].find(x=>x.textContent==='必要な確認操作').nextElementSibling;
        const r=document.createRange();r.selectNodeContents(source.firstChild);
        const end=e.querySelector('footer.end');
        return {id:h.textContent.replace(/^案件\\s*/,''),box:box(e),header:box(h),sourceBox:box(source),sourceTextBox:box({getBoundingClientRect:()=>r.getBoundingClientRect()}),sourceText:source.textContent,status:status.textContent,endBox:box(end),endText:end.textContent,
          reasonText:reason.textContent,reasonBox:box(reason),reasonLines:lines(reason),sourceLines:[...r.getClientRects()].length,
          actionBox:box(action),actionText:action.textContent,linkBox:box(action.querySelector('a')),
          border:parseFloat(getComputedStyle(e).borderTopWidth),textOverflow:reason.scrollWidth>reason.clientWidth,
          units:[...e.querySelectorAll('.unit')].map(u=>({box:box(u),owner:u.closest('article.case').querySelector('h2').textContent.replace(/^案件\\s*/,''),text:u.textContent})),
          unitGlobalCount:document.querySelectorAll('.unit').length};})};})()`);
  const screenshot=async(name,w,h)=>{
    const layout=await call('Page.getLayoutMetrics');
    const r=await call('Page.captureScreenshot',{format:'png',captureBeyondViewport:true,fromSurface:true,
      clip:{x:0,y:0,width:w,height:Math.max(h,Math.ceil(layout.cssContentSize.height)),scale:1}});
    fs.writeFileSync(path.join(c.output,name),Buffer.from(r.data,'base64'));
  };
  const verify=(m,expected,w)=>{
    const failures=[];const check=(ok,label)=>{if(!ok)failures.push(label);};
    check(m.cases.length===expected.length,'case-count');check(new Set(m.cases.map(x=>x.id)).size===expected.length,'unique-case-ids');
    check(m.scripts===0,'escaping');check(m.editable===0,'editable-input-retention-requires-coverage');if(w===390)check(m.scrollWidth<=m.clientWidth,'horizontal-scroll');
    let previous;
    m.cases.forEach((v,i)=>{
      const e=expected[i];check(v.id===e.case_id,'synthetic-case-id');
      check(v.sourceText.includes(e.source_name)&&v.sourceText.includes(e.page_range)&&v.sourceText.includes('Unit数：'+e.unit_count),'header-source-page-units');
      check(/保留|確認可能|完了/.test(v.status),'text-status');check(v.reasonText===e.next_action,'operation-label');
      check(v.units.length===e.unit_count,'unit-count');check(!v.textOverflow,'long-reason-overflow');
      check(v.reasonBox.x>=v.box.x&&v.reasonBox.right<=v.box.right+1,'reason-containment');
      check(v.sourceTextBox.x>=v.box.x&&v.sourceTextBox.right<=v.box.right+1,'long-name-containment');
      check(v.endBox.bottom<=v.box.bottom&&v.endText.includes(e.case_id),'case-end-visible');
      check(v.actionBox.height>0&&v.linkBox.height>0&&v.linkBox.right<=v.box.right+1,'action-visible');
      if(previous){check(previous.bottom<=v.box.y,'case-overlap');check(v.box.y-previous.bottom>=16||v.border>=1,'case-separation');}
      previous=v.box;
      v.units.forEach((u,j)=>{check(u.owner===e.case_id&&u.text.includes(e.units[j].unit_id),'unit-ownership');
        check(u.box.x>v.box.x&&u.box.right<=v.box.right&&u.box.y>=v.sourceBox.bottom&&u.box.bottom<=v.actionBox.y,'nested-unit-bounds');});
      if(w===390){check(v.header.bottom<=v.sourceBox.y,'mobile-heading-order');check(v.sourceBox.bottom<=v.actionBox.y,'mobile-action-order');}
      if(e.case_id==='isolated-purpose-case'){check(v.reasonLines>1,'long-reason-wrap');if(w===390)check(v.sourceLines>1,'long-name-wrap');}
    });
    check(m.cases.every(v=>v.unitGlobalCount===expected.reduce((n,e)=>n+e.unit_count,0)),'all-units-owned');
    console.log(JSON.stringify({viewport:w,caseCount:m.cases.length,caseBoxes:m.cases.map(v=>({id:v.id,...v.box})),
      scrollWidth:m.scrollWidth,clientWidth:m.clientWidth,editableInputResize:m.editable===0?'対象外（編集可能入力なし）':'required',failures}));
    assert.equal(failures.length,0,'DOM/layout inspection failed');assert.equal(network.length,0,'localhost network boundary');
  };
  try{
    await call('Browser.getVersion',{},null);
    const target=await call('Target.createTarget',{url:'about:blank'},null);
    session=(await call('Target.attachToTarget',{targetId:target.targetId,flatten:true},null)).sessionId;
    await call('Page.enable');await call('Network.enable');await call('Fetch.enable',{patterns:[{urlPattern:'*',requestStage:'Request'}]});
    await resize(1440,1000);await navigate(c.base+'/isolated-shadow/receipts');assert.equal(documentStatus,403,'browser must authenticate');
    await call('Network.setCookies',{cookies:c.cookies});await navigate(c.base+'/isolated-shadow/receipts');assert.equal(documentStatus,200,'existing authenticated session required');
    const values=(await measure()).values;
    for(const [w,h,name] of [[1440,1000,'list-pc-1440.png'],[390,844,'list-mobile-390.png']]){
      await resize(w,h);const m=await measure();await screenshot(name,w,h);verify(m,c.cases,w);assert.deepEqual(m.values,values,'existing unsent readonly values retained');
    }
    await resize(1440,1000);assert.deepEqual((await measure()).values,values,'readonly values retained on resize back');
    await navigate(c.base+'/isolated-shadow/receipts/cases/'+c.detail_id);assert.equal(documentStatus,200);
    const selected=c.cases.filter(e=>e.case_id===c.detail_id);
    for(const [w,h,name] of [[1440,1000,'detail-pc-1440.png'],[390,844,'detail-mobile-390.png']]){
      await resize(w,h);const m=await measure();await screenshot(name,w,h);verify(m,selected,w);
    }
  }finally{
    try{await call('Browser.close',{},null);}catch{}
    await new Promise(resolve=>{if(browser.exitCode!==null)resolve();else{browser.once('exit',resolve);setTimeout(()=>{browser.kill('SIGTERM');resolve();},5000);}});
  }
}
