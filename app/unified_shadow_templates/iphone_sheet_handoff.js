'use strict';
// Only the #155 synthetic API. No R1 financial POST, browser-edited payload,
// authority, retry loop or external URL. Stored IDs are recovery hints only.
const base='/isolated-synthetic/iphone-sheet-handoff';
const main=document.querySelector('main');
const attemptKey=`kakeibo-sheet-handoff-attempt:${main.dataset.owner}`;
const labels={DRAFT_IMPORTED:'下書き取込済み・未確定',FINANCIAL_BOUNDARY_STOPPED:'確認済み・未記帳',UNKNOWN:'UNKNOWN：結果不明・要照合'};
let attempted=null, storageOK=true, busy=false;
try {attempted=JSON.parse(sessionStorage.getItem(attemptKey)||'null');} catch {storageOK=false;}
const lookup=document.querySelector('#lookup-context');
if(lookup) {
  const result=JSON.parse(lookup.textContent);
  if(result.status!=='UNKNOWN'&&attempted?.rid===result.request_id) {
    try {sessionStorage.removeItem(attemptKey);attempted=null;} catch {storageOK=false;}
  }
}
const search=document.querySelector('#search');
function filter() {for(const row of document.querySelectorAll('tbody tr'))row.hidden=!row.dataset.caseId.toLowerCase().includes(search.value.toLowerCase());}
if(search) {search.value=new URLSearchParams(location.hash.slice(1)).get('case')||'';filter();search.addEventListener('input',()=>{history.replaceState(null,'','#'+new URLSearchParams({case:search.value}));filter();});}
const rows=[...document.querySelectorAll('tr:has(.row-context)')].map(row=>({row,initial:JSON.parse(row.querySelector('.row-context').textContent),proof:null,blocked:row.dataset.financialStopped==='true'}));
function buttons() {
  for(const item of rows) {
    for(const b of item.row.querySelectorAll('button'))b.disabled=busy||!storageOK||!!attempted||item.blocked;
    const compare=item.row.querySelector('[data-action=compare]');
    compare.disabled||=!item.initial.imported;
    const post=item.row.querySelector('[data-action=confirm_handoff]');
    if(post)post.disabled||=!item.proof||!item.row.querySelector('.explicit').checked;
  }
}
function recovery(item,rid) {
  const link=item.row.querySelector('.recovery');link.href=`${base}/view-results/${encodeURIComponent(rid)}`;link.hidden=false;
}
function unknown(rid,cid) {
  attempted={rid,cid};
  for(const item of rows) {item.proof=null;item.row.querySelector('.review').hidden=true;item.row.querySelector('.result').textContent='UNKNOWN：結果不明・要照合。結果照会のみ可能です。自動再送しません。';recovery(item,rid);}
  buttons();
}
if(attempted)unknown(attempted.rid,attempted.cid);
if(!storageOK)for(const item of rows)item.row.querySelector('.result').textContent='確認状態を保持できません。操作を停止しています。保存済み結果を照会してください。';
function errorText(response,data) {
  if(response.status>=500||data.status==='UNKNOWN')return 'UNKNOWN：結果不明・要照合。結果照会のみ可能です。';
  if(response.status===410)return '期限切れ：入力は保持されています。再認証後に画面を再読込みし、比較からやり直してください。';
  if(response.status===403)return '本人認証・権限・送信元を確認できません。再認証後に再読込みしてください。';
  if(/held_or_unmapped|receipt_category_requires_item_review|^draft_v2_(integer|text|line_identity|line_category|complete_manual_only|receipt_validator)$/.test(data.error||''))return '保留：未入力・原本対応・明細別カテゴリを確認してください。入力は保持されています。';
  return '競合：入力・原本・版が変化したか、重複要求です。入力は保持されています。再読込みして比較し直してください。';
}
async function api(path,payload) {
  const response=await fetch(base+path,{method:'POST',credentials:'same-origin',redirect:'error',
    headers:{'Content-Type':'application/json','X-CSRF-Token':main.dataset.csrf},body:JSON.stringify(payload)});
  const data=await response.json();
  if(!response.ok) {const error=new Error(errorText(response,data));error.status=response.status;throw error;}
  return data;
}
function describe(p) {
  return p?`日付：${p.date}\n店舗・施設：${p.merchant}\n合計：${p.total}円\n支払方法：${p.payment_method}\n`+
    p.items.map(i=>`${i.name} / ${i.amount}円 / ${i.major_category}｜${i.minor_category}`).join('\n'):'未入力';
}
function comparison(item,p) {
  const review=item.row.querySelector('.review');review.replaceChildren();
  const text=document.createElement('pre');text.className='compare';
  const s=p.source,snapshot=p.comparison;
  const before=snapshot.inputs.proposed.original_parsed,after=snapshot.inputs.proposed.parsed;
  const differences=['date','merchant','total','payment_method','items'].filter(k=>JSON.stringify(before?.[k])!==JSON.stringify(after[k]));
  text.textContent=`案件ID：${p.case_id}\n種類：${p.kind==='medical'?'医療（完全手入力）':'一般（明細別カテゴリを維持）'}\n原本ID：${s.source_id}\n原本hash：${s.sha256}\n原本revision：${s.drive_revision_id}\nnative版：${s.native_source_version}\nSheet snapshot：${p.sheet_snapshot}\n要求ID：${p.request_id}\n\n修正前\n${describe(before)}\n\n修正後（確認する下書き・未記帳）\n${describe(after)}\n\n変更差分：${differences.join(' / ')||'なし'}\n途中メモ：${p.review.fields.memo}\n`+
    p.review.items.map(i=>`${i.expense_id} / ${i.state} / ${i.note}`).join('\n');
  const details=document.createElement('details'),summary=document.createElement('summary'),exact=document.createElement('pre');
  summary.textContent='検証した入力・構造・変更内容の全値';exact.textContent=JSON.stringify({review:p.review,comparison:p.comparison},null,2);details.append(summary,exact);
  const label=document.createElement('label'),ack=document.createElement('input');ack.type='checkbox';ack.className='explicit';
  label.append(ack,document.createTextNode('この案件・原本・全入力・差分を確認しました（未記帳）'));
  const post=document.createElement('button');post.dataset.action='confirm_handoff';post.textContent='本人確認を確定（記帳しない）';post.disabled=true;
  ack.addEventListener('change',buttons);review.append(text,details,label,post);review.hidden=false;
  review.scrollIntoView({block:'nearest',inline:'end'});
}
for(const item of rows) {
  const {row,initial}=item,result=row.querySelector('.result');
  row.addEventListener('click',async event=>{
    const action=event.target.dataset.action;
    if(!action||busy||attempted||item.blocked||!storageOK)return;
    if(action==='confirm_handoff'&&(!item.proof||!row.querySelector('.explicit')?.checked))return;
    busy=true;buttons();
    // A previous comparison is never reused after another prepare/error.
    let sent=false;
    try {
      if(action==='compare') {
        item.proof=null;row.querySelector('.review').hidden=true;
        item.proof=await api(`/prepare/${encodeURIComponent(initial.case_id)}/confirm_handoff`,{});
        if(item.proof.case_id!==initial.case_id)throw new Error('競合：案件IDが一致しません。');
        comparison(item,item.proof);result.textContent='未確定：比較内容を確認してください。';
      } else if(action==='import_draft'||action==='confirm_handoff') {
        const p=action==='import_draft'?await api(`/prepare/${encodeURIComponent(initial.case_id)}/import_draft`,{}):item.proof;
        if(p.case_id!==initial.case_id)throw new Error('競合：案件IDが一致しません。');
        // Must persist recovery hint BEFORE sending, including draft import.
        sessionStorage.setItem(attemptKey,JSON.stringify({rid:p.request_id,cid:p.case_id}));
        sent=true;recovery(item,p.request_id);
        const value=await api('/request',{proof:p.proof,case_id:p.case_id,sheet_snapshot:p.sheet_snapshot,confirmed:action==='confirm_handoff'});
        if(value.request_id!==p.request_id||value.case_id!==p.case_id||
           value.status!==(action==='import_draft'?'DRAFT_IMPORTED':'FINANCIAL_BOUNDARY_STOPPED')||
           value.writer_dispatches!==0||value.accounting_permission!==false||value.native_authority!==false)
          throw new Error('UNKNOWN：受付内容が一致しません。');
        sessionStorage.removeItem(attemptKey);
        result.textContent=labels[value.status];item.proof=null;row.querySelector('.review').hidden=true;
        // GET only: show the server's saved draft/revision and keep case search.
        if(action==='import_draft')location.reload();
        else {item.blocked=true;row.querySelector('.save-state').textContent=labels[value.status];}
      }
    } catch(error) {
      item.proof=null;row.querySelector('.review').hidden=true;
      if(sent) {
        // Even a rejected request can have left durable ATTEMPTING. Never
        // erase its hint or retry based on an HTTP error alone.
        let hint;try{hint=JSON.parse(sessionStorage.getItem(attemptKey));}catch{}
        unknown(hint?.rid||'unavailable',initial.case_id);
        result.textContent=error.message.startsWith('UNKNOWN')?error.message:`${error.message} 受付状態はUNKNOWNです。結果照会のみ可能です。`;
      } else {
        result.textContent=error.message||'UNKNOWN：応答を確認できません。';
        // Prepare has no write, but uncertain state is still read-only until
        // an explicit navigation/re-authentication. No automatic re-execution.
        item.blocked=true;
        if(!error.status||error.status>=500) {
          try {sessionStorage.setItem(attemptKey,JSON.stringify({rid:'unavailable',cid:initial.case_id}));}catch {storageOK=false;}
          unknown('unavailable',initial.case_id);
        }
      }
    } finally {busy=false;buttons();}
  });
}
buttons();
window.addEventListener('pageshow',event=>{if(event.persisted)location.reload();});
