/** PDF-only captured requests. Uses the existing category-submit credential,
 * scopes and edit trigger. No Drive access, AI, accounting or actor inference.
 * Install this file into the same existing Apps Script project; no new scopes.
 */
const PDF_UI = 'PDFページ確認';
const PDF_QUEUE = '_PDF確認受付';
const PDF_WORKFLOW = 'pdf-grouping-review.yml';

function pdfGroupingDispatchEnabled_() {
  // Limited live introduction captures human intent only. An absent property
  // must never dispatch a worker, including when existing category auth exists.
  return PropertiesService.getScriptProperties().getProperty('PDF_GROUPING_DISPATCH_ENABLED') === 'true';
}

function pdfGroupingEdited(e) {
  if (e && e.range && e.range.getSheet().getName() === PDF_UI &&
      e.range.getSheet().getRange(1,3).getDisplayValue() === 'pdf-page-review-v1') {
    return pdfPageReviewEdited_(e);
  }
  if (!e || !e.range || e.range.getSheet().getName() !== PDF_UI ||
      e.range.getNumRows() !== 1 || e.range.getNumColumns() !== 1 ||
      e.range.getColumn() !== 8 || e.range.getRow() < 2 || !e.value) return;
  submitPdfGroupingRow_(e.range.getRow());
}

function pdfPageCard_(sheet, token) {
  const data = sheet.getRange(2,1,Math.min(1000,sheet.getLastRow()-1),14).getDisplayValues();
  const positions = [];
  const rows = data.filter((r,i) => {
    const match = r[2] === 'pdf-page-review-v1' && r[3] === token;
    if (match) positions.push(i+2);
    return match;
  });
  if (!rows.length || rows.some(r => r[4] !== rows[0][4])) throw new Error('PDF確認画面を更新してください。');
  const fields = {};
  rows.forEach(r => fields[r[5]] = r[1]);
  const original = positions[rows.findIndex(r => r[5] === 'original')];
  const rich = sheet.getRange(original,2).getRichTextValue();
  return {snapshot:{identity:JSON.parse(rows[0][4]),token:token,
    rows:rows.map(r => [r[5],r[0],r[1]]),original_link:rich ? rich.getLinkUrl() || '' : ''},fields:fields,positions:positions};
}

function pdfPageMedicalValidation_(sheet, card) {
  const f = card.fields;
  const validDate = /^(\d{4})[-/](\d{1,2})[-/](\d{1,2})$/.exec(f.date || '');
  let dateComplete = false;
  if (validDate) {
    const y=Number(validDate[1]),m=Number(validDate[2]),d=Number(validDate[3]);
    const day=new Date(y,m-1,d);
    dateComplete=day.getFullYear()===y && day.getMonth()===m-1 && day.getDate()===d;
  }
  const amount = String(f.amount || '').replace(/,/g,'');
  const complete = dateComplete && String(f.facility || '').trim() && /^\d+$/.test(amount) &&
    Number(amount)>0 && ['医療費','薬代','医療費（その他）'].includes(f.category);
  const index=card.snapshot.rows.findIndex(r => r[0] === 'medical_action');
  if (index<0) return;
  const choices=complete ? ['保留','医療費を確定','既存支出と重複（紐付け）','重複候補と別の支出として確定'] : ['保留'];
  sheet.getRange(card.positions[index],2).setDataValidation(SpreadsheetApp.newDataValidation()
    .requireValueInList(choices,true).setAllowInvalid(false).build());
  return !!complete;
}

function pdfPageGeneralValidation_(sheet, card) {
  const f=card.fields, parts=/^(\d{4})[-/](\d{1,2})[-/](\d{1,2})$/.exec(f.date || '');
  let dateComplete=false;
  if (parts) {
    const y=Number(parts[1]),m=Number(parts[2]),d=Number(parts[3]),day=new Date(y,m-1,d);
    dateComplete=day.getFullYear()===y && day.getMonth()===m-1 && day.getDate()===d;
  }
  const amount=String(f.amount || '').replace(/,/g,''), category=String(f.category || '').split('｜');
  // Existing strict dropdown is convenience, not authority. The worker checks
  // current master membership and Drive/source/owner evidence independently.
  const complete=dateComplete && /^\d+$/.test(amount) && Number(amount)>0 && Number(amount)<=99999999 &&
    category.length===2 && category.every(v=>v.trim()) &&
    String(f.merchant || '').length<=100 && String(f.payment || '').length<=50 && String(f.memo || '').length<=300;
  const index=card.snapshot.rows.findIndex(r=>r[0]==='manual_action');
  if (index<0) return false;
  sheet.getRange(card.positions[index],2).setDataValidation(SpreadsheetApp.newDataValidation()
    .requireValueInList(complete ? ['保留','一般支出を確定'] : ['保留'],true).setAllowInvalid(false).build());
  return !!complete;
}

function pdfPageReviewEdited_(e) {
  if (!e.range || e.range.getNumRows()!==1 || e.range.getNumColumns()!==1 ||
      e.range.getColumn()!==2 || e.range.getRow()<2) return;
  const sheet=e.range.getSheet();
  const tech=sheet.getRange(e.range.getRow(),3,1,4).getDisplayValues()[0];
  if (tech[0]!=='pdf-page-review-v1') return;
  const card=pdfPageCard_(sheet,tech[1]);
  if (card.snapshot.identity.kind==='medical') {
    const complete=pdfPageMedicalValidation_(sheet,card);
    if (tech[3]==='medical_action' && e.value && e.value!=='保留' && !complete) {
      sheet.getRange(e.range.getRow(),2).setValue('保留');
      return; // Typing alone never queues confirmation or writes accounting.
    }
  }
  if (card.snapshot.identity.kind==='general_manual') {
    const complete=pdfPageGeneralValidation_(sheet,card);
    if (tech[3]==='manual_action' && e.value && e.value!=='保留' && !complete) {
      sheet.getRange(e.range.getRow(),2).setValue('保留');return;
    }
  }
  if (!['kind_action','group_action','medical_action','manual_action'].includes(tech[3]) || !e.value) return;
  submitPdfPageCard_(sheet,card);
}

function submitPdfPageCard_(sheet, initial) {
  const lock=LockService.getScriptLock();
  if (!lock.tryLock(1000)) return;
  try {
    const ss=categorySpreadsheet_(),queue=ss.getSheetByName(PDF_QUEUE);
    const card=pdfPageCard_(sheet,initial.snapshot.token); // capture under lock
    const f=card.fields, kind=card.snapshot.identity.kind;
    const operation=kind==='medical' ? f.medical_action : kind==='general_manual' ? f.manual_action : kind==='grouping' ? f.group_action : f.kind_action;
    if (kind==='medical' && operation!=='保留' && !pdfPageMedicalValidation_(sheet,card)) return;
    if (kind==='general_manual' && operation!=='保留' &&
        (operation!=='一般支出を確定' || !pdfPageGeneralValidation_(sheet,card))) return;
    if (kind==='page_kind' && operation==='種別を確定' && !['一般','医療','給与','判定不能'].includes(f.kind_choice)) return;
    if (kind==='grouping' && ['分割','結合'].includes(operation) && !f.group_target) return;
    if (!queue || queue.getLastRow()>=1001) throw new Error('受付履歴を管理者に確認してください。');
    const prior=queue.getLastRow()>1 ? queue.getRange(2,1,queue.getLastRow()-1,6).getValues() : [];
    if (prior.some(r => ['accepted','dispatching'].includes(String(r[1])) &&
        (()=>{try{return JSON.parse(r[2]).token===card.snapshot.token;}catch(_){return true;}})())) return;
    const enabled=pdfGroupingDispatchEnabled_();
    if (enabled && !PropertiesService.getUserProperties().getProperty('CATEGORY_GITHUB_TOKEN')) throw new Error('既存連携設定を確認してください。');
    const id=Utilities.getUuid();
    queue.appendRow([id,enabled ? 'dispatching' : 'accepted',JSON.stringify(card.snapshot),new Date().toISOString(),'','']);
    const index=card.snapshot.rows.findIndex(r=>r[0]==='result');
    sheet.getRange(card.positions[index],2).setValue('受付中・保存とread-back待ち');
    SpreadsheetApp.flush();
    if (!enabled) return; // Draft phase: operator only, no automatic activation.
    let response;
    try {response=categoryFetch_('/actions/workflows/'+PDF_WORKFLOW+'/dispatches','post',{ref:'main',inputs:{mode:'review',request_id:id}});}
    catch (_) {return;} // ambiguous delivery: do not retry
    if (response.getResponseCode()!==204) queue.getRange(queue.getLastRow(),2).setValue('error');
  } finally {lock.releaseLock();}
}

function submitPdfGroupingRow_(rowNumber) {
  const lock = LockService.getScriptLock();
  if (!lock.tryLock(1000)) return;
  try {
    const ss = categorySpreadsheet_();
    const sheet = ss.getSheetByName(PDF_UI);
    const queue = ss.getSheetByName(PDF_QUEUE);
    if (!sheet || !queue || rowNumber < 2 || rowNumber > 1001) throw new Error('PDF確認画面を更新してください。');
    const snapshot = sheet.getRange(rowNumber,1,1,16).getDisplayValues()[0];
    if (!['確定','分割','結合','拒否','保留'].includes(snapshot[7])) return;
    if (['分割','結合'].includes(snapshot[7]) && !snapshot[8]) {
      sheet.getRange(rowNumber,10).setValue('対象groupを選んでから操作してください。');
      return;
    }
    const prior = queue.getLastRow() >= 2 ? queue.getRange(2,1,queue.getLastRow()-1,6).getValues() : [];
    // Never replace/resend a captured request whose delivery may be ambiguous.
    if (prior.some(r => ['dispatching','accepted'].includes(String(r[1])) &&
        (() => {try {return JSON.parse(r[2])[14] === snapshot[14];} catch (_) {return true;}})())) return;
    const dispatchEnabled = pdfGroupingDispatchEnabled_();
    if (dispatchEnabled && !PropertiesService.getUserProperties().getProperty('CATEGORY_GITHUB_TOKEN')) {
      sheet.getRange(rowNumber,10).setValue('既存のカテゴリ操作連携設定を確認してください。');
      return;
    }
    const id = Utilities.getUuid();
    if (queue.getLastRow() >= 1001) throw new Error('受付履歴が上限です。処理済み履歴を管理者に確認してください。');
    queue.appendRow([id,dispatchEnabled ? 'dispatching' : 'accepted',JSON.stringify(snapshot),new Date().toISOString(),'','']);
    sheet.getRange(rowNumber,10).setValue('受付中・Drive保存待ち');
    SpreadsheetApp.flush();
    if (!dispatchEnabled) return; // Manual operator processes the captured UUID.
    let response;
    try {
      response = categoryFetch_('/actions/workflows/' + PDF_WORKFLOW + '/dispatches','post',
        {ref:'main',inputs:{mode:'review',request_id:id}});
    } catch (_) {
      sheet.getRange(rowNumber,10).setValue('受付確認中・繰り返さず実行状況を確認してください。');
      return;
    }
    if (response.getResponseCode() !== 204) {
      queue.getRange(queue.getLastRow(),2).setValue('error');
      sheet.getRange(rowNumber,10).setValue('受付エラー・連携設定を確認してください。');
    }
    // Neither HTTP 204 nor a cell value means grouping is confirmed.
  } finally { lock.releaseLock(); }
}

function refreshPdfGrouping() {
  if (!pdfGroupingDispatchEnabled_()) {
    throw new Error('候補の更新は管理者に依頼してください。確認操作は受付できます。');
  }
  const response = categoryFetch_('/actions/workflows/' + PDF_WORKFLOW + '/dispatches','post',
    {ref:'main',inputs:{mode:'refresh'}});
  if (response.getResponseCode() !== 204) throw new Error('PDF確認画面の更新受付を確認できません。');
}
