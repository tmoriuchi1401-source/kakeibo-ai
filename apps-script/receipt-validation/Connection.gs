/** #200 connection adapter. NOT installed/enabled by adding this file.
 * Uses Code.gs unchanged, the existing private-envelope/journal schemas and
 * category-submit's UUID-only dispatch pattern. No financial operation.
 * First acceptance is limited to the isolated workbook's single case row.
 * Configuration and the existing repo-scoped PAT belong in User Properties.
 */
const RECEIPT_CONNECTION_CONFIG = 'RECEIPT_VALIDATION_CONFIG_JSON';
const RECEIPT_CONNECTION_STOP = 'RECEIPT_VALIDATION_STOPPED';

function receiptConnectionHash_(text) {
  return Utilities.computeDigest(Utilities.DigestAlgorithm.SHA_256, text, Utilities.Charset.UTF_8)
    .map(b => ('0' + ((b + 256) % 256).toString(16)).slice(-2)).join('');
}

function receiptConnectionConfig_(props, allowDisabled=false) {
  const c = JSON.parse(props.getProperty(RECEIPT_CONNECTION_CONFIG) || 'null');
  if(c && typeof c.expected_json==='string') c.expected=JSON.parse(c.expected_json);
  if (!c || typeof c.enabled!=='boolean' || !allowDisabled && c.enabled!==true || c.synthetic_only !== true ||
      !['spreadsheet_id','private_id','folder_id'].every(k => /^[A-Za-z0-9_-]{10,150}$/.test(c[k])) ||
      new Set([c.spreadsheet_id,c.private_id,c.folder_id]).size !== 3 ||
      !/^[a-f0-9]{64}$/.test(c.catalog_digest) || !/^[a-f0-9]{64}$/.test(c.expected_digest) ||
      !c.expected || receiptConnectionHash_(c.expected_json) !== c.expected_digest ||
      typeof c.catalog_json!=='string' || receiptConnectionHash_(c.catalog_json)!==c.catalog_digest ||
      !c.owner_email || !c.service_email || c.owner_email === c.service_email ||
      c.repository !== 'tmoriuchi1401-source/kakeibo-ai' ||
      c.workflow !== 'category-sheet-request.yml' ||
      typeof c.ref !== 'string' || !c.ref.startsWith('feat/receipt-') ||
      typeof c.case_id !== 'string' || c.expected.case_id !== c.case_id)
    throw Error('receipt_connection_configuration_invalid');
  if (Session.getEffectiveUser().getEmail() !== c.owner_email)
    throw Error('receipt_connection_installer_mismatch');
  return c;
}

function receiptConnectionStoreConfig_(c,props) {
  const stored={...c};delete stored.expected;
  const text=JSON.stringify(stored);
  if(text.length>9000) throw Error('receipt_connection_configuration_too_large');
  props.setProperty(RECEIPT_CONNECTION_CONFIG,text);
}

function receiptConnectionConfigure() {
  // Explicit editor invocation AFTER approval. Same staging -> User Properties
  // pattern as category-submit; no new PAT, token scope or trigger here.
  const staged=PropertiesService.getScriptProperties(), props=PropertiesService.getUserProperties();
  const c=receiptConnectionConfig_(staged,true);
  if(c.enabled) throw Error('receipt_connection_stage_disabled_configuration');
  const token=staged.getProperty('CATEGORY_GITHUB_TOKEN') || props.getProperty('CATEGORY_GITHUB_TOKEN');
  if(!token || !token.startsWith('github_pat_')) throw Error('receipt_connection_existing_token_required');
  receiptConnectionStoreConfig_(c,props);
  props.setProperty('RECEIPT_GITHUB_TOKEN',token);
  staged.deleteProperty(RECEIPT_CONNECTION_CONFIG);
  staged.deleteProperty('CATEGORY_GITHUB_TOKEN');
}

function receiptConnectionStart() {
  // Separately approved, explicit editor invocation. Never installs itself.
  const props=PropertiesService.getUserProperties(), c=receiptConnectionConfig_(props,true);
  if(c.enabled || props.getProperty(RECEIPT_CONNECTION_STOP) ||
     !props.getProperty('RECEIPT_GITHUB_TOKEN') || ScriptApp.getProjectTriggers().length!==0)
    throw Error('receipt_connection_start_not_ready');
  receiptConnectionPorts_(c,props).readVersioned(); // Read-only pinned preflight.
  ScriptApp.newTrigger('receiptConnectionEdited').forSpreadsheet(c.spreadsheet_id).onEdit().create();
  c.enabled=true;receiptConnectionStoreConfig_(c,props);
}

function receiptConnectionStop() {
  const props=PropertiesService.getUserProperties(), c=receiptConnectionConfig_(props,true);
  c.enabled=false;receiptConnectionStoreConfig_(c,props);
  const own=ScriptApp.getProjectTriggers().filter(t=>t.getHandlerFunction()==='receiptConnectionEdited' &&
    t.getTriggerSourceId()===c.spreadsheet_id);
  if(own.length>1) throw Error('receipt_connection_unexpected_trigger_count');
  if(own.length===1) ScriptApp.deleteTrigger(own[0]);
  props.deleteProperty('RECEIPT_GITHUB_TOKEN'); // Only this trial's copied value.
  // The uncertainty latch and private journal are deliberately retained.
}

function receiptConnectionPorts_(c, props) {
  const hash=receiptConnectionHash_, same=(a,b)=>receiptCanonical_(a)===receiptCanonical_(b);
  const strong=tag=>typeof tag==='string' && /^"[^"\r\n]+"$/.test(tag);
  const copy=x=>JSON.parse(JSON.stringify(x));
  const metaFields='id,etag,version,parents(id),labels(trashed),mimeType,owners(emailAddress),permissions(type,role,emailAddress,deleted)';
  const driveRoot='https://www.googleapis.com/drive/v2/files/';
  function fetch_(url, options) {
    let response;
    try {response=UrlFetchApp.fetch(url, {...options, followRedirects:false, muteHttpExceptions:true,
      headers:{...(options.headers || {}), Authorization:'Bearer '+ScriptApp.getOAuthToken()}});}
    catch (_) {throw Error('receipt_connection_drive_unavailable');}
    if(response.getResponseCode()!==200) throw Error('receipt_connection_drive_unavailable');
    const text=response.getContentText();
    if(text.length>1048576) throw Error('receipt_connection_size_limit');
    return text;
  }
  function metadata(fid,mime) {
    const m=JSON.parse(fetch_(driveRoot+fid+'?fields='+encodeURIComponent(metaFields),{method:'get'}));
    const grants=(m.permissions || []).filter(p=>!p.deleted).map(p=>[p.type,p.role,p.emailAddress]).sort();
    if(m.id!==fid || m.mimeType!==mime || !m.labels || m.labels.trashed!==false ||
       !strong(m.etag) || typeof m.version!=='string' || !m.version ||
       !same((m.owners || []).map(o=>o.emailAddress),[c.owner_email]) ||
       !same(grants,[['user','owner',c.owner_email],['user','writer',c.service_email]].sort()) ||
       fid===c.private_id && !same(m.parents,[{id:c.folder_id}]))
      throw Error('receipt_connection_acl_or_identity_changed');
    return m;
  }
  function load() {
    const folder=metadata(c.folder_id,'application/vnd.google-apps.folder');
    const before=metadata(c.private_id,'application/json');
    const text=fetch_(driveRoot+c.private_id+'?alt=media',{method:'get'});
    if(!same(before,metadata(c.private_id,'application/json')) ||
       !same(folder,metadata(c.folder_id,'application/vnd.google-apps.folder')))
      throw Error('receipt_connection_private_changed');
    const envelope=JSON.parse(text), catalog=envelope.catalog, j=envelope.journal;
    if(!same(Object.keys(envelope).sort(),['catalog','journal','schema','version']) ||
       envelope.schema!=='receipt-private-envelope-v1' || envelope.version!==1 ||
       !catalog || !catalog.cases || !same(Object.keys(catalog.cases),[c.case_id]) ||
       !same(catalog,JSON.parse(c.catalog_json)) ||
       !j || j.schema!=='receipt-sheet-validation-journal-v2' || !Number.isSafeInteger(j.generation) ||
       j.generation<0 || !j.requests || !j.active_prepared || Object.keys(j.requests).length>1000)
      throw Error('receipt_connection_private_invalid');
    const entry=catalog.cases[c.case_id], anchor=entry.anchor;
    if(!same(anchor.source,c.expected.source) || hash(receiptCanonical_(anchor))!==c.expected.context_digest ||
       !same(entry.categories,c.expected.categories) || !same(entry.decoder.machine,c.expected.machine) ||
       entry.decoder.kind_label!==c.expected.kind_label || !anchor.total_confirmation ||
       !same(anchor.total_confirmation.source,c.expected.source) ||
       anchor.total_confirmation.receipt_total!==c.expected.receipt_total ||
       !/^[a-f0-9]{64}$/.test(anchor.total_confirmation.actor_id))
      throw Error('receipt_connection_catalog_binding_invalid');
    return {envelope,tag:before.etag};
  }
  function save(before, after) {
    if(props.getProperty(RECEIPT_CONNECTION_STOP)) throw Error('receipt_connection_stopped');
    // The existing category receiver also records uncertainty before sending.
    // A killed execution or lost response keeps this latch across invocations.
    props.setProperty(RECEIPT_CONNECTION_STOP,'private_write_outcome_unconfirmed');
    // Preserve the existing Python-canonical catalog bytes, including 1.0.
    // Only the unchanged journal schema is serialized by the existing JS core.
    const payload='{"catalog":'+c.catalog_json+',"journal":'+receiptCanonical_(after.journal)+
      ',"schema":"receipt-private-envelope-v1","version":1}';
    fetch_('https://www.googleapis.com/upload/drive/v2/files/'+c.private_id+'?uploadType=media&fields=id,etag',
      {method:'put',contentType:'application/json',payload,headers:{'If-Match':before.tag}});
    const readback=load();
    if(readback.tag===before.tag || !same(readback.envelope,after))
      throw Error('receipt_connection_readback_unknown');
    props.deleteProperty(RECEIPT_CONNECTION_STOP);
  }
  function duplicates(j,snapshot) {
    for(const old of Object.values(j.requests)) {
      if(old.state==='下書き準備済み') continue;
      const s=old.snapshot, sameCase=s.case_id===snapshot.case_id, sameSource=s.source.source_id===snapshot.source.source_id;
      if(sameSource && !sameCase || (sameSource || sameCase) &&
         (!['修正が必要','保留','検証済み・未記帳'].includes(old.state) || same(s,snapshot)))
        throw Error('receipt_validation_duplicate_or_unresolved');
    }
  }
  function workbook() {
    const ss=SpreadsheetApp.openById(c.spreadsheet_id), sheet=ss.getSheetByName('確認');
    if(!sheet || sheet.getLastRow()>1000) throw Error('receipt_connection_sheet_invalid');
    const rows=sheet.getRange(2,1,Math.max(sheet.getLastRow()-1,1),46).getValues();
    const nonempty=rows.map((r,i)=>({row:r,number:i+2})).filter(x=>x.row.some(v=>v!=='' && v!==null));
    // No other case can inherit a checkbox while sort/publication is in flight.
    if(nonempty.length!==1 || nonempty[0].row[0]!==c.case_id)
      throw Error('receipt_connection_single_case_required');
    return {sheet,...nonempty[0]};
  }
  const ports={hash,uuid:()=>Utilities.getUuid(),
    readVersioned:()=>{const x=load();return [copy(x.envelope.journal),x.tag];},
    readCaseById:caseId=>{
      if(caseId!==c.case_id) throw Error('receipt_connection_case_invalid');
      return receiptConnectionDecodeRow_(workbook().row,c.expected,hash);
    },
    readPrepared:rid=>copy(load().envelope.journal.requests[rid]),
    isRegisterCell:range=>range && range.getSheet().getParent().getId()===c.spreadsheet_id &&
      range.getSheet().getName()==='確認' && range.getColumn()===9 && range.getNumRows()===1 && range.getNumColumns()===1,
    prepare:(request,options)=>{
      if(props.getProperty(RECEIPT_CONNECTION_STOP)) throw Error('receipt_connection_stopped');
      const before=load(), after=copy(before.envelope), j=after.journal, s=request.snapshot;
      if(before.tag!==options.expected_tag || request.purpose!==RECEIPT_VALIDATION_PURPOSE ||
         s.case_id!==c.case_id || !same(s.source,c.expected.source) ||
         s.context_digest!==c.expected.context_digest || j.requests[request.request_id])
        throw Error('receipt_validation_preparation_stale');
      const unsigned=copy(s);delete unsigned.digest;
      if(hash(receiptCanonical_(unsigned))!==s.digest) throw Error('receipt_connection_snapshot_invalid');
      duplicates(j,s);
      j.requests[request.request_id]={...copy(request),state:'下書き準備済み',result:null};
      j.active_prepared[c.case_id]=request.request_id;j.generation++;
      save(before,after);
    },
    acceptPrepared:(rid,snapshotDigest)=>{
      const before=load(), after=copy(before.envelope), j=after.journal, record=j.requests[rid];
      if(!record || record.state!=='下書き準備済み' || record.snapshot.digest!==snapshotDigest ||
         j.active_prepared[c.case_id]!==rid || record.snapshot.case_id!==c.case_id)
        throw Error('receipt_validation_replay_rejected');
      duplicates(j,record.snapshot);
      record.state='受付済み';delete j.active_prepared[c.case_id];j.generation++;
      save(before,after);return copy(record);
    },
    dispatch:payload=>{
      const token=props.getProperty('RECEIPT_GITHUB_TOKEN');
      if(!token || !token.startsWith('github_pat_')) throw Error('receipt_connection_existing_token_required');
      const response=UrlFetchApp.fetch('https://api.github.com/repos/'+c.repository+'/actions/workflows/'+c.workflow+'/dispatches',
        {method:'post',contentType:'application/json',followRedirects:false,muteHttpExceptions:true,
         headers:{Authorization:'Bearer '+token,Accept:'application/vnd.github+json','X-GitHub-Api-Version':'2022-11-28'},
         payload:JSON.stringify({ref:c.ref,inputs:payload.inputs})});
      return response.getResponseCode();
    },
    publishCheckbox:rule=>{
      const current=workbook();
      current.sheet.getRange(current.number,9).setDataValidation(SpreadsheetApp.newDataValidation()
        .requireCheckbox(rule.checkedValue,rule.uncheckedValue).build()).setValue(rule.uncheckedValue);
      SpreadsheetApp.flush();
    }
  };
  return ports;
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

function receiptConnectionEdited(e) {
  const props=PropertiesService.getUserProperties();
  if(props.getProperty(RECEIPT_CONNECTION_STOP)) throw Error('receipt_connection_stopped');
  const c=receiptConnectionConfig_(props);
  if(!e || !e.range || e.range.getSheet().getParent().getId()!==c.spreadsheet_id ||
     e.range.getSheet().getName()!=='確認') return;
  const lock=LockService.getScriptLock();
  if(!lock.tryLock(1000)) throw Error('receipt_connection_busy');
  try {
    const ports=receiptConnectionPorts_(c,props), first=e.range.getColumn(), last=first+e.range.getNumColumns()-1;
    if(first===9 && last===9) return receiptValidationEdited(e,ports);
    if(first<=36 && last>=10) {
      const rule=receiptPrepareDraft(c.case_id,ports);
      ports.publishCheckbox(rule);
      return {state:'下書き準備済み'};
    }
  } finally {lock.releaseLock();}
}
