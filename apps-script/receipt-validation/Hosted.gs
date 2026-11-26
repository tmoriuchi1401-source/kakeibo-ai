/** Existing owner-only Execution API + ScriptLock; no dispatcher or new auth.
 * API executable access MUST remain MYSELF. EffectiveUser is the authenticated
 * OAuth caller here (the edit trigger separately verifies event.user).
 * No GitHub credential, public run identifier, permit issuer or financial API.
 */
function receiptHostedResult_(r) {
  return r.state==='記帳済み' ? Number.isSafeInteger(r.financial_write_count) && r.financial_write_count>0 &&
    r.exact_readback===true && /^[a-f0-9]{64}$/.test(r.plan_digest) && r.issues.length===0 : r.financial_write_count===0;
}
function receiptHostedRetryable_(row) {
  if(!['修正が必要','保留'].includes(row[7]))return false;
  const r=JSON.parse(row[8]);
  return r.posting_authority===false && r.financial_write_count===0 && r.issues.length>0 &&
    !r.issues.includes('existing_receipt_duplicate') && !r.exact_readback;
}
/** Explicit owner setup only, after separate connection approval. No new book,
 * credential, trigger or financial permission. Historical Start stays denied.
 */
function receiptHostedStart() {
  return receiptSheetsLocked_(()=>{
    const c=receiptReviewConfig_(),props=PropertiesService.getScriptProperties();
    if(c.enabled || props.getProperty(REVIEW_STOP))throw Error('review_start_not_ready');
    receiptReviewRegisterAllowed_();
    const triggers=ScriptApp.getProjectTriggers();
    if(triggers.length!==1 || triggers[0].getHandlerFunction()!=='receiptReviewEdited' ||
       triggers[0].getTriggerSourceId()!==c.spreadsheet_id || triggers[0].getEventType()!==ScriptApp.EventType.ON_EDIT)
      throw Error('review_existing_trigger_required');
    const ports=receiptReviewPorts_(c),catalog=receiptReviewCatalog_(ports);
    receiptReviewRequests_(ports,catalog.entries);
    c.enabled=true;props.setProperty('RECEIPT_REVIEW_CONFIG_JSON',JSON.stringify(c));
    if(receiptCanonical_(receiptReviewConfig_())!==receiptCanonical_(c))throw Error('hosted_start_unknown');
    return {state:'受付ON・金融write OFF'};
  });
}
function receiptHostedBackend(command) {
  return receiptSheetsLocked_(()=>{
    const c=receiptReviewConfig_(); // Effective owner + exact Script/book binding.
    if(!command || !['status','lookup','pending','read','intake','claim','finish','rearm'].includes(command.action))
      throw Error('hosted_command_forbidden');
    const allowed={status:['action'],lookup:['action','source_id','sha256'],pending:['action'],
      read:['action','request_id'],intake:['action','entry','row'],claim:['action','request_id','before'],
      finish:['action','request_id','before','result'],rearm:['action','request_id','before','entry']};
    if(Object.keys(command).sort().join(',')!==allowed[command.action].sort().join(','))throw Error('hosted_command_shape');
    const acceptance=()=>{
      if(receiptCanonical_(receiptReviewConfig_())!==receiptCanonical_(c))throw Error('hosted_binding_changed');
      receiptReviewEnabled_(c);receiptReviewRegisterAllowed_();
    };
    // STOP/authentication failure denies even a pending-list response. There
    // is no fallback to the historical private GitHub-variable PAT gate.
    acceptance();
    const ports=receiptReviewPorts_(c);
    ports.beforeChange=acceptance;
    if(command.action==='status') {
      const status=receiptReviewCommand_(command,ports,c,{check:acceptance,acceptance});
      return {...status,binding:{script_id:c.script_id,owner_email:c.owner_email,
        spreadsheet_id:c.spreadsheet_id,folder_id:c.folder_id}};
    }
    if(command.action==='rearm')return receiptHostedRearm_(command,ports,c,acceptance);
    return receiptReviewCommand_(command,ports,c,{check:acceptance,acceptance});
  });
}
function receiptHostedRearm_(command,ports,c,acceptance) {
  const catalog=receiptReviewCatalog_(ports),table=receiptReviewRequests_(ports,catalog.entries);
  const old=catalog.entries.find(e=>e.request_id===command.request_id),e=receiptReviewCase_(command.entry);
  const finished=table.filter(r=>r[0]===command.request_id);
  if(!old || finished.length!==1 || !receiptHostedRetryable_(finished[0]) ||
     receiptCanonical_(finished[0])!==receiptCanonical_(command.before) || e.request_id===old.request_id ||
     catalog.entries.slice().reverse().find(x=>x.case_id===old.case_id)!==old ||
     catalog.entries.some(x=>x.request_id===e.request_id) ||
     receiptCanonical_({...old,request_id:e.request_id})!==receiptCanonical_(e))throw Error('hosted_rearm_binding');
  const ui=ports.readUI(),matches=ui.map((r,i)=>({r,i})).filter(x=>x.r[0]===e.case_id);
  if(matches.length!==1 || receiptCanonical_(matches[0].r.slice(81,96))!==receiptCanonical_(receiptReviewMachine_(old)) ||
     matches[0].r[8]!=='register:'+old.request_id || catalog.rows.length>=1000)throw Error('hosted_rearm_ui_changed');
  receiptReviewSourceAtRegistration_(e.source,c);acceptance();
  const text=receiptCanonical_(e),stored=[e.case_id,e.source.source_id,e.source.version,e.source.sha256,text,receiptSheetsHash_(text),e.request_id];
  const number=matches[0].i+2,previous=matches[0].r;
  receiptReviewChange_(ports,[{tab:REVIEW_CASE_TAB,row:catalog.rows.length+1,values:stored},
    {tab:'確認',row:number,column:8,values:['ready:'+e.request_id]},
    {tab:'確認',row:number,column:93,values:[e.request_id]}],()=>{
      receiptReviewReadback_(ports.readCases(),[...catalog.rows,stored],'hosted_rearm_unknown',REVIEW_CASE_TAB);
      receiptReviewReadback_(ports.readRequests(),table,'hosted_history_changed',RECEIPT_SHEETS_TAB);
      const actual=ports.readUI().filter(r=>r[0]===e.case_id);
      const expected=[...previous];expected[8]='ready:'+e.request_id;expected[93]=e.request_id;
      // Formula outputs E:G may recalculate. Every draft/note/machine cell is
      // independently bound; no row regeneration or clearing owner inputs.
      const evidence=r=>[...r.slice(0,4),...r.slice(7)];
      if(actual.length!==1 || receiptCanonical_(evidence(actual[0]))!==receiptCanonical_(evidence(expected)))
        throw Error('hosted_rearm_draft_changed');
    });
  // Refresh only the existing register checkbox validation, preserving drafts.
  const ss=SpreadsheetApp.openById(c.spreadsheet_id),sheet=ss.getSheetByName('確認');
  PropertiesService.getScriptProperties().setProperty(REVIEW_STOP,'rearm_decoration_unconfirmed');
  sheet.getRange(number,9).setDataValidation(SpreadsheetApp.newDataValidation()
    .requireCheckbox('register:'+e.request_id,'ready:'+e.request_id).setAllowInvalid(false).build());
  PropertiesService.getScriptProperties().deleteProperty(REVIEW_STOP);
  return {request_id:e.request_id,previous_request_id:old.request_id};
}
