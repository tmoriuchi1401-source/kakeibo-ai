/** Explicit preparation for a separately approved, independent review book.
 * Never invoked by import, an edit, or a GET. Does not create a Drive resource,
 * trigger, OAuth credential, GitHub setting, deployment, or financial sheet.
 */
function receiptReviewColumn_(number) {
  let result='';
  while(number>0){number--;result=String.fromCharCode(65+number%26)+result;number=Math.floor(number/26);}
  return result;
}
function receiptReviewFormulas_(taxCorrections=false,policyReviews=false) {
  const at=col=>'INDEX('+col+':'+col+',ROW())';
  const terms=Array.from({length:8},(_,i)=>{
    const start=10+9*i;
    return 'IF(AND('+at(receiptReviewColumn_(start))+'<>"",'+at(receiptReviewColumn_(start+7))+'="採用"),'+at(receiptReviewColumn_(start+2))+',0)';
  });
  const rid=at(receiptReviewColumn_(94)),cid=at('A'),source=at(receiptReviewColumn_(83)),
    version=at(receiptReviewColumn_(84)),hash=at(receiptReviewColumn_(85)),baseline=at(receiptReviewColumn_(96));
  const tab="'_receipt_requests'!",catalog="'_receipt_cases'!";
  let reasons='REGEXEXTRACT(res,"""issues"":\\[([^]]*)\\]")';
  for(const [code,label] of Object.entries({item_total_mismatch:'商品合計と総額が不一致',
    tax_treatment_unknown:'税処理が不明',included_tax_not_additive:'税込の税額を重複加算',
    external_tax_addition_missing:'外税の加算漏れを確認',
    tax_discount_allocation_review:'税・値引きの配賦を確認',source_identity_mismatch:'原本ID・版・hashを確認',
    snapshot_validation_failed:'入力版・カテゴリ・金額を確認',receipt_total_owner_confirmation_required:'総額の原本照合が必要',
    existing_receipt_duplicate:'取込済みレシートとの重複を確認',
    quantity_line_amount_confirmation_required:'数量変更後の行金額を確認',category_not_in_master:'カテゴリを確認',
    receipt_date_required:'日付が不明',receipt_date_invalid:'日付が不正',merchant_required:'店舗名が不明',
    category_invalid:'カテゴリが不正',receipt_validation_failed:'入力と原本を確認'}))
    reasons='SUBSTITUTE('+reasons+',"'+code+'","'+label+'")';
  const draft='IF(LEFT(TO_TEXT('+at('I')+'),6)="ready:",IF('+at('CS')+'="不明","税区分が不明でも、金額が一致すれば登録可能","編集中・未登録"),"結果不明・照合待ち")';
  // Rearming changes only the next UUID. Keep the preceding failure visible
  // until that next request exists, bound to this case/source/baseline/history.
  const previous='IFERROR(LET(h,FILTER('+tab+'A2:J1000,'+tab+'B2:B1000='+cid+','+tab+'C2:C1000='+source+','+
    tab+'D2:D1000='+version+','+tab+'E2:E1000='+hash+'),last,ROWS(h),prid,INDEX(h,last,1),'+
    'st,INDEX(h,last,8),res,INDEX(h,last,9),dg,INDEX(h,last,7),'+
    'ok,AND(LEFT(TO_TEXT('+at('I')+'),6)="ready:",'+cid+'='+at(receiptReviewColumn_(82))+','+
    at('C')+'="https://drive.google.com/file/d/"&'+source+'&"/view",'+
    'COUNTIFS('+catalog+'A2:A1000,'+cid+','+catalog+'G2:G1000,prid)=1,'+
    'REGEXMATCH(INDEX(h,last,6),"""baseline_digest"":"""&'+baseline+'&""""),'+
    'OR(st="修正が必要",st="保留"),REGEXMATCH(res,"""posting_authority"":false[,}]"),'+
    'REGEXMATCH(res,"""financial_write_count"":0[,}]"),REGEXMATCH(res,"""request_id"":"""&prid&""""),'+
    'REGEXMATCH(res,"""snapshot_digest"":"""&dg&""""),REGEXMATCH(res,"""state"":"""&st&"""")),'+
    'IF(ok,"前回 "&st&" "&'+reasons+'&" / 修正後に登録",'+draft+')),'+draft+')';
  let display='=IF('+cid+'="","",IFERROR(LET(rid,'+rid+',n,COUNTIF('+tab+'A2:A1000,rid),'+
    'rs,FILTER('+tab+'A2:J1000,'+tab+'A2:A1000=rid),st,INDEX(rs,1,8),res,INDEX(rs,1,9),dg,INDEX(rs,1,7),'+
    'ok,AND(n=1,'+cid+'='+at(receiptReviewColumn_(82))+','+at('C')+'="https://drive.google.com/file/d/"&'+source+'&"/view",INDEX(rs,1,2)='+cid+',INDEX(rs,1,3)='+source+
    ',INDEX(rs,1,4)='+version+',INDEX(rs,1,5)='+hash+',COUNTIFS('+catalog+'A2:A1000,'+cid+','+catalog+'G2:G1000,rid)=1,'+
    'REGEXMATCH(INDEX(rs,1,6),"""baseline_digest"":"""&'+baseline+'&"""")),'+
    'IF(NOT(ok),"結果不明・照合待ち",IF(OR(st="受付済み",st="検証中"),st&"・未記帳",'+
    'IF(AND(OR(st="検証済み・未記帳",st="修正が必要",st="保留",st="記帳済み"),'+
    'REGEXMATCH(res,"""posting_authority"":false[,}]"),IF(st="記帳済み",AND(REGEXMATCH(res,"""financial_write_count"":[1-9][0-9]*[,}]"),REGEXMATCH(res,"""exact_readback"":true[,}]"),REGEXMATCH(res,"""plan_digest"":""[a-f0-9]{64}""")),REGEXMATCH(res,"""financial_write_count"":0[,}]")),'+
    'REGEXMATCH(res,"""request_id"":"""&rid&""""),REGEXMATCH(res,"""snapshot_digest"":"""&dg&""""),'+
    'REGEXMATCH(res,"""state"":"""&st&"""")),IF(st="記帳済み","記帳済み（読戻し確認済み）",st&"（登録時の内容） "&'+reasons+'&IF(REGEXMATCH(res,"""tax_treatment_unknown""")," 税区分未確定","")),"結果不明・照合待ち")))),'+
    previous+'))';
  if(taxCorrections) {
    const tx="'_receipt_tax_corrections'!",field=(text,key)=>'REGEXEXTRACT('+text+',"""'+key+'"":""([^""]+)""")';
    display='=IF('+cid+'="","",IFERROR(LET(base,'+display.slice(1)+',rid,'+rid+',n,COUNTIF('+tx+'B2:B1000,rid),'+
      'IF(n=0,base,LET(tx,FILTER('+tx+'A2:H1000,'+tx+'B2:B1000=rid),'+
      'intentRows,FILTER(tx,INDEX(tx,,5)="intent"),resultRows,FILTER(tx,INDEX(tx,,5)="result"),'+
      'res,INDEX(resultRows,1,6),intent,INDEX(intentRows,1,6),pd,INDEX(intentRows,1,7),'+
      'dg,INDEX(FILTER('+tab+'G2:G1000,'+tab+'A2:A1000=rid),1),st,'+field('res','state')+','+
      'ok,AND(n=2,ROWS(intentRows)=1,ROWS(resultRows)=1,LEFT(base,2)="保留",'+
      'INDEX(intentRows,1,1)=rid&":tax:1",INDEX(resultRows,1,1)=rid&":tax:1",'+
      'INDEX(intentRows,1,3)=dg,INDEX(resultRows,1,3)=dg,INDEX(intentRows,1,4)="1",INDEX(resultRows,1,4)="1",'+
      field('res','request_id')+'=rid,'+field('res','snapshot_digest')+'=dg,'+field('res','correction_id')+'=rid&":tax:1",'+
      field('res','correction_digest')+'=pd,'+field('res','amended_snapshot_digest')+'='+field('intent','amended_snapshot_digest')+','+
      field('res','tax_mode')+'='+field('intent','after_mode')+',REGEXMATCH(res,"""posting_authority"":false[,}]"),'+
      'REGEXMATCH(res,"""financial_write_count"":0[,}]"),OR(st="検証済み・未記帳",st="修正が必要",st="保留")),'+
      'IF(ok,st&"（税区分修正1・"&'+field('res','tax_mode')+'&"） "&'+reasons+',"結果不明・照合待ち")))),"結果不明・照合待ち"))';
  }
  if(policyReviews) {
    const tab2="'_receipt_policy_reviews'!",field=(value,key)=>'REGEXEXTRACT('+value+',"""'+key+'"":""([^""]+)""")';
    display='=IF('+cid+'="","",IFERROR(LET(base,'+display.slice(1)+',rid,'+rid+',n,COUNTIF('+tab2+'A2:A1000,rid),'+
      'IF(n=0,base,LET(pr,FILTER('+tab2+'A2:G1000,'+tab2+'A2:A1000=rid),res,INDEX(pr,1,5),'+
      'dg,INDEX(FILTER('+tab+'G2:G1000,'+tab+'A2:A1000=rid),1),st,'+field('res','state')+','+
      'ok,AND(n=1,LEFT(base,2)="保留",INDEX(pr,1,2)=dg,INDEX(pr,1,3)="receipt-review-amount-v2",'+
      field('res','request_id')+'=rid,'+field('res','snapshot_digest')+'=dg,'+
      field('res','validator_revision')+'=INDEX(pr,1,3),'+field('res','original_result_digest')+'=INDEX(pr,1,4),'+
      'REGEXMATCH(res,"""posting_authority"":false[,}]"),REGEXMATCH(res,"""financial_write_count"":0[,}]"),'+
      'REGEXMATCH(res,"""tax_treatment_unknown"""),OR(st="検証済み・未記帳",st="修正が必要",st="保留")),'+
      'IF(ok,st&"（再評価・税区分未確定） "&'+reasons+',"結果不明・照合待ち")))) ,"結果不明・照合待ち"))';
  }
  return ['='+terms.join('+'),'='+at('D')+'-'+at('E'),display];
}
function receiptReviewHideConfirmationColumns_(sheet) {
  // Keep the 98-column storage contract and all existing draft values intact.
  sheet.hideColumns(82,15);
  for(let i=0;i<8;i++)sheet.hideColumns(18+9*i);
  sheet.hideColumns(98);
}
function receiptReviewPrepare() {
  return receiptSheetsLocked_(()=>{
    const c=receiptReviewConfig_();
    if(c.enabled)throw Error('review_prepare_requires_disabled');
    const ss=SpreadsheetApp.openById(c.spreadsheet_id);
    if(ss.getSheetByName(REVIEW_CASE_TAB) || ss.getSheetByName(RECEIPT_SHEETS_TAB))throw Error('review_existing_catalog_preserved');
    const ui=ss.getSheetByName('確認');
    if(!ui || ui.getLastRow()>1)throw Error('review_prepare_requires_empty_independent_ui');
    if(ui.getMaxColumns()<98)ui.insertColumnsAfter(ui.getMaxColumns(),98-ui.getMaxColumns());
    const headers=['案件ID','種別','原本','支払総額','商品合計','差額','結果（登録時）','途中メモ','登録（表示内容を一括承認）'];
    for(let i=1;i<=8;i++)headers.push(...['商品名','数量','行金額','大カテゴリ','小カテゴリ','種類','対象商品','採用／除外','原本照合'].map(x=>'商品'+i+' '+x));
    headers.push('固定案件ID','原本ID','原本版','原本hash',...Array.from({length:8},(_,i)=>'商品ID'+(i+1)),
      '要求UUID','入力形式版','基準digest','税の扱い','総額の原本照合');
    ui.getRange(1,1,1,98).setValues([headers]);ui.setFrozenRows(1);ui.setFrozenColumns(1);
    receiptReviewHideConfirmationColumns_(ui);
    ui.getRange(1,1,1000,98).setWrap(true);
    for(const [name,h] of [[REVIEW_CASE_TAB,REVIEW_CASE_HEADERS],[RECEIPT_SHEETS_TAB,RECEIPT_SHEETS_HEADERS]]) {
      const sheet=ss.insertSheet(name);sheet.getRange(1,1,1,h.length).setValues([h]);sheet.hideSheet();
      const protection=sheet.protect();protection.removeEditors(protection.getEditors());
      if(protection.canDomainEdit())protection.setDomainEdit(false);
    }
    return {state:'準備済み・受付OFF',financial_write_count:0};
  });
}
function receiptReviewDecorate_(ss,number,e) {
  const sheet=ss.getSheetByName('確認'),rule=values=>SpreadsheetApp.newDataValidation().requireValueInList(values,true).setAllowInvalid(false).build();
  receiptReviewHideConfirmationColumns_(sheet);
  sheet.getRange(number,9).setDataValidation(SpreadsheetApp.newDataValidation()
    .requireCheckbox('register:'+e.request_id,'ready:'+e.request_id).build());
  sheet.getRange(number,9).setNote('表示中の全明細・支払総額・税の扱いを一括承認して登録します。原本の目視確認を証明する操作ではありません。');
  for(let i=0;i<8;i++) {
    const start=10+i*9;
    sheet.getRange(number,start+3).setDataValidation(rule([...new Set(e.categories.map(c=>c[0]))]));
    sheet.getRange(number,start+4).setDataValidation(rule([...new Set(e.categories.map(c=>c[1]))]));
    sheet.getRange(number,start+5).setDataValidation(rule(['商品','外税','値引き']));
    sheet.getRange(number,start+6).setDataValidation(rule(['指定なし',...Array.from({length:8},(_,j)=>'商品'+(j+1))]));
    sheet.getRange(number,start+7).setDataValidation(rule(['採用','除外']));
    sheet.getRange(number,start+8).clearDataValidations();
  }
  sheet.getRange(number,97).setDataValidation(rule(['税込','税別','不明']));
  sheet.getRange(number,97).setNote('税区分は任意です。不明でも支払総額と明細が一致すれば登録できます。');
  sheet.getRange(number,98).clearDataValidations();
  // Row() references survive complete-row sorts. Only derived cells get formulas.
  if(e.candidate_rows.length<=8)sheet.getRange(number,5,1,3).setFormulas([receiptReviewFormulas_(
    !!ss.getSheetByName('_receipt_tax_corrections'),!!ss.getSheetByName('_receipt_policy_reviews'))]);
  SpreadsheetApp.flush();
}
