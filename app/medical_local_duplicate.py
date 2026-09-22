"""Fresh, local duplicate/distinct resolution for verified medical bills.

The decision is an in-process capability tied to the complete accounting
snapshot. Saved JSON evidence cannot authorize a write. Both original files
are read again before writing; no existing expense is edited.
"""
from copy import deepcopy
from dataclasses import dataclass,field

from .medical_document_identity import compare_identities
from .medical_payment_units import compare_payments,payment_units
from .receipt_reimport import _date,_money
from .receipt_reimport_production import digest

_SEAL=object()
POLICY='medical-local-reconciliation-v1'


@dataclass(frozen=True)
class LocalReconciliation:
    source: dict
    parsed_digest: str
    tables_digest: str
    linked_expense_id: str
    evidence: tuple
    target_rows: dict
    _reader: object=field(repr=False,compare=False)
    _seal: object=field(repr=False,compare=False)

    def valid(self,source,parsed,tables,linked):
        return (self._seal is _SEAL and self.source==source and self.parsed_digest==digest(parsed.model_dump())
                and self.tables_digest==digest(tables) and self.linked_expense_id==linked)

    def fresh(self):
        for prior in self.evidence:
            current=self._reader(prior['source']['source_id'])
            if current!=prior:return False
        return True

    def audit(self):
        return {'policy':POLICY,'linked_expense_id':self.linked_expense_id,
                'operation':'link' if self.linked_expense_id else 'post_distinct',
                'tables_digest':self.tables_digest,'sources':[x['source'] for x in self.evidence],
                'document_refs':[x['identity']['document_ref'] for x in self.evidence],
                'target_rows':deepcopy(self.target_rows)}


def decide(source,parsed,provenance,tables,read_existing):
    identity=provenance.get('document_identity')
    if not identity or identity.get('source_sha256')!=source['sha256']:return None
    if identity.get('date')!=parsed.date or identity.get('amount')!=parsed.total:return None
    relevant=[m for m in compare_payments(parsed,tables,days=31,unknown_dates=True) if m.classification!='different']
    if not relevant:return None
    units=payment_units(tables);evidence=[];targets=[];target_rows={}
    for match in relevant:
        if match.classification!='candidate':return None
        selected=[u for u in units if tuple(r[0] for r in u.receipts)==match.receipt_ids
                  and tuple(r[0] for r in u.imports)==match.import_ids
                  and tuple(r[0] for r in u.expenses)==match.expense_ids]
        if len(selected)!=1:return None
        unit=selected[0]
        if not unit.verified_components or len(unit.receipts)!=1 or len(unit.imports)!=1:return None
        if len(unit.expenses)!=1:return None
        header,imported,expense=unit.receipts[0],unit.imports[0],unit.expenses[0]
        sid=imported[3]
        if (imported[2]!='receipt' or imported[0]!='receipt:'+sid or header[0]!='R-'+sid
                or expense[12]!='active' or expense[5]!='医療・保険'
                or expense[6] not in {'病院','薬','その他'}):return None
        record=read_existing(sid)
        if not record or record['source']['source_id']!=sid:return None
        other=record['identity']
        if (other.get('source_sha256')!=record['source']['sha256']
                or other.get('date')!=_date(header[1]) or other.get('amount')!=_money(header[3])):return None
        relation=compare_identities(identity,other)
        if relation=='unknown':return None
        evidence.append(deepcopy(record))
        for table,rows in [('receipt_rows',unit.receipts),('import_rows',unit.imports),('expense_rows',unit.expenses)]:
            for row in rows:
                original=[r for r in tables[table] if r[0]==row[0]]
                if len(original)!=1:return None
                target_rows.setdefault(table,{})[row[0]]=digest(original[0])
        if relation=='same':targets.append(expense[0])
    if len(targets)>1:return None
    return LocalReconciliation(deepcopy(source),digest(parsed.model_dump()),digest(tables),targets[0] if targets else '',
                          tuple(evidence),target_rows,read_existing,_SEAL)


def verify_saved_targets(item,tables):
    """Protect the canonical expense during readback, recovery and archiving."""
    from .drive_run_state import StateError
    record=item.get('local_decision',{}).get('reconciliation')
    if record is None:return
    if callable(tables):tables=tables()
    if record.get('policy')!=POLICY or not record.get('target_rows'):
        raise StateError('medical_duplicate_resolution_invalid')
    for table,expected in record['target_rows'].items():
        for identity,checksum in expected.items():
            rows=[r for r in tables.get(table,[]) if r and r[0]==identity]
            if len(rows)!=1 or digest(rows[0])!=checksum:
                raise StateError('medical_duplicate_target_changed')


def existing_reader(drive,download,processed_folder,document_key,review):
    """Read only ledger-selected processed originals; cache OCR, not freshness."""
    from hashlib import sha256
    from .receipt_local_ocr import enabled
    from .medical_candidate_preparation import prepare
    from .drive_receipts import is_supported_receipt_mime
    cache={}
    def read(sid):
        if not enabled() or not processed_folder:return None
        def metadata():
            return drive.files().get(fileId=sid,fields='id,parents,mimeType,version,trashed',
                                     supportsAllDrives=True).execute(num_retries=0)
        before=metadata()
        if (before.get('id')!=sid or before.get('trashed') or before.get('parents')!=[processed_folder]
                or not is_supported_receipt_mime(before.get('mimeType',''))):return None
        payload=download(sid)
        if metadata()!=before:return None
        source={'source_id':sid,'mime_type':before['mimeType'],'version':before['version'],'sha256':sha256(payload).hexdigest()}
        historical=[i['source']['sha256'] for i in review.items.values()
                    if i['source']['source_id']==sid and i['status']=='applied']
        if historical and set(historical)!={source['sha256']}:return None
        cache_key=(sid,source['sha256'])
        if cache_key not in cache:
            packet,pixels=prepare(source,payload,document_key,automatic=True,document_key=document_key)
            identity=packet.get('local_provenance',{}).get('document_identity')
            cache[cache_key]=identity if packet.get('status')=='local_ready' and pixels is None else None
        identity=cache[cache_key]
        return {'source':source,'identity':deepcopy(identity)} if identity is not None else None
    return read
