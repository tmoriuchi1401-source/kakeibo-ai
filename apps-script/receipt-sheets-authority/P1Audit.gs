/** Read/hash/log only. Profile is generated offline for the independent trial. */
function receiptConnectionAuditStateDisabled() { return receiptConnectionAuditState(false); }
function receiptConnectionAuditStateEnabled() { return receiptConnectionAuditState(true); }

function receiptConnectionAuditState(expectedEnabled) {
  const r={OWNER:'FAIL',PROJECT:'FAIL',EXPECTED_STATE:expectedEnabled===true?'ENABLED':'DISABLED',
    MODE:'FAIL',IDENTITY:'FAIL',STATE:'UNKNOWN',STATE_MATCH:'FAIL',BINDING:'FAIL',
    PAT_PRESENT:'FAIL',SHEETS_LATCH:'UNKNOWN',VALIDATION_LATCH:'UNKNOWN',READ:'FAIL',AUDIT:'FAIL'};
  try {
    if(typeof expectedEnabled!=='boolean') throw Error('expected_state_required');
    const props=PropertiesService.getUserProperties();
    const c=JSON.parse(props.getProperty('RECEIPT_VALIDATION_CONFIG_JSON') || 'null');
    r.READ='PASS';
    r.OWNER=c && Session.getEffectiveUser().getEmail()===c.owner_email?'PASS':'FAIL';
    r.PROJECT=ScriptApp.getScriptId()===RECEIPT_P1_PROFILE.script_id?'PASS':'FAIL';
    r.MODE=c && c.mode==='sheets-authority-v1'?'PASS':'FAIL';
    r.PAT_PRESENT=Boolean(props.getProperty('RECEIPT_GITHUB_TOKEN'))?'PASS':'FAIL';
    r.SHEETS_LATCH=props.getProperty('RECEIPT_SHEETS_STOPPED')===null?'PASS':'STOPPED';
    r.VALIDATION_LATCH=props.getProperty('RECEIPT_VALIDATION_STOPPED')===null?'PASS':'STOPPED';
    if(c && typeof c.enabled==='boolean') {
      r.STATE=c.enabled?'ENABLED':'DISABLED';
      r.STATE_MATCH=c.enabled===expectedEnabled?'PASS':'FAIL';
    }
    receiptSheetsConfig_(props,true); // Same pinned identity/context checks, no IO.
    r.IDENTITY='PASS';
    const expected=expectedEnabled?RECEIPT_P1_PROFILE.enabled_sha256:RECEIPT_P1_PROFILE.disabled_sha256;
    r.BINDING=receiptSheetsHash_(receiptCanonical_(c))===expected?'PASS':'FAIL';
    if(['OWNER','PROJECT','MODE','IDENTITY','STATE_MATCH','BINDING','PAT_PRESENT',
        'SHEETS_LATCH','VALIDATION_LATCH','READ'].every(k=>r[k]==='PASS')) r.AUDIT='PASS';
  } catch (_) {} // Closed results only, never private exceptions/config/token.
  for(const key of Object.keys(r)) Logger.log(key+':'+r[key]);
  return r;
}
