/** SYNTHETIC ONLY. No installer, trigger, token storage or live service calls.
 * reserve() MUST atomically create the immutable private request AND attach
 * UUID/snapshot digest to the same case/source row, rejecting busy/duplicates.
 * The port must not use hidden cells or read-then-write as a CAS substitute.
 * No such live port has been approved or implemented; fail closed without it.
 */
const RECEIPT_VALIDATION_PURPOSE = 'receipt-sheet-validation-only-v1';

function receiptCanonical_(value) {
  if (Array.isArray(value)) return '[' + value.map(receiptCanonical_).join(',') + ']';
  if (value && typeof value === 'object') return '{' + Object.keys(value).sort().map(
    k => receiptCanonical_(k) + ':' + receiptCanonical_(value[k])).join(',') + '}';
  return JSON.stringify(value).replace(/[\u007f-\uffff]/g,
    c => '\\u' + c.charCodeAt(0).toString(16).padStart(4, '0'));
}

function receiptParseDraft_(text, hash) {
  if (typeof text !== 'string' || text.length > 40000) throw Error('receipt_draft_invalid');
  const rows = text.split(/\r?\n/).filter(x => x.trim()).map(x => x.split('｜').map(x => x.trim()));
  if (rows.length < 3 || rows.length > 103 || rows[0].length !== 2 || rows[0][0] !== '案件' ||
      rows[1].length !== 5 || rows[1][0] !== '原本' || rows[2].length !== 2 || rows[2][0] !== '基準')
    throw Error('receipt_draft_header_invalid');
  const [,sid,sha,version,mime] = rows[1];
  if (!rows[0][1] || !/^[A-Za-z0-9_-]{10,128}$/.test(sid) || !/^[a-f0-9]{64}$/.test(sha) ||
      !version || !['image/png','image/jpeg','image/heic','image/heif'].includes(mime) || !/^[a-f0-9]{64}$/.test(rows[2][1]))
    throw Error('receipt_draft_identity_invalid');
  const operations = rows.slice(3).map(row => {
    if (row.length !== 10) throw Error('receipt_draft_columns_invalid');
    const [action,key,name,qty,amount,kind,major,minor,target,evidence] = row;
    if (!['変更','追加','除外'].includes(action) || !key || evidence.length < 4) throw Error('receipt_draft_operation_invalid');
    const op = {action:{'変更':'edit','追加':'add','除外':'remove'}[action],item_id:key,evidence:evidence};
    if (action === '除外') {
      if (row.slice(2,9).some(Boolean)) throw Error('receipt_draft_remove_invalid');
      return op;
    }
    const fields = {};
    if (name) fields.name = name;
    if (qty) {
      if (!/^[0-9]{1,7}(\.[0-9]{1,3})?$/.test(qty) || Number(qty) <= 0) throw Error('receipt_draft_quantity_invalid');
      fields.quantity = Number(qty);
    }
    if (amount) {
      if (!/^-?[0-9]{1,8}$/.test(amount)) throw Error('receipt_draft_amount_invalid');
      fields.amount = Number(amount);
    }
    if (Boolean(major) !== Boolean(minor)) throw Error('receipt_draft_category_invalid');
    if (major) fields.category = [major,minor];
    if (target) fields.target_item_id = target;
    if (action === '変更') {
      if (kind || !Object.keys(fields).length) throw Error('receipt_draft_edit_invalid');
      op.changes = fields;
    } else {
      if (!['name','quantity','amount','category'].every(k => k in fields) || !['product','tax','discount'].includes(kind))
        throw Error('receipt_draft_add_invalid');
      Object.assign(op,fields,{kind:kind});
    }
    return op;
  });
  const snapshot = {case_id:rows[0][1],source:{source_id:sid,sha256:sha,version:version,mime_type:mime,
    classification:'normal',transaction_kind:'purchase'},operations:operations,context_digest:rows[2][1]};
  snapshot.digest = hash(receiptCanonical_(snapshot));
  return snapshot;
}

function receiptValidationEdited(e, ports) {
  // e.user/effective user/trigger installer/PAT owner are NOT approval evidence.
  if (!ports) throw Error('receipt_validation_not_connected');
  if (!e || e.value !== 'TRUE' || !ports.isRegisterCell(e.range)) return null;
  const candidate = ports.readCaseAtEvent(e.range);
  const snapshot = receiptParseDraft_(candidate.draft, ports.hash);
  if (candidate.case_id !== snapshot.case_id || receiptCanonical_(candidate.source) !== receiptCanonical_(snapshot.source))
    throw Error('receipt_validation_case_mismatch');
  const requestId = ports.uuid();
  if (!/^[a-f0-9]{8}-[a-f0-9]{4}-4[a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}$/.test(requestId))
    throw Error('receipt_request_uuid_invalid');
  const request = {request_id:requestId,purpose:RECEIPT_VALIDATION_PURPOSE,snapshot:snapshot};
  // CAS against the exact observed draft and case identity. If it fails, no send.
  ports.reserve(request, candidate);
  try {
    // Same UUID-only pattern as category-submit. No original, item or identity
    // data leaves private storage. No production dispatch is wired here.
    const status = ports.dispatch({inputs:{request_id:requestId}});
    return {request_id:requestId,state:status === 204 ? '受付済み' : '結果不明・照合待ち'};
  } catch (_) {
    // Do not reset the checkbox/claim, resend, or roll back on ambiguous IO.
    return {request_id:requestId,state:'結果不明・照合待ち'};
  }
}
