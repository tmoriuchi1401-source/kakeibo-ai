'use strict';
document.querySelector('#search')?.addEventListener('input', e => {
  for (const row of document.querySelectorAll('tbody tr')) row.hidden=!row.dataset.caseId.toLowerCase().includes(e.target.value.toLowerCase());
});
for (const row of document.querySelectorAll('tr:has(.row-context)')) {
  const initial=JSON.parse(row.querySelector('.row-context').textContent);
  const result=row.querySelector('.result'), state=row.querySelector('.save-state'), review=row.querySelector('.review');
  const recovery=row.querySelector('.recovery');
  let dirty=false, pending=false, blocked=false, proof=null;
  const base='/isolated-synthetic/iphone-confirmation';
  const number=v=>/^-?\d+$/.test(v)?Number(v):v;
  function value() {
    const v=structuredClone(initial.value);
    for(const input of row.querySelectorAll('[data-field]'))v.fields[input.dataset.field]=input.dataset.field==='amount'?number(input.value):input.value;
    for(const [n,item] of [...row.querySelectorAll('fieldset')].entries())
      for(const input of item.querySelectorAll('[data-item]'))v.items[n][input.dataset.item]=input.dataset.item==='amount'?number(input.value):input.value;
    return v;
  }
  row.addEventListener('input', e=>{if(e.target.closest('.review'))return;dirty=true;proof=null;review.hidden=true;state.textContent='未保存の変更があります';});
  function buttons() {for(const b of row.querySelectorAll('button'))b.disabled=pending||blocked;}
  async function obtain(action) {
    const r=await fetch(`${base}/r1/${initial.case_id}/${action}`,{credentials:'same-origin'});
    const data=await r.json();if(!r.ok)throw new Error(data.message);return data;
  }
  async function send(p, v, confirmed) {
    recovery.href=`${base}/r1/results/${p.claims.request_id}`;recovery.hidden=false;
    const response=await fetch(`${base}/r1/request`,{method:'POST',credentials:'same-origin',redirect:'error',
      headers:{'Content-Type':'application/json','Origin':location.origin,'X-CSRF-Token':p.csrf},
      body:JSON.stringify({proof:p.proof,value:v,confirmed})});
    const data=await response.json();if(!response.ok) {if(response.status>=500)blocked=true;throw new Error(data.message);}
    return data;
  }
  row.addEventListener('click',async e=>{
    const action=e.target.dataset.action;if(!action||pending||blocked)return;
    if(action==='compare'&&dirty){result.textContent='先に下書きを保存してください。';return;}
    pending=true;buttons();
    try {
      if(action==='save_draft') {
        const sent=value(), p=await obtain('save_draft');
        const saved=await send(p,sent,false);result.textContent=saved.label;
        if(JSON.stringify(sent)===JSON.stringify(value())){dirty=false;initial.value=sent;state.textContent='下書き保存済み';}
        else state.textContent='送信中の追加編集を保持しています（未保存）';
        review.hidden=true;proof=null;
      } else if(action==='compare') {
        proof=await obtain('post');
        if(JSON.stringify(proof.claims.draft.value)!==JSON.stringify(initial.value)){throw new Error('別操作で下書きが変更されました。再読込みして再確認してください。');}
        review.replaceChildren();
        const snapshot=proof.claims.snapshot;
        const heading=document.createElement('p');heading.textContent=`${initial.case_id} / ACCOUNTING_POST / この内容を合成記帳します`;
        const compare=document.createElement('pre');compare.className='compare';
        const describe=p=>p?`日付：${p.date}\n店舗・施設：${p.merchant}\n合計：${p.total}円\n支払方法：${p.payment_method}\n`+
          p.items.map(i=>`${i.name} / ${i.amount}円 / ${i.major_category}｜${i.minor_category}`).join('\n'):'未入力';
        compare.textContent='修正前\n'+describe(snapshot.inputs.proposed.original_parsed)+'\n\n修正後（実際の記帳対象）\n'+describe(snapshot.inputs.proposed.parsed)+
          '\n\n途中メモ：'+proof.claims.draft.value.fields.memo+'\n'+proof.claims.draft.value.items.map(i=>`${i.expense_id}：${i.note}`).join('\n');
        const label=document.createElement('label'), ack=document.createElement('input');ack.type='checkbox';ack.className='explicit';
        label.append(ack,document.createTextNode('案件・原本と修正後の全値を確認しました'));
        const post=document.createElement('button');post.textContent='この内容で記帳';post.dataset.action='post';post.disabled=true;
        ack.addEventListener('change',()=>post.disabled=!ack.checked||pending||blocked);
        review.append(heading,compare,label,post);review.hidden=false;
        row.querySelector('.source-hash').textContent=snapshot.source.sha256;
        result.textContent='確定前の再確認が必要';
      } else if(action==='post') {
        if(!proof||dirty||!review.querySelector('.explicit').checked)return;
        const posted=await send(proof,proof.claims.draft.value,true);
        result.textContent=posted.label;blocked=true;review.hidden=true;
        recovery.href=`${base}/r1/results/${posted.request_id}`;
      }
    } catch(err) {result.textContent=err.message||'UNKNOWN：結果不明・要照合';
      if(err instanceof TypeError){blocked=true;result.textContent='UNKNOWN：通信結果不明・入力を保持。再確定せず結果を照会してください。';}}
    finally {pending=false;buttons();const post=review.querySelector('[data-action=post]');if(post)post.disabled=blocked||!review.querySelector('.explicit').checked;}
  });
  window.addEventListener('beforeunload',e=>{if(dirty){e.preventDefault();e.returnValue='';}});
}
