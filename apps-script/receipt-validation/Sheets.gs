/** Review-only Sheets authority receiver. No installer or resource creation.
 * Load Code.gs (pure decoder/snapshot helpers) and this file for the new path.
 * Connection.gs routes the existing handler and Configure/Start/Stop names.
 * All data writes are single-attempt Sheets RAW values. No Drive journal IO.
 */
const RECEIPT_SHEETS_HEADERS = ['request_id','receipt_id','source_id','source_version','source_sha256',
  'snapshot_json','snapshot_digest','status','validation_result','updated_at'];
const RECEIPT_SHEETS_TAB = '_receipt_requests';
const RECEIPT_SHEETS_STOP = 'RECEIPT_SHEETS_STOPPED'; // Circuit breaker, never a request record.

function receiptSheetsSource_(s,allowSinglePagePdf=false) {
  const required=['classification','mime_type','sha256','source_id','transaction_kind'];
  if(!s || typeof s!=='object' || Array.isArray(s) ||
     !required.every(k=>Object.prototype.hasOwnProperty.call(s,k)) ||
     Object.keys(s).some(k=>!required.includes(k) && k!=='version') ||
     typeof s.source_id!=='string' || !/^[A-Za-z0-9_-]{10,128}$/.test(s.source_id) ||
     typeof s.sha256!=='string' || !/^[a-f0-9]{64}$/.test(s.sha256) ||
     !['image/png','image/jpeg','image/heic','image/heif',...(allowSinglePagePdf?['application/pdf']:[])].includes(s.mime_type) ||
     s.classification!=='normal' || s.transaction_kind!=='purchase' ||
     Object.prototype.hasOwnProperty.call(s,'version') && typeof s.version!=='string')
    throw Error('receipt_sheets_source_invalid');
}

function receiptSheetsCheckRow_(r,hash,allowSinglePagePdf=false) {
  if(!Array.isArray(r) || r.length!==10 || !r.every(x=>typeof x==='string' && x.length<=40000) ||
     !/^[a-f0-9]{8}-[a-f0-9]{4}-4[a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}$/.test(r[0]) ||
     !/^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$/.test(r[9]))
    throw Error('receipt_sheets_row_invalid');
  const s=JSON.parse(r[5]);
  receiptSheetsSource_(s.source,allowSinglePagePdf);
  if(receiptCanonical_(s)!==r[5] || hash(r[5])!==r[6] || s.case_id!==r[1] ||
     !s.source || s.source.source_id!==r[2] || (s.source.version || "")!==r[3] || s.source.sha256!==r[4] ||
     !['受付済み','検証中','検証済み・未記帳','修正が必要','保留','記帳済み'].includes(r[7]))
    throw Error('receipt_sheets_row_mismatch');
  if(['受付済み','検証中'].includes(r[7])) {
    if(r[8]!=='') throw Error('receipt_sheets_result_invalid');
  } else {
    const result=JSON.parse(r[8]);
    if(result.request_id!==r[0] || result.snapshot_digest!==r[6] || result.state!==r[7] ||
       result.posting_authority!==false || !receiptHostedResult_(result) || !Array.isArray(result.issues))
      throw Error('receipt_sheets_result_invalid');
  }
}

function receiptSheetsHash_(text) {
  return Utilities.computeDigest(Utilities.DigestAlgorithm.SHA_256,text,Utilities.Charset.UTF_8)
    .map(b=>('0'+((b+256)%256).toString(16)).slice(-2)).join('');
}

function receiptSheetsLocked_(work) {
  const lock=LockService.getScriptLock();
  if(!lock.tryLock(1000)) throw Error('receipt_sheets_busy');
  try {return work();} finally {lock.releaseLock();}
}


// Historical PAT-backed receivers are intentionally not imported.
function receiptSheetsAcceptance_() { throw Error('legacy_review_connection_disabled'); }
