/** Dedicated new-mode entry points. No legacy Drive/journal adapter or installer. */
function receiptConnectionConfigure() { return receiptSheetsConfigure(); }
function receiptConnectionStart() { return receiptSheetsStart(); }
function receiptConnectionStop() { return receiptSheetsStop(); }
function receiptConnectionEdited(e) { return receiptSheetsEdited(e); }
