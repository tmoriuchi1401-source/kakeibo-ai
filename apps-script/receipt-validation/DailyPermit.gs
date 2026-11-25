/** Daily receiver authority kernel. No PAT parameter, storage, logging or IO.
 * The live entry is deliberately closed pending a PAT-free trusted gate reader.
 * Private kernel is exercised offline against the real review command/writer.
 */
const REVIEW_DAILY_PREFIX='RECEIPT_REVIEW_DAILY_USED_';
const REVIEW_DAILY_ACTIVE='RECEIPT_REVIEW_DAILY_ACTIVE';
const REVIEW_DAILY_CLOCK='RECEIPT_REVIEW_DAILY_CLOCK';
const REVIEW_DAILY_TTL=300000;
const REVIEW_DAILY_ACTIONS=['status','pending','lookup','read','intake','claim','finish','close'];

function receiptReviewDailyCommandShape_(command) {
  const fields={status:[],pending:[],close:[],lookup:['source_id','sha256'],read:['request_id'],
    intake:['entry','row'],claim:['request_id','before'],finish:['request_id','before','result']};
  if(!command || !Object.prototype.hasOwnProperty.call(fields,command.action) ||
     Object.keys(command).sort().join(',')!==['action',...fields[command.action]].sort().join(','))
    throw Error('daily_command_invalid');
  // Reject recognizable credentials even if hidden inside a receipt payload.
  function clean(value) {
    if(typeof value==='string' && /github_pat_|ghp_/.test(value))throw Error('daily_secret_forbidden');
    if(value && typeof value==='object')for(const [key,item] of Object.entries(value)) {
      if(['pat','token','authorization','access_token','refresh_token'].includes(key.toLowerCase()))
        throw Error('daily_secret_forbidden');
      clean(item);
    }
  }
  clean(command);
}

function receiptReviewDailyBackend() {
  // Do not accept/forward a PAT through scripts.run. Offline tests cannot prove
  // non-retention by Google's execution infrastructure. No live adapter exists.
  throw Error('daily_pat_free_gate_reader_required');
}

function receiptReviewDailyPermit_(text,binding,now) {
  let p;
  try {p=JSON.parse(text);} catch(_) {throw Error('daily_gate_invalid');}
  const fields='expires_at_ms,issued_at_ms,permit_id,review_spreadsheet_id,schema,script_id,worker_sha';
  if(!p || Array.isArray(p) || Object.keys(p).sort().join(',')!==fields ||
     p.schema!=='receipt-daily-permit-v1' ||
     !/^[a-f0-9]{8}-[a-f0-9]{4}-4[a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}$/.test(p.permit_id || '') ||
     !/^[a-f0-9]{40}$/.test(p.worker_sha || '') ||
     !['script_id','review_spreadsheet_id'].every(k=>typeof p[k]==='string' && /^[A-Za-z0-9_-]{10,150}$/.test(p[k])) ||
     !['issued_at_ms','expires_at_ms'].every(k=>Number.isSafeInteger(p[k]) && p[k]>0) ||
     p.expires_at_ms<=p.issued_at_ms || p.expires_at_ms-p.issued_at_ms>REVIEW_DAILY_TTL ||
     receiptCanonical_(p)!==text)throw Error('daily_gate_invalid');
  if(p.worker_sha!==binding.worker_sha || p.script_id!==binding.script_id ||
     p.review_spreadsheet_id!==binding.review_spreadsheet_id)throw Error('daily_binding_mismatch');
  if(!Number.isSafeInteger(now) || now<p.issued_at_ms)throw Error('daily_clock_invalid');
  if(now>=p.expires_at_ms)throw Error('daily_expired');
  return p;
}

function receiptReviewDailyRun_(envelope,ports,c,runtime) {
  // Called only under the existing ScriptLock, after owner authentication and
  // a trusted gate read. runtime is an internal dependency, never RPC input.
  const props=runtime.props;
  if(props.getProperty(REVIEW_STOP))throw Error('daily_existing_stop');
  if(c.enabled || props.getProperty('RECEIPT_GITHUB_TOKEN'))throw Error('daily_legacy_backend_active');
  if(!envelope || Object.keys(envelope).sort().join(',')!=='command,permit,sequence' ||
     !envelope.command || !REVIEW_DAILY_ACTIONS.includes(envelope.command.action) ||
     !Number.isInteger(envelope.sequence) || envelope.sequence<1 || envelope.sequence>32)
    throw Error('daily_envelope_invalid');
  if(runtime.binding.script_id!==c.script_id || runtime.binding.review_spreadsheet_id!==c.spreadsheet_id)
    throw Error('daily_binding_mismatch');
  receiptReviewDailyCommandShape_(envelope.command);
  const text=receiptCanonical_(envelope.permit),start=runtime.now();
  const p=receiptReviewDailyPermit_(text,runtime.binding,start);
  // Do not trust a caller-supplied copy of the GitHub value as authorization.
  let gate;
  try {gate=runtime.gate();} catch(_) {throw Error('daily_gate_unknown');}
  if(gate!==text)throw Error('daily_gate_mismatch');
  const key=REVIEW_DAILY_PREFIX+p.permit_id,raw=props.getProperty(key);
  let record=null;
  try {record=raw?JSON.parse(raw):null;} catch(_) {throw Error('daily_fence_invalid');}
  if(raw && (!record || Object.keys(record).sort().join(',')!=='closed,inflight,permit,sequence' ||
     typeof record.permit!=='string' || typeof record.closed!=='boolean' || typeof record.inflight!=='boolean' ||
     !Number.isInteger(record.sequence) || record.sequence<1 || record.sequence>32 ||
     receiptCanonical_(record)!==raw))throw Error('daily_fence_invalid');
  const active=props.getProperty(REVIEW_DAILY_ACTIVE),previousClock=props.getProperty(REVIEW_DAILY_CLOCK);
  if(previousClock!==null && (!/^\d+$/.test(previousClock) || start<Number(previousClock)))
    throw Error('daily_clock_invalid');
  if(record) {
    if(record.permit!==text || record.closed || record.inflight || active!==p.permit_id ||
       envelope.sequence!==record.sequence+1)throw Error('daily_replay_or_unknown');
  } else if(envelope.sequence!==1 || active || start-p.issued_at_ms>30000) {
    throw Error('daily_start_not_ready');
  }
  const action=envelope.command.action;
  const reserve=action==='intake'?120000:['claim','finish'].includes(action)?60000:15000;
  let last=start,ownStop=null;
  function check(stage) {
    const now=runtime.now();
    if(!Number.isSafeInteger(now) || now<last || now<p.issued_at_ms)throw Error('daily_clock_invalid');
    if(now>=p.expires_at_ms)throw Error('daily_expired');
    if(p.expires_at_ms-now<reserve)throw Error('daily_insufficient_time');
    const stopped=props.getProperty(REVIEW_STOP);
    if(stopped && stopped!==ownStop)throw Error('daily_existing_stop');
    last=now;
    props.setProperty(REVIEW_DAILY_CLOCK,String(now));
  }
  check('rpc');
  // Consume before any read/write. If killed here, inflight remains permanent.
  record={permit:text,sequence:envelope.sequence,inflight:true,closed:false};
  try {
    // Active first: a kill between these property writes blocks *all* new IDs.
    props.setProperty(REVIEW_DAILY_ACTIVE,p.permit_id);
    props.setProperty(key,receiptCanonical_(record));
    if(props.getProperty(key)!==receiptCanonical_(record))throw Error('daily_fence_unknown');
    if(props.getProperty(REVIEW_DAILY_ACTIVE)!==p.permit_id)throw Error('daily_fence_unknown');
    const guarded={};
    for(const [name,operation] of Object.entries(ports))guarded[name]=(...args)=>{
      check(name);return operation(...args);
    };
    guarded.beforeChange=()=>{check('before_change');ownStop='outcome_unconfirmed';};
    guarded.afterChange=()=>{check('after_change');ownStop=null;};
    guarded.beforeDecoration=()=>{check('before_decoration');ownStop='decoration_unconfirmed';};
    guarded.afterDecoration=()=>{check('after_decoration');ownStop=null;};
    // c.enabled is only an execution-local copy; never persist a long ON flag.
    const authority={check,acceptance:()=>check('acceptance')};
    const result=action==='close'?{closed:true,financial_write_count:0}:
      receiptReviewCommand_(envelope.command,guarded,{...c,enabled:true},authority);
    check('complete');
    record.inflight=false;record.closed=action==='close';
    props.setProperty(key,receiptCanonical_(record));
    if(props.getProperty(key)!==receiptCanonical_(record))throw Error('daily_fence_unknown');
    if(record.closed)props.deleteProperty(REVIEW_DAILY_ACTIVE); // Keep permanent used-ID fence.
    return {permit_id:p.permit_id,sequence:envelope.sequence,result};
  } catch(_) {
    // Never expose error text, envelope, original data, or a credential canary.
    // Never clear an existing STOP or release an uncertain permit.
    if(!props.getProperty(REVIEW_STOP))props.setProperty(REVIEW_STOP,'daily_outcome_unconfirmed');
    throw Error('daily_outcome_unconfirmed');
  }
}
