/** SYNTHETIC ONLY. No installer, trigger, token storage or live service calls.
 * prepare/acceptPrepared operate ONLY on the private CAS journal. Sheets is
 * an input/display projection, never half of a cross-service transaction.
 * Custom checkbox values carry a previously prepared UUID in the edit event.
 * No live port, installer, resource creation or credentials are provided.
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
  if (!e || !ports.isRegisterCell(e.range)) return null;
  if (e.value === 'FALSE' || String(e.value).startsWith('ready:')) return null;
  // TRUE and a row coordinate cannot identify the case BEFORE a delayed sort.
  // Google documents custom checkbox values and single-cell e.value/oldValue.
  const match = /^register:([a-f0-9]{8}-[a-f0-9]{4}-4[a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12})$/.exec(e.value);
  if (!match || e.oldValue !== 'ready:' + match[1]) throw Error('receipt_validation_event_identity_unproven');
  const requestId = match[1];
  const prepared = ports.readPrepared(requestId);
  if (prepared.request_id !== requestId || prepared.purpose !== RECEIPT_VALIDATION_PURPOSE ||
      prepared.state !== '下書き準備済み') throw Error('receipt_validation_replay_rejected');
  // Never look up a case using the event row; it may now contain another case.
  const candidate = ports.readCaseById(prepared.snapshot.case_id);
  // Legacy delimited text remains an offline fixture only. The new raw-cell
  // reader port compiles ordinary product cells to this logical draft object.
  const snapshot = receiptSnapshot_(candidate, ports.hash);
  if (candidate.case_id !== snapshot.case_id || receiptCanonical_(candidate.source) !== receiptCanonical_(snapshot.source) ||
      receiptCanonical_(snapshot) !== receiptCanonical_(prepared.snapshot))
    throw Error('receipt_validation_case_mismatch');
  let receipt;
  try {
    receipt = ports.acceptPrepared(requestId, snapshot.digest);
  } catch (_) {
    // The private save itself may have committed. No dispatch, rollback/retry.
    return {request_id:requestId,state:'結果不明・照合待ち'};
  }
  let state = '結果不明・照合待ち';
  try {
    // Same UUID-only pattern as category-submit. No original, item or identity
    // data leaves private storage. No production dispatch is wired here.
    const status = ports.dispatch({inputs:{request_id:requestId}});
    if (status === 204) state = '受付済み';
  } catch (_) {} // A lost dispatch response must never cause another send.
  let displayPending = false;
  // Optional self-keyed cache publication, never update an input/case row.
  try { if (ports.publishReceipt) ports.publishReceipt(receipt); }
  catch (_) { displayPending = true; }
  return {request_id:requestId,state:state,display_pending:displayPending};
}

function receiptSnapshot_(candidate, hash) {
  if (typeof candidate.draft === 'string') return receiptParseDraft_(candidate.draft,hash);
  const draft = candidate.draft;
  const snapshot = JSON.parse(JSON.stringify(draft));
  delete snapshot.digest;
  if (Array.isArray(draft.items)) {
    const originals = candidate.original_items;
    const binding = hash(receiptCanonical_([draft.case_id,draft.source]));
    const seen = new Set(), operations = [];
    if (!Array.isArray(originals) || draft.items.length > 100) throw Error('receipt_cells_shape_invalid');
    for (const cell of draft.items) {
      const key=cell._item_id, old=originals.find(x=>x.item_id===key);
      if (cell._binding!==binding || seen.has(key) || !['採用','除外'].includes(cell.mode) || typeof cell.confirmed!=='boolean')
        throw Error('receipt_cells_identity_invalid');
      seen.add(key);
      if (!old && !/^new:[A-Za-z0-9_-]{8,64}$/.test(key)) throw Error('receipt_cells_item_invalid');
      if (!old && cell.name==='' && cell.mode==='採用' && ['quantity','amount','target'].every(k=>cell[k]===null || cell[k]==='')) continue;
      const evidence='原本照合済み（入力者の申告・本人認証ではない）';
      if (cell.mode==='除外') {
        if (!old || !cell.confirmed) throw Error('receipt_cells_remove_unconfirmed');
        operations.push({action:'remove',item_id:key,evidence});continue;
      }
      if (typeof cell.name!=='string' || !cell.name.trim() || !Number.isSafeInteger(cell.amount) ||
          typeof cell.quantity!=='number' || !Number.isFinite(cell.quantity) || cell.quantity<=0 ||
          !candidate.categories.some(c=>receiptCanonical_(c)===receiptCanonical_(cell.category)))
        throw Error('receipt_cells_value_invalid');
      let target=cell.target;
      if (target) {
        if (!Number.isInteger(target) || target<1 || target>draft.items.length) throw Error('receipt_cells_target_invalid');
        target=draft.items[target-1]._item_id;
      }
      const values={name:cell.name.trim(),quantity:cell.quantity,amount:cell.amount,category:cell.category,target_item_id:target};
      let op;
      if (old) {
        if (cell.kind!==old.kind) throw Error('receipt_cells_original_kind_changed');
        const changes={};
        for(const k of Object.keys(values)) if(receiptCanonical_(values[k])!==receiptCanonical_(old[k]===undefined?'':old[k])) changes[k]=values[k];
        if (!Object.keys(changes).length) continue;
        op={action:'edit',item_id:key,changes,evidence};
      } else op={action:'add',item_id:key,kind:cell.kind,...values,evidence};
      if(!cell.confirmed) throw Error('receipt_cells_original_check_required');
      operations.push(op);
    }
    if(!originals.every(x=>seen.has(x.item_id))) throw Error('receipt_cells_missing_original_not_explicitly_excluded');
    delete snapshot.items;
    snapshot.operations=operations;
    snapshot.context_digest=candidate.context_digest;
  }
  snapshot.digest=hash(receiptCanonical_(snapshot));
  return snapshot;
}

function receiptPrepareDraft(candidate, ports) {
  // Background draft-save stage, NOT another user confirmation. No dispatch.
  // Live publication of the returned checkbox rule requires separate approval.
  const snapshot=receiptSnapshot_(candidate,ports.hash), rid=ports.uuid();
  if(!/^[a-f0-9]{8}-[a-f0-9]{4}-4[a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}$/.test(rid)) throw Error('receipt_request_uuid_invalid');
  ports.prepare({request_id:rid,purpose:RECEIPT_VALIDATION_PURPOSE,snapshot});
  return {checkedValue:'register:'+rid,uncheckedValue:'ready:'+rid};
}
