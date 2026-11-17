/** Proposal only: read-only same-owner audit. Not placed or executed live. */
function receiptConnectionAuditDisabled() {
  const r = {OWNER:'FAIL',PROJECT:'FAIL',MODE:'FAIL',DISABLED:'FAIL',BINDING:'FAIL',PAT_PRESENT:'FAIL'};
  try {
    r.OWNER = Session.getEffectiveUser().getEmail() === 'tmoriuchi1401@gmail.com' ? 'PASS' : 'FAIL';
    r.PROJECT = ScriptApp.getScriptId() === '1IsGyeAI83NWVOVvJhL11zdl9BFCBWM3iaxxlP47_nhKrD-Ev4AgCD1i2' ? 'PASS' : 'FAIL';
    if (r.OWNER === 'PASS' && r.PROJECT === 'PASS') {
      const props = PropertiesService.getUserProperties();
      const raw = props.getProperty('RECEIPT_VALIDATION_CONFIG_JSON');
      let c = null;
      try { c = JSON.parse(raw || 'null'); } catch (_) {}
      r.MODE = c && c.mode === 'sheets-authority-v1' ? 'PASS' : 'FAIL';
      r.DISABLED = c && c.enabled === false ? 'PASS' : 'FAIL';
      r.BINDING = typeof raw === 'string' && receiptSheetsHash_(raw) === '61fc6c0190b51355e1cd072073cf85e1c75a75a0c3a7feb20b1350750b40573b' ? 'PASS' : 'FAIL';
      r.PAT_PRESENT = Boolean(props.getProperty('RECEIPT_GITHUB_TOKEN')) ? 'PASS' : 'FAIL';
    }
  } catch (_) {}
  for (const key of Object.keys(r)) Logger.log(key + ':' + r[key]);
  return r;
}

/** Explicit editor entry points. Neither changes properties nor starts reception. */
function receiptConnectionAuditStateDisabled() { return receiptConnectionAuditState(false); }
function receiptConnectionAuditStateEnabled() { return receiptConnectionAuditState(true); }

/** Read-only synthetic-trial gate. Call with a boolean expected enabled state. */
function receiptConnectionAuditState(expectedEnabled) {
  const r = {OWNER:'FAIL',PROJECT:'FAIL',EXPECTED_STATE:'FAIL',MODE:'FAIL',IDENTITY:'FAIL',
    STATE:'FAIL',STATE_MATCH:'FAIL',BINDING:'FAIL',PAT_PRESENT:'FAIL',
    SHEETS_LATCH:'FAIL',VALIDATION_LATCH:'FAIL',READ:'FAIL',AUDIT:'FAIL'};
  if (typeof expectedEnabled === 'boolean') r.EXPECTED_STATE = expectedEnabled ? 'ENABLED' : 'DISABLED';
  try {
    r.OWNER = Session.getEffectiveUser().getEmail() === 'tmoriuchi1401@gmail.com' ? 'PASS' : 'FAIL';
    r.PROJECT = ScriptApp.getScriptId() === '1IsGyeAI83NWVOVvJhL11zdl9BFCBWM3iaxxlP47_nhKrD-Ev4AgCD1i2' ? 'PASS' : 'FAIL';
    if (r.OWNER === 'PASS' && r.PROJECT === 'PASS' && typeof expectedEnabled === 'boolean') {
      const props = PropertiesService.getUserProperties();
      const raw = props.getProperty('RECEIPT_VALIDATION_CONFIG_JSON');
      const sheetsStopped = props.getProperty('RECEIPT_SHEETS_STOPPED') !== null;
      const validationStopped = props.getProperty('RECEIPT_VALIDATION_STOPPED') !== null;
      const patPresent = Boolean(props.getProperty('RECEIPT_GITHUB_TOKEN'));
      r.READ = 'PASS';
      r.SHEETS_LATCH = sheetsStopped ? 'STOPPED' : 'PASS';
      r.VALIDATION_LATCH = validationStopped ? 'STOPPED' : 'PASS';
      r.PAT_PRESENT = patPresent ? 'PASS' : 'FAIL';
      let c = null;
      try { c = JSON.parse(raw || 'null'); } catch (_) {}
      r.MODE = c && c.mode === 'sheets-authority-v1' ? 'PASS' : 'FAIL';
      r.IDENTITY = receiptConnectionAuditIdentity_(c) ? 'PASS' : 'FAIL';
      if (c && typeof c.enabled === 'boolean') {
        r.STATE = c.enabled ? 'ENABLED' : 'DISABLED';
        r.STATE_MATCH = c.enabled === expectedEnabled ? 'PASS' : 'FAIL';
      }
      const expectedDigest = expectedEnabled
        ? '99282497558df26a69dea7d3461b681cc20c2e0a174706b1821bc654ecf8142a'
        : '61fc6c0190b51355e1cd072073cf85e1c75a75a0c3a7feb20b1350750b40573b';
      if (r.MODE === 'PASS' && r.IDENTITY === 'PASS' && r.STATE_MATCH === 'PASS' &&
          typeof raw === 'string' && receiptSheetsHash_(raw) === expectedDigest) r.BINDING = 'PASS';
      if (sheetsStopped || validationStopped) r.AUDIT = 'STOPPED';
      else if (['MODE','IDENTITY','STATE_MATCH','BINDING','PAT_PRESENT','READ']
          .every(key => r[key] === 'PASS')) r.AUDIT = 'PASS';
    }
  } catch (_) {
    for (const key of ['MODE','IDENTITY','STATE','STATE_MATCH','BINDING','PAT_PRESENT',
        'SHEETS_LATCH','VALIDATION_LATCH','READ','AUDIT']) r[key] = 'FAIL';
  }
  for (const key of Object.keys(r)) Logger.log(key + ':' + r[key]);
  return r;
}

function receiptConnectionAuditIdentity_(c) {
  try {
    const cid = 'receipt:1qpsNLmSi3s3uhzOTJrAetwvPT8qJKo2G';
    if (!c || c.synthetic_only !== true ||
        c.spreadsheet_id !== '1pNXXr_FwhadUFpiAOZdubAvrKiU5ZnAngXkL8NhnDJM' ||
        c.folder_id !== '0AHhV1kvKGgkbUk9PVA' || c.owner_email !== 'tmoriuchi1401@gmail.com' ||
        c.service_email !== 'kakeibo-python@kakeiboai-505703.iam.gserviceaccount.com' ||
        c.repository !== 'tmoriuchi1401-source/kakeibo-ai' || c.workflow !== 'category-sheet-request.yml' ||
        c.ref !== 'review/receipt-sheets-authority-20261116' || !c.cases ||
        Object.keys(c.cases).length !== 1 || !Object.prototype.hasOwnProperty.call(c.cases,cid)) return false;
    const expected = JSON.parse(c.cases[cid].expected_json), source = expected.source;
    return expected.case_id === cid && source.source_id === '1qpsNLmSi3s3uhzOTJrAetwvPT8qJKo2G' &&
      source.sha256 === '4195eb022db11c33c917d2bcd1159463fadac1594a3671415722c5e3b08f4fa2' &&
      source.mime_type === 'image/png' && source.classification === 'normal' && source.transaction_kind === 'purchase';
  } catch (_) { return false; }
}
