/** Append a new validator result without changing the historical registration. */
const REVIEW_POLICY_TAB='_receipt_policy_reviews';
const REVIEW_POLICY_REVISION='receipt-review-amount-v2';
const REVIEW_POLICY_HEADERS=['request_id','snapshot_digest','validator_revision',
  'original_result_digest','result_json','result_digest','created_at'];
function receiptReviewPolicyHistory_(ports) {
  const rows=ports.readPolicy(),seen=new Set();
  if(receiptCanonical_(rows[0])!==receiptCanonical_(REVIEW_POLICY_HEADERS))throw Error('policy_header_invalid');
  for(const row of rows.slice(1)) {
    if(row.length!==7 || row.some(x=>typeof x!=='string') || seen.has(row[0]) ||
       row[2]!==REVIEW_POLICY_REVISION || receiptSheetsHash_(row[4])!==row[5])throw Error('policy_history_invalid');
    const result=JSON.parse(row[4]);
    if(result.request_id!==row[0] || result.snapshot_digest!==row[1] ||
       result.validator_revision!==row[2] || result.original_result_digest!==row[3] ||
       result.financial_write_count!==0 || result.posting_authority!==false ||
       JSON.stringify(Object.fromEntries(Object.keys(result).sort().map(k=>[k,result[k]])))!==row[4])
      throw Error('policy_history_binding_invalid');
    seen.add(row[0]);
  }
  return rows;
}
function receiptReviewPolicyCommand_(cmd,ports,c,entries,table) {
  receiptReviewEnabled_(c);receiptSheetsAcceptance_(c,PropertiesService.getScriptProperties());
  const history=receiptReviewPolicyHistory_(ports),plan=cmd.plan,result=cmd.result;
  if(!plan || !result || !/^[a-f0-9-]{36}$/.test(plan.request_id || ''))throw Error('policy_plan_invalid');
  const row=table.find(r=>r[0]===plan.request_id),entry=entries.find(e=>e.request_id===plan.request_id);
  if(!row || !entry || history.slice(1).some(r=>r[0]===plan.request_id))throw Error('policy_original_or_replay_invalid');
  const old=JSON.parse(row[8]),snapshot=JSON.parse(row[5]);
  if(Object.keys(plan).sort().join(',')!==
     'entry_digest,original_record_digest,original_result_digest,request_id,schema,snapshot_digest,validator_revision' ||
     plan.schema!=='receipt-policy-recheck-v1' || plan.validator_revision!==REVIEW_POLICY_REVISION ||
     row[7]!=='保留' || old.state!=='保留' || receiptCanonical_(old.issues)!=='["tax_treatment_unknown"]' ||
     old.financial_write_count!==0 || old.posting_authority!==false || snapshot.inputs.tax_mode!=='不明' ||
     old.request_id!==row[0] || old.snapshot_digest!==row[6] || plan.snapshot_digest!==row[6] ||
     plan.original_result_digest!==receiptSheetsHash_(receiptCanonical_(old)) ||
     plan.original_record_digest!==receiptSheetsHash_(receiptCanonical_(row)) ||
     plan.entry_digest!==receiptSheetsHash_(receiptCanonical_(entry)))throw Error('policy_plan_binding_invalid');
  const ui=ports.readUI(),matches=ui.filter(r=>r[0]===entry.case_id);
  if(matches.length!==1 || receiptCanonical_(matches[0].slice(81,96))!==receiptCanonical_(receiptReviewMachine_(entry)) ||
     matches[0][8]!=='register:'+plan.request_id)throw Error('policy_ui_binding_invalid');
  if(result.request_id!==row[0] || result.snapshot_digest!==row[6] ||
     result.original_result_digest!==plan.original_result_digest || result.validator_revision!==REVIEW_POLICY_REVISION ||
     result.financial_write_count!==0 || result.posting_authority!==false ||
     !['検証済み・未記帳','修正が必要','保留'].includes(result.state) ||
     !Array.isArray(result.issues) || !Array.isArray(result.warnings) ||
     result.warnings.length!==1 || result.warnings[0]!=='tax_treatment_unknown' ||
     result.issues.some(x=>typeof x!=='string' || !/^[a-z0-9_:.-]+$/.test(x)) ||
     result.state==='検証済み・未記帳' && (result.issues.length || result.difference!==0 ||
       result.receipt_total!==snapshot.inputs.receipt_total || result.item_total!==result.receipt_total))
    throw Error('policy_result_invalid');
  if(ports.readTaxIfPresent().slice(1).some(r=>r[1]===plan.request_id))throw Error('policy_tax_correction_conflict');
  if(history.length>=1000)throw Error('policy_history_capacity');
  const text=JSON.stringify(Object.fromEntries(Object.keys(result).sort().map(k=>[k,result[k]])));
  if(text.length>40000 || receiptCanonical_(ports.readRequests())!==receiptCanonical_(table) ||
     receiptCanonical_(ports.readUI())!==receiptCanonical_(ui))throw Error('policy_context_changed');
  const appended=[row[0],row[6],REVIEW_POLICY_REVISION,plan.original_result_digest,
    text,receiptSheetsHash_(text),new Date().toISOString()];
  receiptReviewChange_(ports,[{tab:REVIEW_POLICY_TAB,row:history.length+1,values:appended}],()=>{
    receiptReviewReadback_(receiptReviewPolicyHistory_(ports),[...history,appended],'policy_readback_unknown',REVIEW_POLICY_TAB);
    receiptReviewReadback_(ports.readRequests(),table,'policy_original_changed',RECEIPT_SHEETS_TAB);
    // G is a derived formula and can recalculate as history is appended.
    const inputs=rows=>rows.map(r=>r.filter((_,i)=>i!==6));
    receiptReviewReadback_(inputs(ports.readUI()),inputs(ui),'policy_ui_changed','確認');
  });
  return {request_id:row[0],state:result.state,financial_write_count:0};
}
/** Explicit setup while both gates are OFF. Never called by edit/GET/register. */
function receiptReviewPreparePolicyRecheck() {
  return receiptSheetsLocked_(()=>{
    const c=receiptReviewConfig_(),props=PropertiesService.getScriptProperties();
    if(c.enabled || props.getProperty(REVIEW_STOP))throw Error('policy_prepare_requires_off');
    const ss=SpreadsheetApp.openById(c.spreadsheet_id),ports=receiptReviewPorts_(c),ui=ports.readUI();
    const catalog=receiptReviewCatalog_(ports),originals=receiptReviewRequests_(ports,catalog.entries);
    props.setProperty(REVIEW_STOP,'policy_setup_unconfirmed');
    let history=ss.getSheetByName(REVIEW_POLICY_TAB);
    if(history)receiptReviewPolicyHistory_(ports);
    else {
      history=ss.insertSheet(REVIEW_POLICY_TAB);
      history.getRange(1,1,1,7).setValues([REVIEW_POLICY_HEADERS]);history.hideSheet();
      const p=history.protect();p.removeEditors(p.getEditors());if(p.canDomainEdit())p.setDomainEdit(false);
    }
    const sheet=ss.getSheetByName('確認');sheet.getRange(1,7).setValue('結果（有効な検証）');
    for(let i=0;i<ui.length;i++)if(ui[i][0]){
      const entry=catalog.entries.find(e=>e.case_id===ui[i][0]);
      if(!entry)throw Error('policy_setup_case_missing');
      if(entry.candidate_rows.length<=8)sheet.getRange(i+2,7).setFormula(receiptReviewFormulas_(
        !!ss.getSheetByName('_receipt_tax_corrections'),true)[2]);
      sheet.getRange(i+2,97).setNote('税区分は任意です。不明でも支払総額と明細が一致すれば登録できます。');
    }
    SpreadsheetApp.flush();receiptReviewPolicyHistory_(ports);
    receiptReviewReadback_(ports.readRequests(),originals,'policy_setup_original_changed',RECEIPT_SHEETS_TAB);
    receiptReviewReadback_(ports.readCases(),catalog.rows,'policy_setup_catalog_changed',REVIEW_CASE_TAB);
    const inputs=rows=>rows.map(r=>r.filter((_,i)=>i!==6));
    receiptReviewReadback_(inputs(ports.readUI()),inputs(ui),'policy_setup_input_changed','確認');
    props.deleteProperty(REVIEW_STOP);
    return {state:'判定仕様準備済み・受付OFF',financial_write_count:0};
  });
}
