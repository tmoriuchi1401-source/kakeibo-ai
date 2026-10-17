// Render exported, authenticated synthetic HTML with every page request blocked.
// Uses an installed Playwright package; no application server/auth bypass.
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const {chromium} = require(process.env.PHASE5A_PLAYWRIGHT || 'playwright');
async function main() {
  const dir = path.resolve(process.argv[2]);
  const manifest = JSON.parse(fs.readFileSync(path.join(dir,'manifest.json'),'utf8'));
  const browser = await chromium.launch({headless:true, args:[
    '--disable-background-networking','--disable-component-update','--disable-sync',
    '--proxy-server=http://127.0.0.1:9','--host-resolver-rules=MAP * ~NOTFOUND']});
  const metrics = []; let pageRequests = 0;
  try {
    for (const width of [1365,390]) {
      const context = await browser.newContext({viewport:{width,height:844},
        javaScriptEnabled:false, serviceWorkers:'block'});
      await context.route('**/*', route => {pageRequests++; return route.abort();});
      const page = await context.newPage();
      for (const view of manifest.views) {
        await page.setContent(fs.readFileSync(path.join(dir,view.file),'utf8'),{waitUntil:'load'});
        const measured = await page.evaluate(() => ({
          width:innerWidth, documentWidth:document.documentElement.scrollWidth,
          cases:document.querySelectorAll('article[data-case-id]').length,
          controls:document.querySelectorAll('form,input,button,script,iframe').length,
          title:document.querySelector('h1')?.innerText,
          minLinkHeight:Math.min(...[...document.querySelectorAll('a')].map(a=>a.getBoundingClientRect().height)),
          links:[...document.querySelectorAll('a[href^="https:"]')].map(a=>a.getAttribute('rel'))
        }));
        assert.equal(measured.width,width);
        assert(measured.documentWidth<=width,'page overflows viewport');
        assert.equal(measured.cases,view.cases);
        assert.equal(measured.controls,0);
        assert(measured.minLinkHeight>=44);
        assert(measured.links.every(rel=>rel.includes('noreferrer')));
        const screenshot = `${width}-${view.name}.png`;
        await page.screenshot({path:path.join(dir,screenshot),fullPage:true});
        metrics.push({view:view.name,...measured,screenshot});
      }
      await context.close();
    }
    assert.equal(pageRequests,0);
    fs.writeFileSync(path.join(dir,'viewport-metrics.json'),JSON.stringify({metrics,pageRequests},null,2));
    console.log(JSON.stringify({screenshots:metrics.length,widths:[1365,390],pageRequests}));
  } finally { await browser.close(); }
}
main().catch(error=>{console.error(error.message);process.exitCode=1;});
