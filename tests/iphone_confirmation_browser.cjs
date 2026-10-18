// Real Chromium layout/interaction on authenticated exported HTML. All routes
// are served from local fixtures; no listener, real account or network service.
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const {chromium}=require(process.env.KAKEIBO_PLAYWRIGHT||'playwright');
const origin='https://confirmation.example.test';
async function main(){
 const dir=path.resolve(process.argv[2]),manifest=JSON.parse(fs.readFileSync(path.join(dir,'manifest.json'),'utf8'));
 const browser=await chromium.launch({headless:true,args:['--disable-background-networking','--disable-component-update','--disable-sync','--proxy-server=http://127.0.0.1:9','--host-resolver-rules=MAP * ~NOTFOUND','--log-file='+path.join(dir,'chromium-debug.log')]});
 const metrics=[],interactions=[];let external=0;
 try{
  for(const width of [1365,390]){
   const context=await browser.newContext({viewport:{width,height:844},serviceWorkers:'block'});
   const page=await context.newPage();let posts=[];
   await context.route('**/*',async route=>{
    const request=route.request(),url=new URL(request.url());
    if(url.origin!==origin){external++;return route.abort();}
    if(request.method()==='POST'){
     const body=request.postDataJSON();posts.push({body,csrf:request.headers()['x-csrf-token']});
     return route.fulfill({status:503,contentType:'application/json',body:JSON.stringify({message:'受付結果が不明です。同じ要求の受付結果を確認してください。'})});
    }
    const name=url.pathname.split('/').pop();
    if(name==='ui.js')return route.fulfill({contentType:'application/javascript',body:fs.readFileSync(path.join(dir,'ui.js'),'utf8')});
    const view=manifest.views.find(v=>v.file===name);
    if(view)return route.fulfill({contentType:'text/html',body:fs.readFileSync(path.join(dir,view.file),'utf8')});
    return route.fulfill({contentType:'text/html',body:'<p>同じ要求の結果を確認しています（offline fixture）。</p>'});
   });
   for(const view of manifest.views){
    await page.goto(origin+'/'+view.file);await page.waitForLoadState('networkidle');
    const measured=await page.evaluate(()=>({width:innerWidth,documentWidth:document.documentElement.scrollWidth,
      title:document.querySelector('h1')?.innerText,forms:document.querySelectorAll('form').length,
      minControlHeight:Math.min(...[...document.querySelectorAll('input:not([type=checkbox]),label:has(input[type=checkbox]),select,textarea,button,a')].map(e=>e.getBoundingClientRect()).filter(r=>r.width&&r.height).map(r=>r.height)),
      externalLinks:[...document.querySelectorAll('a[href^="https:"]')].map(e=>({target:e.target,rel:e.rel}))}));
    assert(measured.documentWidth<=width,`${view.name}: viewport overflow`);
    assert(measured.minControlHeight>=44,`${view.name}: small target`);
    assert(measured.externalLinks.every(l=>l.target==='_blank'&&l.rel.includes('noreferrer')));
    if(view.name.startsWith('held')||['expired','conflict','result','list'].includes(view.name))assert.equal(measured.forms,0);
    const screenshot=`${width}-${view.name}.png`;await page.screenshot({path:path.join(dir,screenshot),fullPage:true});
    metrics.push({view:view.name,...measured,screenshot});
   }
   await page.goto(origin+'/general.html');
   await page.locator('[name="field:merchant"]').fill('未保存の合成店舗入力');
   await page.locator('[data-item="note"]').first().fill('通信断でも保持する明細メモ');
   await page.locator('#reconfirm-link').click();
   assert((await page.locator('#feedback').innerText()).includes('先に下書きを保存'));
   assert.equal(posts.length,0);
   await page.getByRole('button',{name:'下書きを途中保存する'}).click();
   await page.waitForFunction(()=>document.querySelector('#feedback').textContent.includes('結果が不明'));
   assert.equal(posts.length,1);assert(posts[0].csrf);
   assert.equal(posts[0].body.edits.fields.merchant,'未保存の合成店舗入力');
   assert.equal(await page.locator('[name="field:merchant"]').inputValue(),'未保存の合成店舗入力');
   assert.equal(await page.locator('[data-item="note"]').first().inputValue(),'通信断でも保持する明細メモ');
   assert(await page.locator('button[type=submit]').isDisabled());
   await page.waitForTimeout(500);assert.equal(posts.length,1,'no automatic retry');
   const requestId=posts[0].body.request_id;
   assert((await page.locator('#recovery-link').getAttribute('href')).endsWith(requestId));
   interactions.push({width,unsavedGuard:true,unknownPreservesInputs:true,noAutoRetry:true,requestIdRetained:true});
   await page.unrouteAll({behavior:'wait'});
   await context.close();
   const c2=await browser.newContext({viewport:{width,height:844},serviceWorkers:'block'}),p2=await c2.newPage();
   await c2.route('**/*',route=>{
    const url=new URL(route.request().url());if(url.origin!==origin){external++;return route.abort();}
    if(url.pathname.endsWith('ui.js'))return route.fulfill({contentType:'application/javascript',body:fs.readFileSync(path.join(dir,'ui.js'),'utf8')});
    if(route.request().method()==='POST')return route.fulfill({status:409,contentType:'application/json',body:JSON.stringify({message:'競合しました。入力を保持してください。'})});
    return route.fulfill({contentType:'text/html',body:fs.readFileSync(path.join(dir,'reconfirm.html'),'utf8')});
   });
   await p2.goto(origin+'/reconfirm.html');
   assert(await p2.locator('button[type=submit]').isDisabled());
   await p2.locator('#acknowledged').check();assert(await p2.locator('button[type=submit]').isEnabled());
   await p2.locator('button[type=submit]').click();
   await p2.waitForFunction(()=>document.querySelector('#feedback').textContent.includes('競合'));
   assert(await p2.locator('#acknowledged').isChecked());
   interactions.push({width,explicitCheckboxRequired:true,conflictPreservesView:true});
   await c2.close();
   const c3=await browser.newContext({viewport:{width,height:844},serviceWorkers:'block'}),p3=await c3.newPage();
   const success=JSON.parse(fs.readFileSync(path.join(dir,'save-success.json'),'utf8'));
   let release,seen,latePosts=[];
   const inFlight=new Promise(resolve=>release=resolve),postSeen=new Promise(resolve=>seen=resolve);
   await c3.route('**/*',async route=>{
    const request=route.request(),url=new URL(request.url());
    if(url.origin!==origin){external++;return route.abort();}
    if(url.pathname.endsWith('ui.js'))return route.fulfill({contentType:'application/javascript',body:fs.readFileSync(path.join(dir,'ui.js'),'utf8')});
    if(request.method()==='POST'){
     latePosts.push(request.postDataJSON());
     if(latePosts.length===1){seen();await inFlight;return route.fulfill({contentType:'application/json',body:JSON.stringify(success)});}
     return route.fulfill({status:409,contentType:'application/json',body:JSON.stringify({message:'次の明示保存を確認しました。入力を保持します。'})});
    }
    const filename=url.pathname.endsWith('partial-edit.html')?'partial-edit.html':'saved-edit.html';
    return route.fulfill({contentType:'text/html',body:fs.readFileSync(path.join(dir,filename),'utf8')});
   });
   await p3.goto(origin+'/partial-edit.html');
   for(const select of await p3.locator('[data-item="state"]').all())await select.selectOption('entered');
   await p3.locator('button[type=submit]').click();await postSeen;
   await p3.locator('[name="field:memo"]').fill('送信中に追加した未保存メモ');release();
   await p3.waitForFunction(()=>document.querySelector('#feedback').textContent.includes('追加編集は未保存'));
   assert.equal(await p3.locator('[name="field:memo"]').inputValue(),'送信中に追加した未保存メモ');
   assert(await p3.locator('button[type=submit]').isEnabled());
   await p3.locator('button[type=submit]').click();
   await p3.waitForFunction(()=>document.querySelector('#feedback').textContent.includes('次の明示保存'));
   assert.equal(latePosts.length,2);
   assert.equal(latePosts[1].expected_revision,success.result.stored_revision);
   assert.notEqual(latePosts[0].request_id,latePosts[1].request_id);
   assert.equal(latePosts[1].edits.fields.memo,'送信中に追加した未保存メモ');
   interactions.push({width,inflightTypingRetained:true,knownSaveRefreshesExactProof:true});
   await c3.close();
  }
  assert.equal(external,0);
  fs.writeFileSync(path.join(dir,'viewport-metrics.json'),JSON.stringify({metrics,interactions,externalRequests:external},null,2));
  console.log(JSON.stringify({screenshots:metrics.length,widths:[1365,390],interactionChecks:interactions.length,externalRequests:external}));
 }finally{await browser.close();}
}
main().catch(e=>{console.error(e.stack);process.exitCode=1;});
