"""Opt-in single-line ledger draft -> independent PR-B review. No AI/writer."""
from copy import deepcopy
from datetime import date
from hashlib import sha256
import json
import math
import re
import sqlite3
from urllib.parse import urlsplit

from .page_receipt_model import digest
from .receipt_review_cases import make_case, make_row, source_identity
from .receipt_review_diagnostics import DiagnosticFailure
from .receipt_review_worker import AttemptFence


class ReuseUnknown(RuntimeError):
    """Preserve the fence and inspect read-only; never automatically resend."""


class ReuseFence(AttemptFence):
    def consume_copy(self, original_id, copy_id, hashed):
        # One atomic commit retains original aliases AND copy/content identity.
        with sqlite3.connect(self.path) as db:
            for key in ('source:'+original_id, 'source:'+copy_id, 'hash:'+hashed):
                db.execute('INSERT INTO attempts VALUES (?)', (key,))


def build_reused_case(manifest, *, clock):
    expected={'schema','original','copy','records','owner_confirmation','categories'}
    if type(manifest) is not dict or set(manifest)!=expected or manifest['schema']!='receipt-review-reuse-v1':
        raise ValueError('reuse_manifest_invalid')
    original,copy=manifest['original'],manifest['copy']
    for source in (original,copy):
        source_identity(source)
        if (source['mime_type']!='image/png' or source['classification']!='normal'
                or source['transaction_kind']!='purchase'):
            raise ValueError('reuse_general_png_only')
    if original['source_id']==copy['source_id'] or original['sha256']!=copy['sha256']:
        raise ValueError('reuse_copy_lineage_invalid')
    owner=manifest['owner_confirmation']
    flags={'image_matches_records','distinct_from_unknown','categories_confirmed'}
    if type(owner) is not dict or set(owner)!=flags|{'item_count','quantity','tax_treatment'}:
        raise ValueError('reuse_owner_confirmation_required')
    if any(owner[k] is not True for k in flags):raise ValueError('reuse_owner_confirmation_required')
    if type(owner['item_count']) is not int or owner['item_count']!=1:
        raise ValueError('reuse_single_item_only')
    quantity=owner['quantity']
    if (type(quantity) not in (int,float) or not math.isfinite(quantity) or quantity<=0
            or quantity>1_000_000 or round(quantity,3)!=quantity):
        raise ValueError('reuse_explicit_quantity_required')
    # The one-line projection has no independent external-tax allocation evidence.
    if owner['tax_treatment']!='税込':raise ValueError('reuse_included_tax_confirmation_required')
    records=manifest['records']
    if type(records) is not dict or set(records)!={'import_row','receipt_row','expense_rows'}:
        raise ValueError('reuse_record_schema_invalid')
    old,receipt,expenses=records['import_row'],records['receipt_row'],records['expense_rows']
    sid=original['source_id'];iid='receipt:'+sid;rid='R-'+sid
    if (type(old) is not list or len(old)!=12 or old[0]!=iid or old[2:4]!=['receipt',sid]
            or old[8]!='解析済' or not re.fullmatch('[a-f0-9]{64}',str(old[10]))
            or not re.fullmatch(r'\d{4}-\d{2}-\d{2}',str(old[4]))
            or not isinstance(old[5],str) or not old[5].strip()
            or type(old[6]) is not int or not 0<old[6]<=99_999_999):
        raise ValueError('reuse_import_binding_invalid')
    date.fromisoformat(old[4])
    if (type(receipt) is not list or len(receipt)!=9 or receipt[0]!=rid
            or receipt[1:5]!=[old[4],old[5],old[6],old[7]] or receipt[6]!='解析済'):
        raise ValueError('reuse_receipt_binding_invalid')
    link=urlsplit(str(receipt[5]))
    if (link.scheme!='https' or link.hostname!='drive.google.com' or link.username or link.password
            or link.path not in {'/file/d/'+sid+'/view','/file/d/'+sid}):
        raise ValueError('reuse_original_link_invalid')
    if type(expenses) is not list or len(expenses)!=1:raise ValueError('reuse_single_item_only')
    item=expenses[0]
    if (type(item) is not list or len(item)!=13 or item[1:3]!=old[4:6]
            or type(item[4]) is not int or item[4]!=old[6] or item[7]!=old[7]
            or item[8:11]!=['receipt',rid,iid] or not isinstance(item[3],str) or not item[3].strip()):
        raise ValueError('reuse_expense_binding_invalid')
    categories=manifest['categories']
    if (type(categories) is not list or not categories or any(type(c) is not list or len(c)!=2
            or any(not isinstance(v,str) or not v.strip() for v in c) for c in categories)
            or len({tuple(c) for c in categories})!=len(categories) or item[5:7] not in categories):
        raise ValueError('reuse_categories_invalid')
    provenance={'schema':'receipt-reuse-origin-v1','original':deepcopy(original),
        'source_import_id':iid,'source_receipt_id':rid,'original_analysis_hash':old[10],
        'records_digest':digest(records),'owner_confirmation_digest':digest(owner),
        'evidence_kind':'owner_confirmed_ledger_projection_not_original_ai_response'}
    # New draft digest; the old analysis hash authenticates neither this draft nor bytes.
    evidence=digest({'provenance':provenance,'copy':copy,'quantity':quantity,'tax':'税込'})
    cid='receipt:'+copy['source_id']
    imported=[cid,clock(),'receipt',copy['source_id'],old[4],old[5],old[6],old[7],
        '要確認','',evidence,json.dumps(provenance,sort_keys=True,separators=(',',':'))]
    candidates=[[cid+':001',cid,evidence,1,item[3],quantity,item[4],item[5],item[6],
        '既存明細由来・原本確認が必要',old[6]]]
    entry=make_case(copy,imported,candidates,categories)
    row=make_row(entry)
    row[7]='既存解析記録からの独立試験用下書き。原本・数量・税を再確認'
    row[-2]=owner['tax_treatment']
    # Item/total confirmations remain FALSE until explicit iPhone review.
    return entry,row


def intake_reused(manifest, *, original_bytes, copy_bytes, original_check, port,
                  fence, diagnostics, clock):
    """Single invocation; no Gemini construction, production lookup or fallback."""
    stage='candidate';consumed=False
    try:
        diagnostics.event(stage,'STARTED')
        entry,row=build_reused_case(manifest,clock=clock)
        original=manifest['original'];copy=entry['source']
        if (sha256(original_bytes).hexdigest()!=original['sha256']
                or sha256(copy_bytes).hexdigest()!=copy['sha256']):
            raise ValueError('reuse_bytes_mismatch')
        stage='privacy';diagnostics.event(stage,'STARTED')
        from .receipt_privacy_gate import evaluate_receipt_privacy
        for raw in (original_bytes,copy_bytes):
            gate=evaluate_receipt_privacy(raw,'image/png')
            if gate.classification!='normal' or not gate.gemini_allowed or gate.buyback_evidence:
                raise ValueError('reuse_privacy_blocked')
        stage='fence';diagnostics.event(stage,'STARTED')
        fence.consume_copy(original['source_id'],copy['source_id'],copy['sha256']);consumed=True
        diagnostics.event(stage,'PASS')
        stage='lookup';diagnostics.event(stage,'STARTED')
        if port.lookup_source(copy['source_id'],copy['sha256']) is not None:
            raise ValueError('reuse_independent_duplicate')
        stage='source_recheck';diagnostics.event(stage,'STARTED')
        if original_check(original) is not True:raise ValueError('reuse_original_identity_unknown')
        port.source_check(copy)
        if original_check(original) is not True:raise ValueError('reuse_original_identity_unknown')
        stage='intake';diagnostics.intake_attempted=True;diagnostics.event(stage,'STARTED')
        result=port.intake(entry,row)
        if result!={'case_id':entry['case_id']}:raise ValueError('reuse_intake_unknown')
        if port.lookup_source(copy['source_id'],copy['sha256'])!=entry:
            raise ValueError('reuse_intake_readback_unknown')
        diagnostics.event('complete','PASS')
        return {'state':'編集中・未登録','case_id':entry['case_id'],'request_id':entry['request_id'],
            'ai_http_attempt_count':0,'financial_write_count':0,'posting_authority':False}
    except Exception as error:
        if not isinstance(error,DiagnosticFailure):
            diagnostics.event(stage,'UNKNOWN' if consumed else 'FAIL',error=error,category='LOCAL_ERROR')
        if consumed:raise ReuseUnknown() from None
        raise
