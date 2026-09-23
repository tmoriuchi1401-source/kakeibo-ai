/**
 * Category-only submission. Never sends sheet values or IDs to GitHub.
 * Set CATEGORY_GITHUB_TOKEN in User Properties through configureCategorySubmit.
 * Use a repository-scoped fine-grained PAT (only Actions: write + metadata).
 */
const CATEGORY_UI = 'カテゴリ操作';
const CATEGORY_QUEUE = '_カテゴリ実行受付';
const CATEGORY_REPO = 'tmoriuchi1401-source/kakeibo-ai';
const CATEGORY_WORKFLOW = 'category-sheet-request.yml';
const CATEGORY_BUSY = ['dispatching', 'accepted', 'running'];

function onOpen() {
  SpreadsheetApp.getUi().createMenu('カテゴリ操作')
    .addItem('入力内容を処理する', 'submitCategoryInput')
    .addItem('実行状況を確認', 'checkCategoryStatus')
    .addItem('初回設定', 'configureCategorySubmit').addToUi();
}

function configureCategorySubmit() {
  // Standalone installation: stage these two properties in the editor. Move
  // the credential into the installing user's private properties immediately.
  const staged = PropertiesService.getScriptProperties();
  if (staged.getProperty('CATEGORY_GITHUB_TOKEN')) {
    const token = staged.getProperty('CATEGORY_GITHUB_TOKEN').trim();
    const sid = staged.getProperty('CATEGORY_SPREADSHEET_ID');
    if (!token.startsWith('github_pat_') || !sid) throw new Error('接続設定を確認してください。');
    const response = categoryFetch_('/actions/workflows/' + CATEGORY_WORKFLOW, 'get', null, token);
    if (response.getResponseCode() !== 200) throw new Error('GitHubの対象と権限を確認してください。');
    PropertiesService.getUserProperties().setProperties({CATEGORY_GITHUB_TOKEN:token,CATEGORY_SPREADSHEET_ID:sid});
    staged.deleteProperty('CATEGORY_GITHUB_TOKEN');
    installCategorySubmit();
    return;
  }
  const ui = SpreadsheetApp.getUi();
  const answer = ui.prompt('カテゴリ操作の初回設定',
    'このリポジトリだけに Actions の書き込み権限を持つ GitHub トークンを入力してください。' +
    'トークンはあなた専用の設定に保存され、シートやログには表示されません。', ui.ButtonSet.OK_CANCEL);
  if (answer.getSelectedButton() !== ui.Button.OK) return;
  const token = answer.getResponseText().trim();
  if (!token.startsWith('github_pat_')) throw new Error('リポジトリ限定の fine-grained token を指定してください。');
  const response = categoryFetch_('/actions/workflows/' + CATEGORY_WORKFLOW, 'get', null, token);
  if (response.getResponseCode() !== 200) throw new Error('GitHubの接続を確認できません。対象と権限を確認してください。');
  const props = PropertiesService.getUserProperties();
  props.setProperty('CATEGORY_GITHUB_TOKEN', token);
  props.setProperty('CATEGORY_SPREADSHEET_ID', SpreadsheetApp.getActive().getId());
  installCategorySubmit();
}

function installCategorySubmit() {
  const props = PropertiesService.getUserProperties();
  if (!props.getProperty('CATEGORY_GITHUB_TOKEN')) throw new Error('先に初回設定を行ってください。');
  const ss = categorySpreadsheet_();
  const sheet = ss.getSheetByName(CATEGORY_UI);
  if (!sheet) throw new Error('カテゴリ操作シートが見つかりません。');
  let queue = ss.getSheetByName(CATEGORY_QUEUE);
  if (queue && CATEGORY_BUSY.includes(String(queue.getRange('B2').getValue()))) {
    throw new Error('処理の完了後に設定してください。');
  }
  if (!queue) {
    if (sheet.getRange('A1').getValue() !== '■ 1. カテゴリを選ぶ・今後の自動分類') {
      throw new Error('シート構成を確認してください。入力は変更していません。');
    }
    // These five rows are permanently excluded from the Python UI writer.
    sheet.insertRowsBefore(1, 5);
    queue = ss.insertSheet(CATEGORY_QUEUE);
    queue.getRange('A1:J1').setValues([['request_id','state','submitted_at','started_at',
      'finished_at','result','run_id','snapshot_rows','snapshot_sha256','version']]);
    queue.hideSheet();
  }
  sheet.getRange('A1').setValue('入力内容を処理する');
  sheet.getRange('B1').insertCheckboxes().setValue(false);
  sheet.getRange('C1:F1').merge().setValue('入力待ち');
  sheet.getRange('A2').setValue('処理結果');
  sheet.getRange('B2:F2').merge().setValue('入力後、上のチェックを入れると受付します。');
  sheet.getRange('A3').setValue('最終更新');
  sheet.getRange('B3:F3').merge().setValue('');
  sheet.getRange('A4:F4').merge().setValue('受付済みなら閉じても処理は続きます。過去分はプレビュー確認後、反映チェックを入れて再実行。');
  sheet.getRange('A1:F4').setWrap(true).setVerticalAlignment('middle').setFontSize(10)
    .setFontColor('#1f2933').setFontWeight('normal');
  sheet.getRange('A1:F1').setBackground('#dceee8').setFontWeight('bold');
  sheet.getRange('B1').setBackground('#b9ddca').setHorizontalAlignment('center');
  sheet.getRange('A2:F4').setBackground('#f3f6f8');
  sheet.setRowHeights(1,3,34); sheet.setRowHeight(4,44); sheet.setRowHeight(5,10);
  sheet.setFrozenRows(4);
  const names = ['categorySubmitEdited', 'categoryCheckActive'];
  ScriptApp.getProjectTriggers().forEach(t => {
    if (names.includes(t.getHandlerFunction())) ScriptApp.deleteTrigger(t);
  });
  ScriptApp.newTrigger('categorySubmitEdited').forSpreadsheet(ss).onEdit().create();
  ScriptApp.newTrigger('categoryCheckActive').timeBased().everyMinutes(5).create();
  props.setProperty('CATEGORY_SPREADSHEET_ID', ss.getId());
  SpreadsheetApp.flush();
}

function categorySubmitEdited(e) {
  if (!e || !e.range || e.range.getSheet().getName() !== CATEGORY_UI ||
      e.range.getA1Notation() !== 'B1' || e.value !== 'TRUE') return;
  submitCategoryInput();
}

function submitCategoryInput() {
  const lock = LockService.getScriptLock();
  if (!lock.tryLock(1000)) return;
  try {
    const ss = categorySpreadsheet_();
    const sheet = ss.getSheetByName(CATEGORY_UI);
    const queue = ss.getSheetByName(CATEGORY_QUEUE);
    if (!queue) throw new Error('初回設定が必要です。');
    const prior = queue.getRange('A2:J2').getValues()[0];
    if (CATEGORY_BUSY.includes(String(prior[1]))) {
      sheet.getRange('B1').setValue(true);
      return; // Never change the existing captured input or dispatch twice.
    }
    if (!PropertiesService.getUserProperties().getProperty('CATEGORY_GITHUB_TOKEN')) {
      sheet.getRange('B1').setValue(false);
      sheet.getRange('C1').setValue('初回設定が必要です');
      return;
    }
    let rows = sheet.getRange(6,1,sheet.getLastRow()-5,12).getDisplayValues();
    while (rows.length && rows[rows.length-1].every(v => v === '')) rows.pop();
    if (!rows.length || rows.length > 10000) throw new Error('受付できる行数を超えています。');
    const markers = ['■ 1. カテゴリを選ぶ・今後の自動分類','■ 2. 過去分の固定プレビュー','■ 3. 固定プレビューを確認して反映'];
    if (markers.some(m => rows.filter(r => r[0] === m).length !== 1)) throw new Error('シートの見出しを確認してください。');
    const requestId = Utilities.getUuid();
    const submitted = new Date().toISOString();
    const digest = Utilities.computeDigest(Utilities.DigestAlgorithm.SHA_256, JSON.stringify(rows), Utilities.Charset.UTF_8)
      .map(b => ('0' + ((b + 256) % 256).toString(16)).slice(-2)).join('');
    if (queue.getMaxRows() < rows.length+3) queue.insertRowsAfter(queue.getMaxRows(), rows.length+3-queue.getMaxRows());
    // JSON begins with '[' so source text cannot become a spreadsheet formula.
    queue.getRange(4,1,rows.length,1).setValues(rows.map(r => [JSON.stringify(r)]));
    queue.getRange('A2:J2').setValues([[requestId,'dispatching',submitted,'','','受付処理中','',rows.length,digest,'1']]);
    sheet.getRange('B1').setValue(true);
    sheet.getRange('C1').setValue('受付中');
    sheet.getRange('B2').setValue('入力を保存しました。実行を依頼しています。');
    sheet.getRange('B3').setValue(submitted);
    SpreadsheetApp.flush();
    let response;
    try {
      response = categoryFetch_('/actions/workflows/' + CATEGORY_WORKFLOW + '/dispatches', 'post',
        {ref:'main', inputs:{request_id:requestId}});
    } catch (error) {
      // Delivery can be ambiguous. Keep the ID and snapshot; never auto-resend.
      sheet.getRange('C1').setValue('受付確認中');
      sheet.getRange('B2').setValue('通信結果を確認中です。チェックを繰り返さずお待ちください。');
      return;
    }
    if (response.getResponseCode() !== 204) {
      queue.getRange('B2').setValue('dispatch_failed');
      sheet.getRange('B1').setValue(false);
      sheet.getRange('C1').setValue('受付エラー');
      sheet.getRange('B2').setValue('GitHub連携を確認してください。入力内容は残っています。');
      return;
    }
    // A fast worker may already have claimed the request. Do not regress it.
    if (queue.getRange('B2').getValue() === 'dispatching') {
      sheet.getRange('C1').setValue('受付済み・開始待ち');
      sheet.getRange('B2').setValue('受付済みです。シートを閉じても処理は続きます。');
    }
  } finally { lock.releaseLock(); }
}

function categorySpreadsheet_() {
  const ss = SpreadsheetApp.getActive();
  if (ss) return ss;
  return SpreadsheetApp.openById(PropertiesService.getUserProperties().getProperty('CATEGORY_SPREADSHEET_ID'));
}

function categoryFetch_(path, method, payload, token) {
  return UrlFetchApp.fetch('https://api.github.com/repos/' + CATEGORY_REPO + path, {
    method:method, contentType:'application/json', muteHttpExceptions:true,
    headers:{Authorization:'Bearer ' + (token || PropertiesService.getUserProperties().getProperty('CATEGORY_GITHUB_TOKEN')),
      Accept:'application/vnd.github+json','X-GitHub-Api-Version':'2022-11-28'},
    ...(payload ? {payload:JSON.stringify(payload)} : {})
  });
}

function checkCategoryStatus() { categoryCheckActive(); }

function categoryCheckActive() {
  const lock = LockService.getScriptLock();
  if (!lock.tryLock(1000)) return;
  try {
    const ss = categorySpreadsheet_();
    const queue = ss.getSheetByName(CATEGORY_QUEUE);
    if (!queue) return;
    const row = queue.getRange('A2:J2').getValues()[0];
    if (!CATEGORY_BUSY.includes(String(row[1]))) return;
    const sheet = ss.getSheetByName(CATEGORY_UI);
    if (!row[6]) {
      const list = categoryFetch_('/actions/workflows/' + CATEGORY_WORKFLOW + '/runs?event=workflow_dispatch&per_page=100', 'get');
      if (list.getResponseCode() === 200) {
        const found = JSON.parse(list.getContentText()).workflow_runs.find(r =>
          r.display_title === 'Category request ' + row[0]);
        if (found) row[6] = String(found.id);
      }
    }
    if (row[6]) {
      const response = categoryFetch_('/actions/runs/' + encodeURIComponent(row[6]), 'get');
      if (response.getResponseCode() !== 200) return;
      const run = JSON.parse(response.getContentText());
      // The worker normally writes its own terminal state. Handle cancellation
      // or runner termination only after GitHub authoritatively says completed.
      if (run.status !== 'completed') return;
      const current = queue.getRange('A2:J2').getValues()[0];
      if (current[0] !== row[0] || !CATEGORY_BUSY.includes(String(current[1]))) return;
      queue.getRange('B2').setValue('error');
      sheet.getRange('B1').setValue(false);
      sheet.getRange('C1').setValue('中断・要確認');
      sheet.getRange('B2').setValue('処理が中断しました。各行の状態を確認して再実行してください。');
    } else if (Date.now() - Date.parse(row[2]) > 15*60*1000) {
      // A busy production queue is not a failed dispatch. Never unlock/resubmit
      // based only on elapsed time; a delayed old run could still start.
      sheet.getRange('C1').setValue('開始待ち・確認が必要');
      sheet.getRange('B2').setValue('開始まで時間がかかっています。入力は保存済みです。実行状況の確認を依頼してください。');
    }
  } finally { lock.releaseLock(); }
}
