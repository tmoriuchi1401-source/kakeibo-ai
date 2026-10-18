'use strict';
(() => {
  const form=document.querySelector('#draft-form'); if(!form) return;
  const context=JSON.parse(document.querySelector('#form-context').textContent);
  const button=form.querySelector('button[type=submit]'), feedback=document.querySelector('#feedback');
  const status=document.querySelector('#save-status'), recovery=document.querySelector('#recovery-link');
  const ack=document.querySelector('#acknowledged');
  const canAck=form.dataset.canAck!=='false';
  let dirty=false, pending=false, unknown=false, blocked=false, editEpoch=0;
  const integer=v=>/^-?\d+$/.test(v) ? Number(v) : v;
  form.addEventListener('input',event=>{
    if(form.dataset.mode==='detail') {
      dirty=true;editEpoch++;status.textContent='未保存の変更があります';
      const proposed=event.target.closest('.proposed');
      if(proposed) proposed.querySelector('strong').textContent='提案値（未保存の変更を含みます）';
    }
    if(ack && !pending && !unknown) button.disabled=!canAck||!ack.checked;
  });
  const link=document.querySelector('#reconfirm-link');
  if(link) link.addEventListener('click',e=>{if(dirty){e.preventDefault();feedback.textContent='未保存の変更があります。先に下書きを保存してください。';}});
  window.addEventListener('beforeunload',e=>{if(dirty||unknown){e.preventDefault();e.returnValue='';}});
  form.addEventListener('submit',async e=>{
    e.preventDefault(); if(pending||unknown||blocked||ack&&(!canAck||!ack.checked)) return;
    const sentEpoch=editEpoch;
    const payload={...context.request};
    if(form.dataset.mode==='detail') {
      const fields={...context.fields};
      for(const control of form.querySelectorAll('[name^="field:"]')) {const k=control.name.slice(6); fields[k]=k==='amount'?integer(control.value):control.value;}
      const items=[...form.querySelectorAll('.item')].map(item=>{
        const value={item_id:item.dataset.itemId};
        for(const control of item.querySelectorAll('[data-item]')) value[control.dataset.item]=control.dataset.item==='amount'?integer(control.value):control.value;
        return value;
      });
      payload.edits={fields,items};
    } else payload.acknowledged=ack.checked;
    pending=true;button.disabled=true;feedback.textContent='同じ要求IDで送信中です。入力を保持しています。';
    try {
      const response=await fetch(form.dataset.endpoint,{method:'POST',credentials:'same-origin',redirect:'error',
        headers:{'Content-Type':'application/json','X-CSRF-Token':form.dataset.csrf},body:JSON.stringify(payload)});
      const result=await response.json();
      if(!response.ok){feedback.textContent=result.message;recovery.hidden=false;
        if(response.status===503){unknown=true;status.textContent='受付結果が不明・入力を保持中';}
        return;}
      if(form.dataset.mode==='detail' && editEpoch!==sentEpoch){
        // A successful save must not discard typing that happened in flight.
        // Refresh only the proof, and only if the saved input is still exactly
        // OUR acknowledged save. Never merge a different instance's changes.
        blocked=true;dirty=true;recovery.hidden=false;
        feedback.textContent='送信中に追加した編集を保持しています。保存済み状態を確認中です。';
        try{
          const refreshed=await fetch(result.redirect,{credentials:'same-origin',redirect:'error'});
          const html=await refreshed.text();
          const node=new DOMParser().parseFromString(html,'text/html').querySelector('#form-context');
          const fresh=node&&JSON.parse(node.textContent);
          if(!refreshed.ok||!fresh||fresh.request.case_id!==context.request.case_id||fresh.request.input_digest!==result.result.input_digest)throw new Error('changed');
          context.request=fresh.request;context.fields=fresh.fields;blocked=false;
          feedback.textContent='送信時点の内容は保存済みです。その後の追加編集は未保存のまま保持しています。もう一度保存してください。';
        }catch(error){feedback.textContent='送信時点の内容は保存済みです。追加編集をこの画面に保持しています。現在の保存内容を別画面で確認してから続けてください。';}
        return;
      }
      dirty=false;unknown=false;window.location.assign(result.redirect);
    } catch(error) {unknown=true;status.textContent='受付結果が不明・入力を保持中';feedback.textContent='通信結果が不明です。新しい要求で再送せず、同じ要求の受付結果を確認してください。';recovery.hidden=false;}
    finally {pending=false;button.disabled=blocked||unknown||!!ack&&(!canAck||!ack.checked);}
  });
})();
