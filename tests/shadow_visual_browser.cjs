// Installed Chrome through its standard CDP pipe; Node built-ins only.
const fs=require('node:fs');
const {spawn,spawnSync}=require('node:child_process');
const assert=require('node:assert/strict');
const path=require('node:path');
function safeUrl(raw){
  if(!raw)return {url:'unknown',scheme:'unknown',hostname:'unknown',port:'unknown',path:'unknown'};
  try{
    const u=new URL(raw),scheme=u.protocol.slice(0,-1);
    if(['data','blob'].includes(scheme))return {url:scheme+':<redacted>',scheme,hostname:'unknown',port:'unknown',path:'<redacted>'};
    if(!['http','https','ws','wss'].includes(scheme))return {url:scheme+':<redacted>',scheme,hostname:'unknown',port:'unknown',path:'<redacted>'};
    const keys=[...u.searchParams.keys()].map(k=>/^[a-zA-Z0-9_.-]{1,64}$/.test(k)?k:'redacted-key');
    const query=keys.map(k=>encodeURIComponent(k)+'=<redacted>').join('&');
    const port=u.port||(['https','wss'].includes(scheme)?'443':'80');
    return {url:scheme+'://'+u.hostname+(u.port?':'+u.port:'')+u.pathname+(query?'?'+query:''),scheme,hostname:u.hostname,port,path:u.pathname};
  }catch{return {url:'unknown',scheme:'unknown',hostname:'unknown',port:'unknown',path:'unknown'};}
}
function redactionSelfTest(){
  const sample=safeUrl('https://private-user:private-pass@example.test:8443/font.css?token=private-token&signature=private-signature#private-fragment');
  const text=JSON.stringify(sample);
  for(const secret of ['private-user','private-pass','private-token','private-signature','private-fragment'])assert(!text.includes(secret),'URL redaction failed');
  assert.equal(sample.url,'https://example.test:8443/font.css?token=<redacted>&signature=<redacted>');
  assert(!JSON.stringify(safeUrl('data:text/plain,private-data')).includes('private-data'));
  const argv=JSON.stringify(safeArguments(['/bin/chrome','--auth=private-token','--ignore-certificate-errors-spki-list=private-spki','--user-data-dir=/tmp/synthetic-profile']));
  assert(!argv.includes('private-token')&&!argv.includes('private-spki'),'argument redaction failed');
  assert.equal(secretMatches({query:'private-canary'},['private-canary']),1);
}
function secretMatches(value,secrets){const text=JSON.stringify(value);return secrets.filter(s=>s&&text.includes(s)).length;}
if(require.main===module){
  if(process.argv.includes('--redaction-self-test')){redactionSelfTest();console.log('Node URL redaction self-test PASSED');}
  else{
    let input='';process.stdin.setEncoding('utf8');process.stdin.on('data',s=>input+=s);
    process.stdin.on('end',()=>{const c=JSON.parse(input);(c.phase?processPhase(c):main(c)).catch(()=>{console.log('diagnostic or visual verification failed; no secrets logged');process.exitCode=1;});});
  }
}

function browserArguments(c){
  const args=['--headless=new','--remote-debugging-pipe','--user-data-dir='+c.profile,
    '--disable-background-networking','--disable-component-update','--disable-default-apps','--disable-sync',
    '--no-first-run','--no-default-browser-check','--disable-domain-reliability','--metrics-recording-only',
    '--disable-client-side-phishing-detection','--safebrowsing-disable-auto-update',
    '--disable-features=OptimizationHints,MediaRouter,Translate,AutofillServerCommunication,NetworkTimeServiceQuerying',
    '--proxy-server='+c.proxy,'--proxy-bypass-list=127.0.0.1',
    '--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1',
    '--ignore-certificate-errors-spki-list='+c.spki,'--log-net-log='+c.netlog];
  // No added switches. Default NetLog capture mode remains Default.
  // B has no positional URL and no explicit context/page creation.
  if(c.phase!=='B')args.push('about:blank');
  return args;
}

function procInfo(pid){
  const stat=fs.readFileSync('/proc/'+pid+'/stat','utf8');
  const fields=stat.slice(stat.lastIndexOf(')')+2).split(' ');
  return {pid,parent_pid:Number(fields[1]),group:Number(fields[2]),state:fields[0],exe:fs.readlinkSync('/proc/'+pid+'/exe')};
}
function safeArguments(args){
  const plain=new Set(['--headless','--disable-features','--proxy-bypass-list','--host-resolver-rules']);
  return args.map((arg,i)=>{
    if(i===0)return arg;
    if(arg==='about:blank')return arg;
    if(!arg.startsWith('--'))return '<redacted>';
    const split=arg.indexOf('=');if(split<0)return arg;
    const key=arg.slice(0,split),value=arg.slice(split+1);
    if(['--user-data-dir','--log-net-log'].includes(key))return key+'='+value;
    if(key==='--proxy-server')return key+'='+safeUrl(value).url;
    return key+'='+(plain.has(key)?value:'<redacted>');
  });
}
async function processPhase(c){
  redactionSelfTest();
  const records=[],milestones=[],tracked=new Set();let binary,session;
  const mark=event=>milestones.push({event,time_ms:Date.now()});
  mark('browser_spawn');
  const browser=spawn(c.chrome,browserArguments(c),{detached:true,stdio:['ignore','ignore','pipe','pipe','pipe']});
  fs.writeFileSync(c.pid_file,String(browser.pid));
  browser.stderr.on('data',()=>{});
  let sequence=0,buffer='';const pending=new Map();
  const call=(method,params={},sid=session)=>new Promise((resolve,reject)=>{
    const id=++sequence,timer=setTimeout(()=>{pending.delete(id);reject(new Error('phase CDP timeout'));},15000);
    pending.set(id,{resolve:x=>{clearTimeout(timer);resolve(x);},reject:e=>{clearTimeout(timer);reject(e);}});
    browser.stdio[3].write(JSON.stringify({id,method,params,...(sid?{sessionId:sid}:{})})+'\0');
  });
  let status;
  const frames=new Map(),requests=new Map();
  const stack=s=>s?[...(s.callFrames||[]).map(f=>({script_url:safeUrl(f.url).url,line:f.lineNumber,column:f.columnNumber})),...stack(s.parent)]:[];
  const record=(layer,p,original)=>{
    const url=safeUrl(p.request.url),init=original?.initiator;
    records.push({sequence:records.length+1,layer,time_ms:Date.now(),method:p.request.method,...url,
      resource_type:p.type||p.resourceType||'unknown',document_url:safeUrl(original?.documentURL).url,
      redirect_from:safeUrl(original?.redirectResponse?.url).url,initiator_type:init?.type||'unknown',
      initiator_stack:stack(init?.stack),frame_url:safeUrl(frames.get(p.frameId)).url,
      allowed:['data','blob'].includes(url.scheme)||new URL(p.request.url).origin===c.base});
  };
  browser.stdio[4].on('data',data=>{
    buffer+=data.toString();let end;
    while((end=buffer.indexOf('\0'))>=0){
      const raw=buffer.slice(0,end);buffer=buffer.slice(end+1);if(!raw)continue;const m=JSON.parse(raw);
      if(m.id){const p=pending.get(m.id);if(p){pending.delete(m.id);m.error?p.reject(new Error('phase CDP rejected')):p.resolve(m.result);}continue;}
      if(m.method==='Page.frameNavigated')frames.set(m.params.frame.id,m.params.frame.url);
      if(m.method==='Network.requestWillBeSent'){requests.set(m.params.requestId,m.params);record('cdp-network',m.params,m.params);}
      if(m.method==='Network.responseReceived'&&m.params.type==='Document')status=m.params.response.status;
      if(m.method==='Fetch.requestPaused'){
        record('page-fetch',m.params,requests.get(m.params.networkId));
        const u=new URL(m.params.request.url),ok=['data:','blob:'].includes(u.protocol)||u.origin===c.base;
        call(ok?'Fetch.continueRequest':'Fetch.failRequest',{requestId:m.params.requestId,...(ok?{}:{errorReason:'BlockedByClient'})}).catch(()=>{});
      }
    }
  });
  browser.on('exit',()=>{for(const p of pending.values())p.reject(new Error('phase browser exited'));});
  const collect=()=>{
    for(const name of fs.readdirSync('/proc'))if(/^\d+$/.test(name)){
      try{const info=procInfo(Number(name));if(info.group===browser.pid)tracked.add(info.pid);}catch{}
    }
  };
  let phaseError=false;
  try{
    const version=await call('Browser.getVersion',{},null);
    const info=procInfo(browser.pid),parent=procInfo(info.parent_pid);
    const rawArgs=fs.readFileSync('/proc/'+browser.pid+'/cmdline').toString().split('\0').filter(Boolean);
    const ownVersion=spawnSync(info.exe,['--version'],{encoding:'utf8',timeout:10000});
    assert.equal(ownVersion.status,0,'binary version query failed');
    assert(/\d+\.\d+\.\d+\.\d+/.test(ownVersion.stdout),'binary version missing');
    binary={...info,parent:{pid:parent.pid,exe:parent.exe},version:ownVersion.stdout.trim(),product:version.product,
      revision:version.revision,effective_arguments:safeArguments(rawArgs),user_data_dir:c.profile};
    const targets=await call('Target.getTargets',{},null);
    binary.implicit_startup_targets=targets.targetInfos.map(t=>({type:t.type,url:safeUrl(t.url).url,is_about_blank:t.url==='about:blank'}));
    mark('binary_verified');collect();
    if(c.phase!=='B'){
      mark('context_create_start');const context=await call('Target.createBrowserContext',{},null);mark('context_created');
      mark('page_create_start');const target=await call('Target.createTarget',{url:'about:blank',browserContextId:context.browserContextId},null);mark('page_created');
      session=(await call('Target.attachToTarget',{targetId:target.targetId,flatten:true},null)).sessionId;
      await call('Page.enable');await call('Network.enable');await call('Fetch.enable',{patterns:[{urlPattern:'*',requestStage:'Request'}]});
      if(c.phase==='D'){
        const navigate=async(url,expected)=>{
          status=undefined;mark('GET_start');await call('Page.navigate',{url});
          for(let i=0;i<100&&status===undefined;i++)await new Promise(r=>setTimeout(r,20));
          assert.equal(status,expected,'existing authentication response changed');mark('GET_response');
        };
        await navigate(c.base+'/isolated-shadow/receipts',403);
        await call('Network.setCookies',{cookies:c.cookies});
        await navigate(c.base+'/isolated-shadow/receipts',200);
        await navigate(c.base+'/isolated-shadow/receipts/cases/'+c.detail_id,200);
      }
    }
    assert(Date.now()<c.deadline_ms,'phase initialization exceeded fixed observation window');
    while(Date.now()<c.deadline_ms){collect();await new Promise(r=>setTimeout(r,Math.min(200,c.deadline_ms-Date.now())));}
    mark('observation_end');
  }catch{phaseError=true;mark('phase_error');}
  finally{
    collect();mark('browser_close_start');try{await call('Browser.close',{},null);}catch{}
    for(let i=0;i<30;i++){
      const living=[...tracked].filter(pid=>{try{return procInfo(pid).state!=='Z';}catch{return false;}});
      if(!living.length)break;await new Promise(r=>setTimeout(r,100));
    }
    const living=[...tracked].filter(pid=>{try{return procInfo(pid).state!=='Z';}catch{return false;}});
    if(living.length){phaseError=true;try{process.kill(-browser.pid,'SIGTERM');}catch{}}
    mark('process_cleanup_checked');
    const remaining=[...tracked].filter(pid=>{try{return procInfo(pid).state!=='Z';}catch{return false;}});
    fs.writeFileSync(c.result_file,JSON.stringify({phase:c.phase,binary,records,milestones,
      tracked_pids:[...tracked],remaining_running_pids:remaining,phase_error:phaseError,
      capture_mode:'Default (no capture-mode switch; Chromium default)',explicit_context_created:c.phase!=='B'},null,2));
  }
  assert(!phaseError,'phase setup/cleanup failed');
}

async function main(c){
  redactionSelfTest();
  const records=[],metrics=[],fonts=[],frames=new Map(),initiators=new Map();
  const privacy={external_canary_matches:0,cookie_scope_checked:false,browser_network_namespace:'not checked'};
  const stackFrames=stack=>stack?[...(stack.callFrames||[]).map(f=>({script_url:safeUrl(f.url).url,line:f.lineNumber,column:f.columnNumber})),...stackFrames(stack.parent)]:[];
  const allowed=raw=>{try{const u=new URL(raw);return ['data:','blob:'].includes(u.protocol)||u.origin===c.base;}catch{return false;}};
  const record=(layer,p,init)=>{
    const u=safeUrl(p.request.url),frame=frames.get(p.frameId);
    if(c.egress_safety&&!allowed(p.request.url))privacy.external_canary_matches+=secretMatches(p.request,c.private_canaries);
    records.push({sequence:records.length+1,layer,time_ms:Date.now(),method:p.request.method,...u,
      resource_type:p.type||p.resourceType||'unknown',document_url:safeUrl(p.documentURL).url,
      redirect_from:safeUrl(p.redirectResponse?.url).url,initiator_type:init?.type||'unknown',
      initiator_stack:stackFrames(init?.stack),frame_url:safeUrl(frame?.url).url,allowed:allowed(p.request.url)});
  };
  const browser=spawn(c.chrome,browserArguments(c),
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
      if(m.method==='Page.frameNavigated')frames.set(m.params.frame.id,{url:m.params.frame.url});
      if(m.method==='Network.requestWillBeSent'){
        initiators.set(m.params.requestId,m.params);
        record('cdp-network',m.params,m.params.initiator);
        if(!allowed(m.params.request.url))network.push('non-loopback request');
      }
      if(m.method==='Network.responseReceived'&&m.params.type==='Document')documentStatus=m.params.response.status;
      if(m.method==='Fetch.requestPaused'){
        const observed=initiators.get(m.params.networkId);
        record('page-fetch', {...m.params,documentURL:observed?.documentURL,redirectResponse:observed?.redirectResponse},observed?.initiator);
        const ok=allowed(m.params.request.url);
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
  const fontDiagnostic=async(viewport,screen)=>{
    const result=await evaluate(`(async()=>{await document.fonts.ready;
      const sample='日本語領収書確認';const text=document.body.innerText;
      return {computed_font_family:getComputedStyle(document.body).fontFamily,document_fonts_status:document.fonts.status,
        japanese_sample:sample,sample_font_check:document.fonts.check('17px '+getComputedStyle(document.body).fontFamily,sample),
        japanese_text_present:/[\u3040-\u30ff\u4e00-\u9fff]/.test(text),replacement_character_present:text.includes('\ufffd')};})()`);
    const document=await call('DOM.getDocument');const node=await call('DOM.querySelector',{nodeId:document.root.nodeId,selector:'.notice'});
    const actual=await call('CSS.getPlatformFontsForNode',{nodeId:node.nodeId});
    result.platform_fonts=actual.fonts.map(f=>({family:f.familyName,glyph_count:f.glyphCount,custom:f.isCustomFont}));
    fonts.push({viewport,screen,...result});
  };
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
    const measured={viewport:w,caseCount:m.cases.length,caseBoxes:m.cases.map(v=>({id:v.id,...v.box})),
      scrollWidth:m.scrollWidth,clientWidth:m.clientWidth,editableInputResize:m.editable===0?'対象外（編集可能入力なし）':'required',failures};
    metrics.push(measured);console.log(JSON.stringify(measured));
    assert.equal(failures.length,0,'DOM/layout inspection failed');
  };
  try{
    await call('Browser.getVersion',{},null);
    if(c.egress_safety){
      privacy.browser_network_namespace=fs.readlinkSync('/proc/'+browser.pid+'/ns/net');
      assert.equal(privacy.browser_network_namespace,c.egress_namespace,'browser escaped isolated network namespace');
    }
    const target=await call('Target.createTarget',{url:'about:blank'},null);
    session=(await call('Target.attachToTarget',{targetId:target.targetId,flatten:true},null)).sessionId;
    await call('Page.enable');await call('Network.enable');await call('DOM.enable');await call('CSS.enable');await call('Fetch.enable',{patterns:[{urlPattern:'*',requestStage:'Request'}]});
    await resize(1440,1000);await navigate(c.base+'/isolated-shadow/receipts');assert.equal(documentStatus,403,'browser must authenticate');
    await call('Network.setCookies',{cookies:c.cookies});await navigate(c.base+'/isolated-shadow/receipts');assert.equal(documentStatus,200,'existing authenticated session required');
    if(c.egress_safety){
      const stored=await call('Network.getCookies',{urls:[c.base+'/']});
      for(const expected of c.cookies){
        const cookie=stored.cookies.find(value=>value.name===expected.name);
        assert(cookie&&cookie.value===expected.value&&cookie.domain==='127.0.0.1'&&cookie.secure&&cookie.httpOnly,'authentication cookie scope changed');
      }
      privacy.cookie_scope_checked=true;
      assert(await evaluate('document.body.innerText.includes('+JSON.stringify(c.business_canary)+')'),'synthetic business canary missing from rendered page');
    }
    const values=(await measure()).values;
    for(const [w,h,name] of [[1440,1000,'list-pc-1440.png'],[390,844,'list-mobile-390.png']]){
      await resize(w,h);await fontDiagnostic(w,'list');const m=await measure();await screenshot(name,w,h);verify(m,c.cases,w);assert.deepEqual(m.values,values,'existing unsent readonly values retained');
    }
    await resize(1440,1000);assert.deepEqual((await measure()).values,values,'readonly values retained on resize back');
    await navigate(c.base+'/isolated-shadow/receipts/cases/'+c.detail_id);assert.equal(documentStatus,200);
    const selected=c.cases.filter(e=>e.case_id===c.detail_id);
    for(const [w,h,name] of [[1440,1000,'detail-pc-1440.png'],[390,844,'detail-mobile-390.png']]){
      await resize(w,h);await fontDiagnostic(w,'detail');const m=await measure();await screenshot(name,w,h);verify(m,selected,w);
    }
  }finally{
    try{await call('Browser.close',{},null);}catch{}
    await new Promise(resolve=>{if(browser.exitCode!==null)resolve();else{browser.once('exit',resolve);setTimeout(()=>{browser.kill('SIGTERM');resolve();},5000);}});
    fs.writeFileSync(path.join(c.output,'network-records.json'),JSON.stringify({observer:'page-fetch is existing Fetch interception, not Playwright; initiator only copied from matching CDP event',records},null,2));
    fs.writeFileSync(path.join(c.output,'computed-fonts.json'),JSON.stringify(fonts,null,2));
    fs.writeFileSync(path.join(c.output,'viewport-metrics.json'),JSON.stringify(metrics,null,2));
    if(c.egress_safety)fs.writeFileSync(path.join(c.output,'browser-privacy.json'),JSON.stringify(privacy,null,2));
  }
  // Preserve the rejection, but first persist only the redacted diagnostics.
  assert.equal(network.length,0,'localhost network boundary');
}
