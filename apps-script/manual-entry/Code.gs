/** Private owner-only web app. Configure MANUAL_SPREADSHEET_ID and MANUAL_GITHUB_TOKEN
 * in Script Properties, then run installManualEntry once before deployment. */
const MANUAL_SHEET = '_手入力受付';
const MANUAL_REPO = 'tmoriuchi1401-source/kakeibo-ai';
const MANUAL_WORKFLOW = 'manual-entry.yml';
const MANUAL_HEADER = ['request_id','state','payload_json','submitted_at','expense_id','message'];

function manualSpreadsheet_() {
  const id = PropertiesService.getScriptProperties().getProperty('MANUAL_SPREADSHEET_ID');
  if (!id) throw new Error('初期設定が必要です');
  return SpreadsheetApp.openById(id);
}
function manualToken_() {
  const token = PropertiesService.getScriptProperties().getProperty('MANUAL_GITHUB_TOKEN');
  if (!token) throw new Error('GitHubの接続設定が必要です');
  return token;
}
function installManualEntry() {
  const ss = manualSpreadsheet_();
  if (!ss.getSheetByName('カテゴリ') || !ss.getSheetByName('支出明細')) throw new Error('家計簿の接続先を確認してください');
  const response = UrlFetchApp.fetch('https://api.github.com/repos/' + MANUAL_REPO + '/actions/workflows/' + MANUAL_WORKFLOW,
    {headers:{Authorization:'Bearer ' + manualToken_(), Accept:'application/vnd.github+json'},muteHttpExceptions:true});
  if (response.getResponseCode() !== 200) throw new Error('GitHubの権限を確認してください');
  let sheet = ss.getSheetByName(MANUAL_SHEET);
  if (!sheet) sheet = ss.insertSheet(MANUAL_SHEET);
  if (sheet.getLastRow() === 0) sheet.getRange(1,1,1,6).setValues([MANUAL_HEADER]);
  if (JSON.stringify(sheet.getRange(1,1,1,6).getValues()[0]) !== JSON.stringify(MANUAL_HEADER)) throw new Error('受付シートの見出しが異なります');
  sheet.hideSheet();
}
function doGet() {
  // Deployment must be "execute as me" and "only myself". Never make it public.
  manualSpreadsheet_();
  return HtmlService.createHtmlOutputFromFile('Index').setTitle('家計簿 手入力');
}
function manualBootstrap() {
  const rows = manualSpreadsheet_().getSheetByName('カテゴリ').getRange('A2:B').getDisplayValues();
  const seen = new Set();
  const categories = rows.filter(row => row[0] && row[1] && !seen.has(JSON.stringify(row)) && seen.add(JSON.stringify(row)));
  return {today:Utilities.formatDate(new Date(),'Asia/Tokyo','yyyy-MM-dd'),categories:categories,
    id:Utilities.getUuid(),cancelId:Utilities.getUuid()};
}
function manualRow_(sheet,id) {
  if (sheet.getLastRow() < 2) return null;
  const found = sheet.getRange(2,1,sheet.getLastRow()-1,1)
    .createTextFinder(id).matchEntireCell(true).findNext();
  return found ? {number:found.getRow(),values:sheet.getRange(found.getRow(),1,1,6).getValues()[0]} : null;
}
function manualDispatch_(id) {
  try {
    const response = UrlFetchApp.fetch('https://api.github.com/repos/' + MANUAL_REPO + '/actions/workflows/' + MANUAL_WORKFLOW + '/dispatches',
      {method:'post',contentType:'application/json',muteHttpExceptions:true,
        headers:{Authorization:'Bearer ' + manualToken_(),Accept:'application/vnd.github+json'},
        payload:JSON.stringify({ref:'main',inputs:{request_id:id}})});
    return response.getResponseCode() === 204;
  } catch (_) { return null; } // Delivery is uncertain. Never automatically resend.
}
function manualValidate_(input,ss) {
  if (!input || !/^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/i.test(String(input.id))) throw new Error('受付IDが不正です');
  const amount = Number(input.amount);
  if (!Number.isSafeInteger(amount) || amount < 1 || amount > 99999999) throw new Error('金額を確認してください');
  const date = String(input.date || '');
  if (!/^\d{4}-\d\d-\d\d$/.test(date) || isNaN(Date.parse(date + 'T00:00:00Z')) || new Date(date+'T00:00:00Z').toISOString().slice(0,10) !== date) throw new Error('日付を確認してください');
  const merchant=String(input.merchant||'').trim(), note=String(input.note||'').trim();
  if (merchant.length>100 || note.length>300) throw new Error('入力が長すぎます');
  const major=String(input.major||''),minor=String(input.minor||'');
  if (major || minor) {
    const pairs=ss.getSheetByName('カテゴリ').getRange('A2:B').getDisplayValues();
    if (!pairs.some(pair=>pair[0]===major && pair[1]===minor)) throw new Error('カテゴリを選び直してください');
  }
  return {date:date,amount:amount,merchant:merchant,major:major,minor:minor,note:note,payment:'現金'};
}
function manualSubmit(input) {
  const lock=LockService.getScriptLock();lock.waitLock(30000);
  try {
    const ss=manualSpreadsheet_(), sheet=ss.getSheetByName(MANUAL_SHEET);
    if (!sheet) throw new Error('初期設定が必要です');
    const id=String(input && input.id || '');
    const prior=manualRow_(sheet,id);
    if (prior) return {id:id,state:String(prior.values[1])}; // Same click/retry is idempotent.
    const payload=manualValidate_(input,ss);
    sheet.appendRow([id,'dispatching',JSON.stringify(payload),new Date().toISOString(),'','']);
    SpreadsheetApp.flush();
    const sent=manualDispatch_(id);
    if (sent===false) sheet.getRange(sheet.getLastRow(),2).setValue('dispatch_failed');
    return {id:id,state:sent===true?'dispatching':sent===false?'dispatch_failed':'dispatching'};
  } finally {lock.releaseLock();}
}
function manualCancel(targetId,cancelId) {
  const lock=LockService.getScriptLock();lock.waitLock(30000);
  try {
    const sheet=manualSpreadsheet_().getSheetByName(MANUAL_SHEET);
    if (!sheet) throw new Error('初期設定が必要です');
    if (!/^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/i.test(String(cancelId)) || cancelId===targetId) throw new Error('取消IDが不正です');
    const prior=manualRow_(sheet,String(cancelId));
    if (prior) return {id:cancelId,state:String(prior.values[1])};
    const target=manualRow_(sheet,String(targetId));
    if (!target || JSON.parse(target.values[2]).target) throw new Error('取消対象を確認してください');
    if (target.values[1]==='cancelled') return {id:cancelId,state:'cancelled'};
    sheet.appendRow([cancelId,'dispatching',JSON.stringify({target:targetId}),new Date().toISOString(),'','']);
    SpreadsheetApp.flush();
    const sent=manualDispatch_(cancelId);
    if (sent===false) sheet.getRange(sheet.getLastRow(),2).setValue('dispatch_failed');
    return {id:cancelId,state:sent===false?'dispatch_failed':'dispatching'};
  } finally {lock.releaseLock();}
}
function manualStatus(id) {
  const sheet=manualSpreadsheet_().getSheetByName(MANUAL_SHEET);
  const row=manualRow_(sheet,String(id));
  return row?{state:String(row.values[1]),message:String(row.values[5]||'')}:{state:'unknown'};
}
