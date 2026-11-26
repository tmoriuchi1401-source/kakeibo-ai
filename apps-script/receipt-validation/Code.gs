/** Pure cell decoder/snapshot helpers only. No journal or IO. */
function receiptTrim_(value) {
  return value.replace(/^[\u0009-\u000d \u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]+|[\u0009-\u000d \u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]+$/g, '');
}
function receiptName_(value) {
  if (typeof value !== 'string') throw Error('receipt_cells_name_invalid');
  const name = receiptTrim_(value);
  if (!name || Array.from(name).some(c => {
    const n = c.codePointAt(0);
    return n < 32 || n >= 127 && n <= 159 || n >= 0xd800 && n <= 0xdfff || n === 0xfeff;
  })) throw Error('receipt_cells_name_invalid');
  return name;
}
function receiptQuantity_(value) {
  if (typeof value !== 'number' || !(value > 0 && value < 10000000) ||
      !/^[0-9]{1,7}(\.[0-9]{1,3})?$/.test(String(value))) throw Error('receipt_cells_quantity_invalid');
  return value;
}
function receiptOperations_(operations) {
  if (!Array.isArray(operations)) throw Error('receipt_cells_shape_invalid');
  return operations.map(operation => {
    const op=JSON.parse(JSON.stringify(operation));
    const fields=op.action==='edit' ? op.changes : op.action==='add' ? op : {};
    if ('name' in fields) fields.name=receiptName_(fields.name);
    if ('quantity' in fields) fields.quantity=receiptQuantity_(fields.quantity);
    return op;
  });
}
function receiptCanonical_(value) {
  if (Array.isArray(value)) return '[' + value.map(receiptCanonical_).join(',') + ']';
  if (value && typeof value === 'object') return '{' + Object.keys(value).sort().map(
    k => receiptCanonical_(k) + ':' + receiptCanonical_(value[k])).join(',') + '}';
  return JSON.stringify(value).replace(/[\u007f-\uffff]/g,
    c => '\\u' + c.charCodeAt(0).toString(16).padStart(4, '0'));
}
function receiptSnapshot_(candidate, hash) {
  if (!candidate.draft || typeof candidate.draft !== 'object') throw Error('receipt_sheets_draft_invalid');
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
      const name=receiptName_(cell.name), quantity=receiptQuantity_(cell.quantity);
      if (!Number.isSafeInteger(cell.amount) ||
          !candidate.categories.some(c=>receiptCanonical_(c)===receiptCanonical_(cell.category)))
        throw Error('receipt_cells_value_invalid');
      let target=cell.target;
      if (target) {
        if (!Number.isInteger(target) || target<1 || target>draft.items.length) throw Error('receipt_cells_target_invalid');
        target=draft.items[target-1]._item_id;
      }
      const values={name,quantity,amount:cell.amount,category:cell.category,target_item_id:target};
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
  }
  snapshot.context_digest=candidate.context_digest;
  snapshot.operations=receiptOperations_(snapshot.operations);
  snapshot.digest=hash(receiptCanonical_(snapshot));
  return snapshot;
}
function receiptConnectionDecodeRow_(r,e,hash) {
  const same=(a,b)=>receiptCanonical_(a)===receiptCanonical_(b), blank=v=>v==='' || v===null;
  if(!Array.isArray(r) || r.length!==46 || r[0]!==e.case_id || r[1]!==e.kind_label ||
     !same(r.slice(36),e.machine) || !Number.isSafeInteger(r[3]) || r[3]!==e.receipt_total)
    throw Error('receipt_row_machine_binding_mismatch');
  const binding=hash(receiptCanonical_([e.case_id,e.source])), items=[];
  const kinds={'商品':'product','外税':'tax','値引き':'discount'}, targets={'商品1':1,'商品2':2,'商品3':3};
  for(let i=0;i<3;i++) {
    const [name,quantity,amount,major,minor,kind,target,mode,confirmed]=r.slice(9+9*i,18+9*i);
    const key=e.machine[4+i], old=e.original_items.find(x=>x.item_id===key);
    const noTarget=blank(target) || target==='指定なし', category=[major,minor];
    if(!old && blank(name) && blank(quantity) && blank(amount) && noTarget) {
      if(![null,'','商品'].includes(kind) || ![null,'','採用'].includes(mode) || ![null,'',false].includes(confirmed) ||
         !(blank(major) && blank(minor)) && !e.categories.some(c=>same(c,category)))
        throw Error('receipt_row_empty_addition_invalid');
      items.push({_item_id:key,_binding:binding,name:'',quantity:'',amount:'',category:null,kind:'product',target:'',mode:'採用',confirmed:false});continue;
    }
    if(!kinds[kind] || !['採用','除外'].includes(mode) || typeof confirmed!=='boolean' ||
       !noTarget && !targets[target] || !e.categories.some(c=>same(c,category)))
      throw Error('receipt_row_product_controls_invalid');
    items.push({_item_id:key,_binding:binding,name,quantity,amount,category,kind:kinds[kind],
      target:noTarget?'':targets[target],mode,confirmed});
  }
  const total=items.filter(x=>x.name && x.mode==='採用').reduce((sum,x)=>sum+x.amount,0);
  if(!Number.isSafeInteger(total) || r[4]!==total || r[5]!==r[3]-total)
    throw Error('receipt_row_display_calculation_mismatch');
  const candidate={case_id:e.case_id,source:e.source,draft:{case_id:e.case_id,source:e.source,items},
    original_items:e.original_items,categories:e.categories,context_digest:e.context_digest};
  receiptSnapshot_(candidate,hash); // Existing item identity/confirmation checks.
  return candidate;
}
