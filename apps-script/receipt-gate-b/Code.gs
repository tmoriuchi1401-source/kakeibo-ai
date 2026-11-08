/** @OnlyCurrentDoc
 * Gate B observation only. No request acceptance, validator or dispatch.
 * Configure only in the separately approved bound synthetic project.
 */
// PRIVATE_CONFIG_START (replace privately, never commit real IDs/UUIDs)
const RECEIPT_GATE_B_CONFIG = Object.freeze({
  enabled: false,
  bookId: 'UNCONFIGURED',
  sheetId: -1,
  logSheetId: -1,
  runId: 'UNCONFIGURED',
  ticket: {uuid: 'UNCONFIGURED', caseId: 'UNCONFIGURED', machine: []}
});
// PRIVATE_CONFIG_END

const RECEIPT_GATE_B_HEADERS = Object.freeze([
  'UTC','run_id','trigger_uid','sheet_id','event_row','column','rows','columns',
  'value','old_value','request_uuid','case_id','verdict'
]);

function receiptGateBConfig_(c) {
  if (!c || !/^[A-Za-z0-9_-]{10,150}$/.test(c.bookId) ||
      !Number.isSafeInteger(c.sheetId) || c.sheetId < 0 ||
      !Number.isSafeInteger(c.logSheetId) || c.logSheetId < 0 || c.sheetId === c.logSheetId ||
      !/^GATE-B-[A-Za-z0-9-]{1,48}$/.test(c.runId) || !c.ticket ||
      !/^[a-f0-9]{8}-[a-f0-9]{4}-4[a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}$/.test(c.ticket.uuid) ||
      !/^SYN-[A-Z0-9-]{1,60}$/.test(c.ticket.caseId) || !Array.isArray(c.ticket.machine) ||
      c.ticket.machine.length !== 10 || c.ticket.machine[0] !== c.ticket.caseId ||
      c.ticket.machine[7] !== c.ticket.uuid || c.ticket.machine[8] !== c.runId ||
      !/^[a-f0-9]{64}$/.test(c.ticket.machine[3]) || !/^[a-f0-9]{64}$/.test(c.ticket.machine[9]))
    throw Error('GATE_B_NOT_CONFIGURED');
}

function receiptGateBClassify_(event, config, bindings, history) {
  receiptGateBConfig_(config);
  const ticket = config.ticket, checked = 'register:' + ticket.uuid, ready = 'ready:' + ticket.uuid;
  const safe = value => value === checked || value === ready ? value :
    value === undefined ? '<missing>' : value === 'TRUE' || value === true ? '<TRUE>' : '<invalid>';
  const result = {value:safe(event.value), oldValue:safe(event.oldValue), requestId:'', caseId:'', verdict:'REJECT_EVENT_IDENTITY'};
  if (event.rows !== 1 || event.columns !== 1 || event.column !== 9) {
    result.verdict = 'REJECT_MULTI_CELL'; return result;
  }
  if (event.value !== checked || event.oldValue !== ready) return result;
  // The event token, never the current event row, selects the fixture case.
  result.requestId = ticket.uuid; result.caseId = ticket.caseId;
  const matches = bindings.filter(row => row[43] === ticket.uuid);
  if (matches.length !== 1 || matches[0][0] !== ticket.caseId ||
      JSON.stringify(matches[0].slice(36,46)) !== JSON.stringify(ticket.machine)) {
    result.verdict = 'REJECT_CASE_BINDING'; return result;
  }
  if (history.some(row => row[10] === ticket.uuid && row[12] === 'OBSERVED_UNAPPROVED')) {
    result.verdict = 'REJECT_DUPLICATE'; return result;
  }
  result.verdict = 'OBSERVED_UNAPPROVED';
  return result;
}

function receiptGateBTargets_(book, config) {
  receiptGateBConfig_(config);
  if (!book || book.getId() !== config.bookId) throw Error('GATE_B_WRONG_BOOK');
  const sheets = book.getSheets();
  const review = sheets.find(s => s.getSheetId() === config.sheetId);
  const log = sheets.find(s => s.getSheetId() === config.logSheetId);
  if (sheets.length !== 2 || !review || review.getName() !== '確認' || review.isSheetHidden() ||
      !log || log.getName() !== '_gate_b_events' ||
      !log.isSheetHidden() || log.getLastRow() < 1 || log.getLastRow() >= 200 ||
      JSON.stringify(log.getRange(1,1,1,13).getValues()[0]) !== JSON.stringify(RECEIPT_GATE_B_HEADERS))
    throw Error('GATE_B_LAYOUT_CHANGED');
  return {review,log};
}

// Read-only authorization/preflight entry. Never creates a trigger or writes.
function receiptGateBPreflight() {
  receiptGateBTargets_(SpreadsheetApp.getActiveSpreadsheet(), RECEIPT_GATE_B_CONFIG);
  return 'GATE_B_PREFLIGHT_ONLY_NOT_DEVICE_ACCEPTANCE';
}

function receiptGateBObserveEdit(e) {
  return receiptGateBObserve_(e, RECEIPT_GATE_B_CONFIG, LockService, SpreadsheetApp);
}

function receiptGateBObserve_(e, config, locks, spreadsheets) {
  receiptGateBConfig_(config);
  if (config.enabled !== true) throw Error('GATE_B_DISABLED');
  if (!e || !e.source || !e.range) return null;
  if (e.source.getId() !== config.bookId || e.range.getSheet().getSheetId() !== config.sheetId ||
      e.range.getSheet().getParent().getId() !== config.bookId) return null;
  const column = e.range.getColumn(), columns = e.range.getNumColumns();
  if (column > 9 || column + columns - 1 < 9) return null;
  // A manual/synthetic call is not an installable device event.
  if (typeof e.triggerUid !== 'string' || !/^[0-9]{1,80}$/.test(e.triggerUid))
    throw Error('GATE_B_INSTALLABLE_EVENT_REQUIRED');
  const lock = locks.getScriptLock();
  if (!lock.tryLock(1000)) throw Error('GATE_B_BUSY_STOP');
  try {
    const targets = receiptGateBTargets_(e.source,config);
    const lastRow = targets.review.getLastRow();
    if (lastRow < 2 || lastRow > 200) throw Error('GATE_B_REVIEW_BOUNDS_CHANGED');
    const bindings = targets.review.getRange(2,1,lastRow-1,46).getValues();
    const logRows = targets.log.getLastRow();
    const history = logRows > 1 ? targets.log.getRange(2,1,logRows-1,13).getValues() : [];
    const event = {column,columns,rows:e.range.getNumRows(),value:e.value,oldValue:e.oldValue};
    const result = receiptGateBClassify_(event,config,bindings,history);
    // Row is diagnostic only. No input, memo, original link or case result write.
    const record = [new Date().toISOString(),config.runId,e.triggerUid,config.sheetId,
      e.range.getRow(),column,event.rows,columns,result.value,result.oldValue,
      result.requestId,result.caseId,result.verdict];
    try {
      targets.log.appendRow(record); // One attempt. UNKNOWN never retries.
      spreadsheets.flush();
    } catch (_) { throw Error('GATE_B_LOG_UNKNOWN_STOP'); }
    return result.verdict;
  } finally { lock.releaseLock(); }
}

if (typeof module !== 'undefined') module.exports = {
  receiptGateBClassify_, receiptGateBObserve_, receiptGateBTargets_, RECEIPT_GATE_B_HEADERS
};
