/** Owner backend. Legacy UI remains owner-only; authenticated household requests
 * arrive through doPost. See docs/manual-entry.md before deployment. */
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
  manualOwner_();
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
  manualOwner_();
  manualSpreadsheet_();
  return HtmlService.createHtmlOutputFromFile('Index').setTitle('家計簿 手入力');
}
function manualBootstrap() {
  manualOwner_();
  return manualBootstrap_();
}
function manualBootstrap_() {
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
  return manualSubmit_(input, manualOwner_());
}
function manualSubmit_(input, actor) {
  const lock=LockService.getScriptLock();lock.waitLock(30000);
  try {
    const ss=manualSpreadsheet_(), sheet=ss.getSheetByName(MANUAL_SHEET);
    if (!sheet) throw new Error('初期設定が必要です');
    const id=String(input && input.id || '');
    const prior=manualRow_(sheet,id);
    if (prior) return {id:id,state:String(prior.values[1])}; // Same click/retry is idempotent.
    const payload=manualValidate_(input,ss);
    const createdAt=new Date().toISOString();
    payload.entered_by=actor;
    payload.created_at=createdAt;
    payload.manual_entry_id=id;
    sheet.appendRow([id,'dispatching',JSON.stringify(payload),createdAt,'','']);
    SpreadsheetApp.flush();
    const sent=manualDispatch_(id);
    if (sent===false) sheet.getRange(sheet.getLastRow(),2).setValue('dispatch_failed');
    return {id:id,state:sent===true?'dispatching':sent===false?'dispatch_failed':'dispatching'};
  } finally {lock.releaseLock();}
}
function manualCancel(targetId,cancelId) {
  return manualCancel_(targetId,cancelId,manualOwner_());
}
function manualCancel_(targetId,cancelId,actor) {
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
    const createdAt=new Date().toISOString();
    sheet.appendRow([cancelId,'dispatching',JSON.stringify({target:targetId,entered_by:actor,
      created_at:createdAt,manual_entry_id:cancelId}),createdAt,'','']);
    SpreadsheetApp.flush();
    const sent=manualDispatch_(cancelId);
    if (sent===false) sheet.getRange(sheet.getLastRow(),2).setValue('dispatch_failed');
    return {id:cancelId,state:sent===false?'dispatch_failed':'dispatching'};
  } finally {lock.releaseLock();}
}
function manualStatus(id) {
  manualOwner_();
  return manualStatus_(id);
}
function manualStatus_(id) {
  const sheet=manualSpreadsheet_().getSheetByName(MANUAL_SHEET);
  const row=manualRow_(sheet,String(id));
  return row?{state:String(row.values[1]),message:String(row.values[5]||'')}:{state:'unknown'};
}

function manualOwner_() {
  const owner='tmoriuchi1401@gmail.com';
  if (Session.getActiveUser().getEmail().toLowerCase()!==owner ||
      Session.getEffectiveUser().getEmail().toLowerCase()!==owner) {
    throw new Error('この操作は管理者のみ利用できます');
  }
  return owner;
}

function manualHouseholdActor_(token) {
  const properties=PropertiesService.getScriptProperties();
  const audience=properties.getProperty('MANUAL_IDENTITY_AUDIENCE');
  const allowed=JSON.parse(properties.getProperty('MANUAL_HOUSEHOLD_EMAILS') || '[]');
  if (!audience || !Array.isArray(allowed) || allowed.length!==2 ||
      typeof token!=='string' || token.length<20 || token.length>8192) throw new Error('本人確認が必要です');
  let claims;
  try {
    const response=UrlFetchApp.fetch('https://oauth2.googleapis.com/tokeninfo?id_token='+encodeURIComponent(token),
      {muteHttpExceptions:true,followRedirects:false});
    if (response.getResponseCode()!==200) throw new Error();
    claims=JSON.parse(response.getContentText());
  } catch (_) { throw new Error('本人確認できませんでした'); }
  const now=Math.floor(Date.now()/1000);
  if (claims.aud!==audience || !['accounts.google.com','https://accounts.google.com'].includes(claims.iss) ||
      typeof claims.sub!=='string' || !/^\d{1,255}$/.test(claims.sub) ||
      !Number.isFinite(Number(claims.exp)) || Number(claims.exp)<=now ||
      !Number.isFinite(Number(claims.iat)) || Number(claims.iat)>now+60 ||
      ![true,'true'].includes(claims.email_verified) || typeof claims.email!=='string') {
    throw new Error('本人確認できませんでした');
  }
  const email=claims.email.toLowerCase();
  if (!allowed.every(value=>typeof value==='string') || !allowed.includes(email)) throw new Error('この家計簿へのアクセス権がありません');
  return email;
}

function doPost(e) {
  let result;
  try {
    if (!e || !e.postData || e.postData.type!=='application/json' ||
        typeof e.postData.contents!=='string' || e.postData.contents.length>16384) throw new Error();
    const request=JSON.parse(e.postData.contents);
    const actor=manualHouseholdActor_(request.identityToken);
    switch(request.action) {
      case 'bootstrap': result=manualBootstrap_(); break;
      case 'submit': result=manualSubmit_(request.input,actor); break;
      case 'cancel': result=manualCancel_(request.targetId,request.cancelId,actor); break;
      case 'status': result=manualStatus_(request.id); break;
      default: throw new Error();
    }
    return ContentService.createTextOutput(JSON.stringify({ok:true,result:result})).setMimeType(ContentService.MimeType.JSON);
  } catch (_) {
    // Never serialize identity tokens, Google errors, stack traces or credentials.
    return ContentService.createTextOutput(JSON.stringify({ok:false,error:'本人確認または入力内容を確認してください'})).setMimeType(ContentService.MimeType.JSON);
  }
}
