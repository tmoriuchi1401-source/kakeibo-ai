/** Narrow native Sheets review UI over the unchanged v1 category runner.
 * Historical scope uses the existing immutable preview/confirm engine.
 * Only this candidate's decisions enter the captured request; bank approvals
 * and other unsent decisions never enter it. No ledger writes occur here.
 */
const CC_UI = 'カテゴリ確認';
const CC_LOG = '_カテゴリ確認ログ';
const CC_MARKERS = ['■ 1. カテゴリを選ぶ・今後の自動分類',
  '■ 2. 過去分の固定プレビュー','■ 3. 固定プレビューを確認して反映'];
const CC_FIRST = 30;
const CC_CELLS = {category:'A15',future:'A17',scope:'A20',action:'A24',status:'A25'};
const CC_FILTERS = ['対象月の未分類','他月の未処理','その他の候補','すべて'];

function ccText_(v) { return String(v || '').normalize('NFKC').trim().replace(/\s+/g,' '); }
function ccJson_(v) { try { return JSON.parse(String(v)); } catch (_) { return {}; } }
function ccCanonical_(v) {
  if (Array.isArray(v)) return '[' + v.map(ccCanonical_).join(',') + ']';
  if (v && typeof v === 'object') return '{' + Object.keys(v).sort().map(k=>JSON.stringify(k)+':'+ccCanonical_(v[k])).join(',') + '}';
  return JSON.stringify(v);
}
function ccDigest_(v) {
  return Utilities.computeDigest(Utilities.DigestAlgorithm.SHA_256,ccCanonical_(v),Utilities.Charset.UTF_8)
    .map(b=>('0'+((b+256)%256).toString(16)).slice(-2)).join('');
}
function ccRows_(sheet,width) {
  return sheet.getRange(1,1,Math.max(1,sheet.getLastRow()),width).getDisplayValues();
}
function ccSet_(range,value) {
  // Display source text literally, including a merchant beginning with '='.
  range.setValue(typeof value === 'string' && value.startsWith('=') ? "'"+value : value);
}
function ccFit_(sheet,row,text,width=25) {
  const lines=String(text).split('\n').reduce((n,line)=>n+Math.max(1,Math.ceil(Array.from(line).reduce((s,c)=>s+(c.charCodeAt(0)<128?0.55:1),0)/width)),0);
  sheet.setRowHeight(row,Math.max(34,lines*14+8));
}
function ccState_(sheet) { return ccJson_(sheet.getRange('C1').getValue()); }
function ccSave_(sheet,state) { sheet.getRange('C1').setValue(JSON.stringify(state)); }
function ccSection_(rows,section) {
  const hits=rows.map((r,i)=>r[0]===CC_MARKERS[section]?i:-1).filter(i=>i>=0);
  if(hits.length!==1) throw Error('カテゴリ処理表の構成が変わりました。');
  const first=hits[0]+2;
  const end=rows.findIndex((r,i)=>i>=first && r[0].startsWith('■ '));
  return {first,end:end<0?rows.length:end};
}
function ccIdentity_(proof) { return ['service',ccText_(proof.source),ccText_(proof.account_alias),ccText_(proof.merchant)]; }
function ccAccount_(id) { const p=String(id).split(':'); return p.length>=4 && p[0]==='bankpdf'?p[2]:''; }
function ccMembers_(proof,expenses,imports) {
  const wanted=ccIdentity_(proof);
  return expenses.slice(1).filter(e=>{
    const tx=imports[String(e[10])];
    return tx && e[12]==='active' && tx[8]==='auto_expense' && tx[9]===e[0]
      && ccText_(tx[2])===wanted[1] && ccAccount_(tx[0])===wanted[2] && ccText_(tx[5])===wanted[3];
  }).sort((a,b)=>String(a[1]).localeCompare(String(b[1])) || String(a[0]).localeCompare(String(b[0])));
}
function ccSummary_(members,month) {
  const fallback=members.filter(e=>e[5]==='その他' && e[6]==='未分類');
  const selected=fallback.filter(e=>String(e[1]).slice(0,7)===month);
  const total=rs=>rs.reduce((s,e)=>s+Number(String(e[4]).replace(/,/g,'')),0);
  return {monthCount:selected.length,monthAmount:total(selected),allCount:fallback.length,allAmount:total(fallback),
    samples:members.filter(e=>String(e[1]).slice(0,7)<month).slice(-3).reverse()};
}
function ccPartition_(candidates,filter) {
  const groups=[candidates.filter(c=>c.summary.monthCount>0),
    candidates.filter(c=>!c.summary.monthCount && c.summary.allCount>0),
    candidates.filter(c=>!c.summary.allCount)];
  return {groups,visible:filter==='すべて'?candidates:groups[CC_FILTERS.indexOf(filter)]||groups[0]};
}
function ccInitial_(candidate,month,filter) {
  return {v:1,key:candidate.key,sig:candidate.sig,proof:candidate.proof,month,filter,
    category:candidate.physical[2]?candidate.physical[2].replace('｜',' ＞ '):'',
    future:'OFF',scope:'反映しない'};
}
function ccModel_(ss) {
  const legacy=ss.getSheetByName(CATEGORY_UI), rows=ccRows_(legacy,12), part=ccSection_(rows,0);
  const expenses=ccRows_(ss.getSheetByName('支出明細'),13);
  const imported=ccRows_(ss.getSheetByName('取込データ'),12), imports={};
  imported.slice(1).forEach(r=>{if(r[0]) imports[r[0]]=r;});
  if(expenses[0][0]!=='支出ID' || imported[0][0]!=='取込ID') throw Error('台帳の列構成が変わりました。');
  const raw=legacy.getRange('E5').getDisplayValue();
  const month=/^\d{4}-\d{2}$/.test(raw)?raw:ss.getSheetByName('ホーム').getRange('B4').getDisplayValue();
  if(!/^\d{4}-\d{2}$/.test(month)) throw Error('対象月を確認してください。');
  const completed=new Set(ccRows_(ss.getSheetByName(CC_LOG),8).slice(1).map(r=>r[0]));
  const candidates=[];
  for(let i=part.first;i<part.end;i++) {
    const row=rows[i], proof=ccJson_(row[11]);
    if(!row[6] || proof.kind!=='service' || !proof.source || !proof.merchant) continue;
    const members=ccMembers_(proof,expenses,imports), summary=ccSummary_(members,month);
    const sig=ccDigest_({condition:ccIdentity_(proof),month,transactions:members.map(e=>e.slice(0,13))});
    const final=String(row[1]).split('\n').pop();
    if(completed.has(sig) || (!summary.allCount && ['登録済み','登録しない','既存ルールあり・追加登録しない'].includes(final))) continue;
    candidates.push({row:i+1,key:row[6],proof,members,summary,sig,physical:row});
  }
  return {legacy,rows,month,candidates};
}
function ccDropdown_(sheet,a1,values) {
  sheet.getRange(a1).setDataValidation(SpreadsheetApp.newDataValidation().requireValueInList(values,true).setAllowInvalid(false).build());
}

function installCategoryConfirmation() {
  const lock=LockService.getScriptLock(); if(!lock.tryLock(1000)) throw Error('処理完了後に実行してください。');
  try {
    const ss=categorySpreadsheet_(),queue=ss.getSheetByName(CATEGORY_QUEUE);
    if(!queue || CATEGORY_BUSY.includes(String(queue.getRange('B2').getValue()))) throw Error('既存受付の完了後に実行してください。');
    if(ss.getSheetByName(CC_UI)) throw Error('導入済みです。refreshCategoryConfirmation を使ってください。');
    const log=ss.getSheetByName(CC_LOG)||ss.insertSheet(CC_LOG);
    log.getRange('A1:H1').setValues([['表示スナップショット','候補キー','受付ID','カテゴリ','自動分類','過去範囲','対象月','確定日時']]);log.hideSheet();
    const sheet=ss.insertSheet(CC_UI);sheet.setHiddenGridlines(true);sheet.setFrozenRows(2);
    sheet.setColumnWidth(1,290);sheet.setColumnWidth(2,44);sheet.hideColumns(3,sheet.getMaxColumns()-2);
    sheet.getRange('A1:B28').mergeAcross();sheet.getRange('A1:B28').setWrap(true).setVerticalAlignment('middle').setFontSize(10).setFontColor('#1f2933');
    sheet.setRowHeights(1,28,22);sheet.setRowHeight(4,64);sheet.setRowHeight(6,48);
    [7,10,11,12,18,21,23,25].forEach(r=>sheet.setRowHeight(r,34));
    [13,15,17,20,24].forEach(r=>sheet.setRowHeight(r,44));sheet.setRowHeight(27,10);
    [[1,'カテゴリ確認'],[9,'判断用の過去取引（対象月より前）'],[14,'カテゴリ'],[16,'今後の自動分類'],[18,'ON：同じ条件の今後の未分類を自動分類\nOFF：追加登録しない（既存ルールは停止しません）'],[19,'過去分への反映'],[22,'分類済みの明細は変更しません。'],[28,'未確認一覧 · 右の「開く」で選択']].forEach(([r,v])=>ccSet_(sheet.getRange('A'+r),v));
    sheet.getRange('A1:B2').setBackground('#dceee8').setFontWeight('bold');sheet.getRange('A4').setFontSize(16).setFontWeight('bold');
    [14,16,19,28].forEach(r=>sheet.getRange('A'+r+':B'+r).setBackground('#f3f6f8').setFontWeight('bold'));
    [15,17,20].forEach(r=>sheet.getRange('A'+r+':B'+r).setBackground('#eef5ff').setFontSize(12));
    sheet.getRange('A24:B24').setBackground('#b9ddca').setFontWeight('bold').setFontSize(12);
    sheet.getRange('A29:B29').setValues([['取引先 / 対象月の未分類 / 選択カテゴリ','開く']]).setBackground('#f3f6f8').setFontWeight('bold');
    ccDropdown_(sheet,'A17',['OFF','ON']);ccDropdown_(sheet,'A20',['反映しない','対象月のみ','全期間']);
    ccDropdown_(sheet,'A13',['操作を選択','対象取引をすべて見る']);ccSet_(sheet.getRange('A13'),'操作を選択');
    ccSave_(sheet,{v:1});ccRefresh_(ss);
    ss.setActiveSheet(sheet);ss.moveActiveSheet(ss.getSheetByName(CATEGORY_UI).getIndex());
  } finally {lock.releaseLock();}
}
function refreshCategoryConfirmation() {
  const lock=LockService.getScriptLock();if(!lock.tryLock(1000))return;
  try{const ss=categorySpreadsheet_();ccSync_(ss);ccRefresh_(ss);}finally{lock.releaseLock();}
}
function ccRefresh_(ss,key) {
  const sheet=ss.getSheetByName(CC_UI);if(!sheet)return;
  ccDropdown_(sheet,'A3',['操作を選択','表示・処理結果を更新']);ccSet_(sheet.getRange('A3'),'操作を選択');sheet.setRowHeight(3,36);
  const prior=ccState_(sheet); if(prior.pending)return;
  const model=ccModel_(ss),filter=CC_FILTERS.includes(prior.filter)?prior.filter:CC_FILTERS[0];
  const partition=ccPartition_(model.candidates,filter),candidates=partition.visible;
  ccDropdown_(sheet,'A28',CC_FILTERS);ccSet_(sheet.getRange('A28'),filter);sheet.setRowHeight(28,44);
  const current=candidates.find(c=>c.key===(key||prior.key))||candidates[0];
  const helper=ss.getSheetByName('_支出明細カテゴリ候補'), choices=ccRows_(helper,4).slice(1).filter(r=>r[2]&&r[3]);
  const labels=choices.map(r=>r[2]+' ＞ '+r[3]);
  sheet.getRange('D2:E'+(labels.length+1)).setValues(choices.map((r,i)=>[labels[i],r[0]]));
  sheet.getRange('A15').setDataValidation(SpreadsheetApp.newDataValidation().requireValueInRange(sheet.getRange(2,4,labels.length,1),true).setAllowInvalid(false).build());
  ccSet_(sheet.getRange('A2'),'対象月の未確認 '+partition.groups[0].length+'候補\n他月 '+partition.groups[1].length+' / その他 '+partition.groups[2].length+'候補');sheet.setRowHeight(2,40);
  const last=Math.max(CC_FIRST,sheet.getLastRow());sheet.getRange(CC_FIRST,1,last-CC_FIRST+1,3).clearContent().clearDataValidations();
  if(candidates.length) {
    const needed=CC_FIRST+candidates.length-1;if(needed>sheet.getMaxRows())sheet.insertRowsAfter(sheet.getMaxRows(),needed-sheet.getMaxRows());
    candidates.forEach((c,i)=>{
      const count=c.summary.monthCount || c.summary.allCount,amount=c.summary.monthCount?c.summary.monthAmount:c.summary.allAmount;
      const context=c.summary.monthCount?'対象月':c.summary.allCount?'対象月以外の未分類':'自動分類の検討';
      const line=c.proof.merchant+'\n'+context+' '+count+'件 / '+amount.toLocaleString('ja-JP')+'円 · '+(c.physical[2]?c.physical[2].replace('｜',' ＞ '):'未選択')+' · 確認待ち';
      ccSet_(sheet.getRange(CC_FIRST+i,1),line);sheet.getRange(CC_FIRST+i,3).setValue(c.key);
      ccFit_(sheet,CC_FIRST+i,line,22);
    });
    sheet.getRange(CC_FIRST,1,candidates.length,2).setWrap(true).setVerticalAlignment('middle');
    ccDropdown_(sheet,'B'+CC_FIRST+':B'+needed,['開く']);
  }
  if(!current){
    [4,5,6,7,8,10,11,12,15,17,20,21,23,24,25,26].forEach(r=>sheet.getRange('A'+r).clearContent());
    ccSet_(sheet.getRange('A4'),'この区分に未確認の候補はありません');
    ccSet_(sheet.getRange('A7'),'対象月 '+model.month);ccSet_(sheet.getRange('A17'),'OFF');ccSet_(sheet.getRange('A20'),'反映しない');
    ccDropdown_(sheet,'A24',['操作を選択']);ccSet_(sheet.getRange('A24'),'操作を選択');
    ccSet_(sheet.getRange('A25'),'下の一覧フィルタで他月・その他の候補を確認できます。');ccSave_(sheet,{v:1,filter});return;
  }
  const same=prior.key===current.key && prior.sig===current.sig && !key;
  const state=same?prior:ccInitial_(current,model.month,filter);state.filter=filter;
  ccSave_(sheet,state);ccSet_(sheet.getRange('A4'),current.proof.merchant);
  const glyphs=Array.from(current.proof.merchant).reduce((n,c)=>n+(c.charCodeAt(0)<128?0.55:1),0);
  sheet.setRowHeight(4,Math.max(64,Math.ceil(glyphs/19)*24+12));
  const representative=current.members.find(e=>e[0]===current.physical[7]);
  ccSet_(sheet.getRange('A5'),'明細原文：'+(representative?representative[3]:current.proof.item_name||''));
  ccSet_(sheet.getRange('A6'),'一致条件：摘要「'+current.proof.merchant+'」と完全一致\nデータ元：'+current.proof.source+(current.proof.account_alias?' / 口座：'+current.proof.account_alias:'')+' / 金額不問・自動計上のみ');
  [5,6].forEach(r=>ccFit_(sheet,r,sheet.getRange('A'+r).getDisplayValue()));
  ccSet_(sheet.getRange('A7'),'対象月 '+model.month+' の未分類：'+current.summary.monthCount+'件 / '+current.summary.monthAmount.toLocaleString('ja-JP')+'円');
  ccSet_(sheet.getRange('A8'),'全期間の未分類（参考）：'+current.summary.allCount+'件 / '+current.summary.allAmount.toLocaleString('ja-JP')+'円');
  [10,11,12].forEach((r,i)=>{const e=current.summary.samples[i];ccSet_(sheet.getRange('A'+r),e?e[1]+'  '+Number(String(e[4]).replace(/,/g,'')).toLocaleString('ja-JP')+'円\n'+e[2]:(i===0?'対象月より前の一致取引はありません':''));sheet.setRowHeight(r,e?Math.max(34,Math.ceil(Array.from(e[2]).length/26)*14+20):i===0?34:6);});
  ['category','future','scope'].forEach(k=>ccSet_(sheet.getRange(CC_CELLS[k]),state[k]));
  ccPaint_(sheet,state);ccSet_(sheet.getRange('A26'),'表示更新 '+Utilities.formatDate(new Date(),'Asia/Tokyo','yyyy-MM-dd HH:mm'));
}
function ccPaint_(sheet,state) {
  const fixed=state.fixed;
  const impact=state.scope==='反映しない'?'過去明細への変更は0件です。':fixed?'固定対象 '+fixed.count+'件 / '+Number(fixed.amount).toLocaleString('ja-JP')+'円へ反映します。':'「対象件数を確認」で変更対象を固定します。';
  ccSet_(sheet.getRange('A21'),impact);
  ccSet_(sheet.getRange('A23'),(state.category||'カテゴリ未選択')+' / 自動分類 '+state.future+'\n過去：'+state.scope);
  const action=state.scope==='反映しない'||fixed?'この内容で確定':'対象件数を確認';
  ccDropdown_(sheet,'A24',['操作を選択',action]);ccSet_(sheet.getRange('A24'),'操作を選択');
  ccSet_(sheet.getRange('A25'),state.message||'内容を確認して、最後に操作を選んでください。');
  ccFit_(sheet,25,sheet.getRange('A25').getDisplayValue());
}
function categoryConfirmationEdited(e) {
  if(!e || !e.range || e.range.getSheet().getName()!==CC_UI)return false;
  const ss=categorySpreadsheet_(),sheet=e.range.getSheet(),cell=e.range.getA1Notation();
  if(cell==='A3' && e.value==='表示・処理結果を更新'){checkCategoryStatus();refreshCategoryConfirmation();return true;}
  if(cell==='A24' && ['対象件数を確認','この内容で確定'].includes(e.value)) {
    try{submitCategoryInput({prepare:()=>ccPrepare_(ss,e.value),accepted:id=>ccAccepted_(ss,id)});}
    catch(error){ccSet_(sheet.getRange('A24'),'操作を選択');ccSet_(sheet.getRange('A25'),'受付できませんでした。カテゴリ・設定・固定対象を確認し、表示を更新してください。');}
    return true;
  }
  const lock=LockService.getScriptLock();if(!lock.tryLock(10000))return true;
  try{
    const wasPending=!!ccState_(sheet).pending;ccSync_(ss);const state=ccState_(sheet);if(state.pending || wasPending){['category','future','scope'].forEach(k=>ccSet_(sheet.getRange(CC_CELLS[k]),state[k]||''));ccSet_(sheet.getRange('A24'),'操作を選択');if(state.pending)ccSet_(sheet.getRange('A25'),'受付済みです。完了まで設定は変更できません。');return true;}
    if(cell==='A28' && CC_FILTERS.includes(e.value)) {
      ccSave_(sheet,{v:1,filter:e.value});ccRefresh_(ss);
    } else if(e.range.getRow()>=CC_FIRST && e.range.getColumn()===2 && e.value==='開く') {
      const key=sheet.getRange(e.range.getRow(),3).getValue();ccRefresh_(ss,key);ss.setActiveSheet(sheet);sheet.getRange('A4').activate();
    } else if(['A15','A17','A20'].includes(cell)) {
      const field={A15:'category',A17:'future',A20:'scope'}[cell];state[field]=e.value||'';delete state.fixed;delete state.message;ccSave_(sheet,state);ccPaint_(sheet,state);
    } else if(cell==='A13' && e.value==='対象取引をすべて見る'){ccSet_(sheet.getRange('A13'),'操作を選択');ccDetails_(ss,state);}
  }finally{lock.releaseLock();}
  return true;
}

function ccPayload_(proof,pair) {
  return ccCanonical_({kind:'service',source:ccText_(proof.source),account_alias:ccText_(proof.account_alias),billing_name:ccText_(proof.merchant),merchant:'',product_id:'',product_name:'',amount:null,category:pair,rule_id:'adhoc',revision:1,saved_rule:false});
}
function ccCapture_(rows,key,rawCategory,stage,state) {
  const output=rows.map(r=>r.slice(0,12)),rule=ccSection_(output,0),past=ccSection_(output,1),confirm=ccSection_(output,2);
  // Bind the old preview-only header. Never interpret F as an all-month apply.
  output[rule.first-1][5]='過去分の候補に追加';
  let selected=-1;
  for(let i=rule.first;i<rule.end;i++){output[i][4]='未選択';output[i][5]='FALSE';if(output[i][6]===key)selected=i;}
  if(selected<0)throw Error('候補が変わりました。一覧を更新してください。');
  output[selected][2]=rawCategory;output[selected][3]='';output[selected][4]=stage==='preview'?'未選択':state.future==='ON'?'登録する':'登録しない';output[selected][5]=stage==='preview'?'TRUE':'FALSE';
  for(let i=past.first;i<past.end;i++)output[i][4]='FALSE';
  for(let i=confirm.first;i<confirm.end;i++)output[i][2]='FALSE';
  const bank=output.findIndex(r=>r[0]==='■ 4. 銀行取引をまとめて確認');if(bank>=0)for(let i=bank+2;i<output.length;i++)if(output[i][8]==='group')output[i][5]='FALSE';
  if(stage==='preview') {
    const pair=rawCategory.split('｜'),month=state.month;
    const physical=['表示中の条件',pair.join('\n'),month,state.scope==='全期間'?'過去すべて':month,'TRUE','','表示中の条件（過去分のみ）','displayed:'+key,'1',ccPayload_(state.proof,pair),'',''];
    // Logical past controls are mapped to physical A:E,G:K.
    const previous=output.findIndex((r,i)=>i>=past.first && i<past.end && r[7]==='displayed:'+key);
    if(previous>=0)output[previous]=physical;else output.splice(past.end,0,physical);
  } else if(state.scope!=='反映しない' && state.fixed && state.fixed.count>0) {
    const hit=output.findIndex((r,i)=>i>=confirm.first&&i<confirm.end&&r[6]===state.fixed.id);
    if(hit<0)throw Error('固定対象が変わりました。対象件数を再確認してください。');
    output[hit][2]='TRUE';
  }
  return output;
}
function ccPrepare_(ss,action) {
  const sheet=ss.getSheetByName(CC_UI),state=ccState_(sheet);
  if(state.pending)throw Error('受付済みです。完了をお待ちください。');
  if(['category','future','scope'].some(k=>sheet.getRange(CC_CELLS[k]).getDisplayValue()!==state[k]))throw Error('選択の保存を待ってから、もう一度操作してください。');
  const model=ccModel_(ss),candidate=model.candidates.find(c=>c.key===state.key);
  if(!candidate || candidate.sig!==state.sig || state.month!==model.month)throw Error('取引または対象月が変わりました。一覧を更新してください。');
  const choices=sheet.getRange(2,4,ss.getSheetByName('_支出明細カテゴリ候補').getLastRow()-1,2).getDisplayValues();
  const pair=choices.find(r=>r[0]===state.category);if(!pair || !['ON','OFF'].includes(state.future) || !['反映しない','対象月のみ','全期間'].includes(state.scope))throw Error('カテゴリと設定を選んでください。');
  const stage=action==='対象件数を確認'?'preview':'confirm';
  if(stage==='confirm' && state.scope!=='反映しない' && !state.fixed)throw Error('先に対象件数を確認してください。');
  if(state.fixed && !ccFixedValid_(ss,state))throw Error('固定対象が変更されています。再確認してください。');
  state.stage=stage;state.proof=candidate.proof;
  state.before=ccRows_(ss.getSheetByName('カテゴリ過去反映要求'),11).slice(1).map(r=>r[0]);
  // UI-only category entry is retained in the legacy surface. No approvals
  // are written there: other pending controls remain unsubmitted.
  model.legacy.getRange(candidate.row,3).setValue(pair[1]);model.rows[candidate.row-1][2]=pair[1];
  ccSave_(sheet,state);
  return ccCapture_(model.rows.slice(5),state.key,pair[1],stage,state);
}
function ccAccepted_(ss,id) {
  const sheet=ss.getSheetByName(CC_UI),state=ccState_(sheet);state.pending=id;ccSave_(sheet,state);
  ccSet_(sheet.getRange('A24'),'操作を選択');ccSet_(sheet.getRange('A25'),'受付済み · '+(state.stage==='preview'?'対象件数を確認中':'確定処理中'));
}
function ccFixedValid_(ss,state) {
  const fixed=state.fixed;if(!fixed)return true;
  if(!fixed.count)return true;
  const row=ccRows_(ss.getSheetByName('カテゴリ過去反映要求'),11).find(r=>r[0]===fixed.id);
  return !!row && row[2]==='previewed' && row[8]===fixed.digest && Number(row[6])===fixed.count && Number(row[7])===Number(fixed.amount);
}
function ccSync_(ss) {
  const sheet=ss.getSheetByName(CC_UI);if(!sheet)return;
  const state=ccState_(sheet);if(!state.pending)return;
  const queue=ss.getSheetByName(CATEGORY_QUEUE).getRange('A2:J2').getDisplayValues()[0];
  if(queue[0]!==state.pending){ccSet_(sheet.getRange('A25'),'受付が変わりました。実行記録の確認が必要です。');return;}
  if(CATEGORY_BUSY.includes(queue[1]))return;
  if(queue[1]!=='complete'){delete state.pending;state.message='中断・要確認（'+queue[1]+'）。処理結果：'+queue[5];ccSave_(sheet,state);ccPaint_(sheet,state);return;}
  if(state.stage==='preview') {
    const rows=ccRows_(ss.getSheetByName('カテゴリ過去反映要求'),11).filter(r=>!state.before.includes(r[0]) && r[2]==='previewed');
    const selected=rows.filter(r=>{const p=ccJson_(r[3]);return ccCanonical_(ccIdentity_({...p,merchant:p.billing_name||p.merchant}))===ccCanonical_(ccIdentity_(state.proof))&&p.category.join(' ＞ ')===state.category;});
    if(selected.length===1){const r=selected[0];state.fixed={id:r[0],count:Number(r[6]),amount:Number(r[7]),digest:r[8]};state.message='対象を固定しました。件数・金額を確認して確定してください。';}
    else{
      const legacyRows=ccRows_(ss.getSheetByName(CATEGORY_UI),12);
      const empty=legacyRows.some(r=>r[7]==='displayed:'+state.key && String(r[0]).includes('preview_empty'));
      if(!selected.length && empty){state.fixed={id:'',count:0,amount:0,digest:''};state.message='反映対象は0件です。設定を確認して確定してください。';}
      else state.message='対象を固定できませんでした。旧シートの処理結果を確認してください。';
    }
    delete state.pending;ccSave_(sheet,state);ccPaint_(sheet,state);return;
  }
  if(state.scope!=='反映しない' && state.fixed && state.fixed.count>0) {
    const fixed=ccRows_(ss.getSheetByName('カテゴリ過去反映要求'),11).find(r=>r[0]===state.fixed.id);
    if(!fixed || fixed[2]!=='complete'){delete state.pending;state.message='反映が完了していません。対象と処理結果を確認してください。';ccSave_(sheet,state);ccPaint_(sheet,state);return;}
  }
  const log=ss.getSheetByName(CC_LOG),current=ccModel_(ss).candidates.find(c=>c.key===state.key);
  const signature=current?current.sig:state.sig;
  if(!ccRows_(log,8).some(r=>r[2]===state.pending))log.appendRow([signature,state.key,state.pending,state.category,state.future,state.scope,state.month,new Date().toISOString()]);
  ccSave_(sheet,{v:1,filter:state.filter});ccRefresh_(ss);ccSet_(sheet.getRange('A25'),'前の候補を確定しました。'+queue[5]);
}
function ccDetails_(ss,state) {
  if(!state.key)return;
  const candidate=ccModel_(ss).candidates.find(c=>c.key===state.key);if(!candidate)return;
  let detail=ss.getSheetByName('カテゴリ対象取引');if(!detail)detail=ss.insertSheet('カテゴリ対象取引');
  detail.clear();detail.setHiddenGridlines(true);detail.setColumnWidth(1,250);detail.setColumnWidth(2,84);detail.hideColumns(3,detail.getMaxColumns()-2);
  detail.getRange('A1:B1').merge().setValue('判断用の一致取引 · 対象月 '+state.month+' と過去を分離');
  detail.getRange('A2:B2').setValues([['日付 / 摘要 / 区分','金額']]);
  const rows=candidate.members.map(e=>[e[1]+' / '+e[2]+'\n'+(String(e[1]).slice(0,7)===state.month?'対象月':'判断用過去・他月')+' / '+e[5]+' ＞ '+e[6],Number(String(e[4]).replace(/,/g,''))]);
  if(rows.length){if(rows.length+2>detail.getMaxRows())detail.insertRowsAfter(detail.getMaxRows(),rows.length+2-detail.getMaxRows());detail.getRange(3,1,rows.length,2).setValues(rows.map(r=>[r[0].startsWith('=')?"'"+r[0]:r[0],r[1]])).setWrap(true);detail.getRange(3,2,rows.length,1).setNumberFormat('#,##0"円"');detail.setRowHeights(3,rows.length,56);}
  detail.getRange('A1:B2').setBackground('#f3f6f8').setFontWeight('bold');ss.setActiveSheet(detail);
}
