/** Explicit tax-only correction. Original catalog/request rows are never written. */
const REVIEW_TAX_TAB='_receipt_tax_corrections';
const REVIEW_TAX_HEADERS=['correction_id','request_id','snapshot_digest','correction_no','event','payload_json','payload_digest','created_at'];
function receiptReviewTaxHistory_(ports) {
  const rows=ports.readTax(),seen=new Set();
  if(receiptCanonical_(rows[0])!==receiptCanonical_(REVIEW_TAX_HEADERS))throw Error('tax_history_header_invalid');
  for(const r of rows.slice(1)) {
    if(r.length!==8 || r.some(x=>typeof x!=='string') || r[0]!==r[1]+':tax:1' || r[3]!=='1' ||
       !['intent','result'].includes(r[4]) || receiptSheetsHash_(r[5])!==r[6] || seen.has(r[0]+':'+r[4]))throw Error('tax_history_invalid');
    const p=JSON.parse(r[5]);
    if(p.correction_id!==r[0] || p.request_id!==r[1] || p.snapshot_digest!==r[2])throw Error('tax_history_binding_invalid');
    const text=r[4]==='intent'?receiptCanonical_(p):JSON.stringify(Object.fromEntries(Object.keys(p).sort().map(k=>[k,p[k]])));
    if(text!==r[5])throw Error('tax_history_noncanonical');
    if(r[4]==='result' && !seen.has(r[0]+':intent'))throw Error('tax_history_order_invalid');
    seen.add(r[0]+':'+r[4]);
  }
  return rows;
}
function receiptReviewTaxPlan_(plan,e,row) {
  const keys='after_mode,amended_snapshot_digest,before_mode,correction_id,correction_no,entry_digest,evidence,original_record_digest,request_id,schema,snapshot_digest';
  const s=JSON.parse(row[5]),result=JSON.parse(row[8]);
  if(Object.keys(plan).sort().join(',')!==keys || plan.schema!=='receipt-tax-correction-v1' ||
     plan.request_id!==e.request_id || plan.correction_id!==e.request_id+':tax:1' || plan.correction_no!==1 ||
     plan.snapshot_digest!==row[6] || plan.entry_digest!==receiptSheetsHash_(receiptCanonical_(e)) ||
     plan.original_record_digest!==receiptSheetsHash_(receiptCanonical_(row)) || row[7]!=='保留' ||
     receiptCanonical_(result.issues)!=='["tax_treatment_unknown"]' || s.inputs.tax_mode!=='不明' ||
     plan.before_mode!=='不明' || !['税込','税別'].includes(plan.after_mode) || plan.evidence!=='original_print_confirmed')
    throw Error('tax_plan_binding_invalid');
  const amended=JSON.parse(row[5]);amended.inputs.tax_mode=plan.after_mode;
  amended.input_version=receiptSheetsHash_(receiptCanonical_(amended.inputs));
  if(plan.amended_snapshot_digest!==receiptSheetsHash_(receiptCanonical_(amended)))throw Error('tax_amendment_invalid');
  return amended;
}
function receiptReviewTaxCommand_(cmd,ports,c,entries,table) {
  receiptReviewEnabled_(c);
  receiptSheetsAcceptance_(c,PropertiesService.getScriptProperties());
  const history=receiptReviewTaxHistory_(ports),rid=cmd.action==='tax_begin'?cmd.plan?.request_id:cmd.result?.request_id;
  const e=entries.find(x=>x.request_id===rid),row=table.find(r=>r[0]===rid);
  if(!e || !row)throw Error('tax_original_missing');
  const events=history.slice(1).filter(r=>r[1]===rid),cid=rid+':tax:1';
  const ui=ports.readUI(),matches=ui.map((r,i)=>({r,i})).filter(x=>x.r[0]===e.case_id);
  if(matches.length!==1 || receiptCanonical_(matches[0].r.slice(81,96))!==receiptCanonical_(receiptReviewMachine_(e)) ||
     matches[0].r[2]!=='https://drive.google.com/file/d/'+e.source.source_id+'/view' || matches[0].r[8]!=='register:'+rid)
    throw Error('tax_ui_binding_invalid');
  let plan,payload,event,text,changes=[];
  if(cmd.action==='tax_begin') {
    if(events.length)throw Error('tax_correction_already_attempted');
    plan=cmd.plan;receiptReviewTaxPlan_(plan,e,row);
    if(matches[0].r[96]!==plan.before_mode)throw Error('tax_draft_changed');
    payload=plan;event='intent';text=receiptCanonical_(plan);
  } else {
    if(events.length!==1 || events[0][4]!=='intent' || cmd.correction_id!==cid)throw Error('tax_finish_replay');
    plan=JSON.parse(events[0][5]);receiptReviewTaxPlan_(plan,e,row);
    payload=cmd.result;event='result';
    if(payload.correction_id!==cid || payload.request_id!==rid || payload.snapshot_digest!==row[6] ||
       payload.correction_digest!==events[0][6] || payload.amended_snapshot_digest!==plan.amended_snapshot_digest ||
       payload.tax_mode!==plan.after_mode || payload.financial_write_count!==0 || payload.posting_authority!==false ||
       !['検証済み・未記帳','修正が必要','保留'].includes(payload.state) || !Array.isArray(payload.issues) ||
       payload.issues.some(x=>typeof x!=='string' || !/^[a-z0-9_:.-]+$/.test(x)))throw Error('tax_result_invalid');
    if(matches[0].r[96]!==plan.before_mode)throw Error('tax_draft_changed');
    text=JSON.stringify(Object.fromEntries(Object.keys(payload).sort().map(k=>[k,payload[k]])));
    changes.push({tab:'確認',row:matches[0].i+2,column:96,values:[plan.after_mode]});
  }
  if(history.length>=1000 || text.length>40000)throw Error('tax_history_capacity');
  const appended=[cid,rid,row[6],'1',event,text,receiptSheetsHash_(text),new Date().toISOString()];
  changes.push({tab:REVIEW_TAX_TAB,row:history.length+1,values:appended});
  if(receiptCanonical_(ports.readUI())!==receiptCanonical_(ui) ||
     receiptCanonical_(ports.readRequests())!==receiptCanonical_(table))throw Error('tax_context_changed');
  receiptReviewChange_(ports,changes,()=>{
    receiptReviewReadback_(ports.readTax(),[...history,appended],'tax_readback_unknown',REVIEW_TAX_TAB);
    receiptReviewReadback_(ports.readRequests(),table,'tax_original_changed',RECEIPT_SHEETS_TAB);
    const expected=JSON.parse(JSON.stringify(ui));
    if(event==='result')expected[matches[0].i][96]=plan.after_mode;
    // E/F/G are derived formulas and may recalculate on intent/result append.
    // Compare every input cell, including the checkbox, plus the CS amendment.
    const inputs=rows=>rows.map(r=>r.filter((_,i)=>![4,5,6].includes(i)));
    receiptReviewReadback_(inputs(ports.readUI()),inputs(expected),'tax_ui_readback_unknown','確認');
  });
  return {correction_id:cid,event,financial_write_count:0};
}
/** Future explicit setup while OFF. Never executed by a GET or registration. */
function receiptReviewPrepareTaxCorrections() {
  return receiptSheetsLocked_(()=>{
    const c=receiptReviewConfig_();
    if(c.enabled || PropertiesService.getScriptProperties().getProperty(REVIEW_STOP))throw Error('tax_prepare_requires_off');
    const ss=SpreadsheetApp.openById(c.spreadsheet_id),ports=receiptReviewPorts_(c),ui=ports.readUI();
    const catalog=receiptReviewCatalog_(ports),entries=catalog.entries,originals=receiptReviewRequests_(ports,entries);
    PropertiesService.getScriptProperties().setProperty(REVIEW_STOP,'tax_setup_unconfirmed');
    let history=ss.getSheetByName(REVIEW_TAX_TAB);
    if(history)receiptReviewTaxHistory_(ports);
    else {
      history=ss.insertSheet(REVIEW_TAX_TAB);history.getRange(1,1,1,8).setValues([REVIEW_TAX_HEADERS]);history.hideSheet();
      const p=history.protect();p.removeEditors(p.getEditors());if(p.canDomainEdit())p.setDomainEdit(false);
    }
    const sheet=ss.getSheetByName('確認');
    sheet.getRange(1,7).setValue('結果（有効な検証）');
    for(let i=0;i<ui.length;i++)if(ui[i][0]) {
      const entry=entries.find(e=>e.case_id===ui[i][0]);
      if(!entry)throw Error('tax_setup_case_missing');
      if(entry.candidate_rows.length<=8)sheet.getRange(i+2,7).setFormula(receiptReviewFormulas_(true)[2]);
      sheet.getRange(i+2,97).setNote('不明のまま登録すると保留になります。原本の印字が不十分なら不明を維持してください。');
    }
    SpreadsheetApp.flush();receiptReviewTaxHistory_(ports);
    receiptReviewReadback_(ports.readRequests(),originals,'tax_setup_original_changed',RECEIPT_SHEETS_TAB);
    receiptReviewReadback_(ports.readCases(),catalog.rows,'tax_setup_catalog_changed',REVIEW_CASE_TAB);
    const strip=rows=>rows.map(r=>r.filter((_,i)=>i!==6));
    receiptReviewReadback_(strip(ports.readUI()),strip(ui),'tax_setup_input_changed','確認');
    PropertiesService.getScriptProperties().deleteProperty(REVIEW_STOP);
    return {state:'税区分修正準備済み・受付OFF',financial_write_count:0};
  });
}
