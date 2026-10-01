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
  if (!e || !e.range || e.range.getSheet().getName() !== PDF_UI ||
      e.range.getNumRows() !== 1 || e.range.getNumColumns() !== 1 ||
      e.range.getColumn() !== 8 || e.range.getRow() < 2 || !e.value) return;
  submitPdfGroupingRow_(e.range.getRow());
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
