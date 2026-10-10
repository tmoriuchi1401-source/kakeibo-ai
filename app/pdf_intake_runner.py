"""Independent Page/Receipt Unit stages behind the existing receipt runner.

Intake has no AI credential. Analysis sends one fresh page through the reviewed
array parser. Posting uses the existing materializer ONLY after a durable intent
and complete plan/freshness gates. Ambiguous/partial writes are read-back only.
"""
from copy import deepcopy
from datetime import datetime, timezone
from hashlib import sha256
from uuid import uuid4

from .drive_run_state import StateError
from .pdf_intake_authority import AuthorityAdapter, digest, page_key, review_identity, binding
from .pdf_intake_registry import TERMINAL
from .page_receipt_model import PageUnit, SourceRef, stable_page
from .receipt_pdf_units import observe_pdf
from .receipt_plan.planning import PlanningDB, check_duplicates, table_rows
from .receipt_plan.models import ReceiptResult
from .pdf_review_fields import original_uri
from .receipt_item_review import prepare, fields, evaluate, card, check_snapshot
from .receipt_item_review_ui import read_snapshot


def observe_source(raw, source_id, store, *, observer=observe_pdf, reuse_complete=False):
    """Observe ALL pages before any allowed page reaches an external parser."""
    if reuse_complete:
        # Reuse only fully observed, unchanged bytes. The exact fresh PNG
        # still passes local privacy and source checks before each AI send.
        from .pdf_intake_production import page_count
        source = SourceRef(source_file_id=source_id,source_content_hash=sha256(raw).hexdigest(),page_count=page_count(raw))
        same = [r for r in store.load()['pages'].values() if r['page']['source']['source_file_id']==source_id]
        if any(r['page']['source'] != source.model_dump() for r in same):
            raise StateError('pdf_intake_source_changed')
        if (len(same)==source.page_count and {r['page']['page_number'] for r in same}==set(range(1,source.page_count+1))
                and all(r['page']['observation_complete'] for r in same)):
            return [page_key(r['page']) for r in sorted(same,key=lambda r:r['page']['page_number'])]
    observed = observer(raw, source_id)
    source = SourceRef(source_file_id=source_id,source_content_hash=sha256(raw).hexdigest(),page_count=len(observed.pages))
    if observed.source_content_hash != source.source_content_hash or not 2 <= source.page_count <= 50:
        raise StateError('pdf_intake_source_structure_invalid')
    existing = store.load()
    same = [r for r in existing['pages'].values() if r['page']['source']['source_file_id']==source_id]
    if any(r['page']['source'] != source.model_dump() for r in same):
        raise StateError('pdf_intake_source_changed')
    registered=[]
    for observation in observed.pages:
        old = next((r['page'] for r in same if r['page']['page_number']==observation.page_number),None)
        if old is not None:
            # Renderer/OCR jitter is not a new review or a permission migration.
            # Changed classification can only restrict; never silently upgrade.
            if (observation.classification in {'medical','payroll'} and observation.classification!=old['automatic_classification']
                    or observation.clearly_sensitive and not old['clearly_sensitive']):
                latest=store.load()
                if latest['pages'][page_key(old)]['status']!='privacy_observation_changed':
                    latest['pages'][page_key(old)]['status']='privacy_observation_changed'
                    latest['generation']+=1;store.save(latest)
            registered.append(page_key(old));continue
        page = PageUnit(source=source,page_number=observation.page_number,stable_page_identity=stable_page(source,observation.page_number),
            automatic_classification=observation.classification,automatic_reason=observation.reason_code,
            observation_complete=observation.observation_complete,extraction_status=observation.extraction_status,
            observation_render_hash=observation.page_hash,review_identity='0'*64,authority_revision=1,
            clearly_sensitive=observation.clearly_sensitive).model_dump()
        page['review_identity']=review_identity(page)
        registered.append(store.register(page))
    return registered


def ledger_source_id(unit_id):
    if not isinstance(unit_id,str) or not unit_id.startswith('page-receipt-v1:'):
        raise StateError('pdf_intake_unit_identity_invalid')
    return 'pdf-receipt-'+unit_id.split(':',1)[1]


def plan(result, unit_id, page, categories, prepared_at):
    from .receipt_pipeline import ReceiptPipeline
    db=PlanningDB(categories)
    ReceiptPipeline(db,None,clock=lambda:prepared_at)._materialize_result(
        ReceiptResult.model_validate(result),ledger_source_id(unit_id),
        original_uri(page['source']['source_file_id'],page['page_number']),[])
    operations=[{'table':title,'rows':[deepcopy(row) for table,row in db.plan if table==title]}
                for title in ('レシート','支出明細','取込データ')]
    if ([o['table'] for o in operations]!=['レシート','支出明細','取込データ']
            or len(operations[0]['rows'])!=1 or len(operations[2]['rows'])!=1
            or sum(row[4] for row in operations[1]['rows'])!=result['total']):
        raise StateError('pdf_intake_plan_invalid')
    return {'operations':operations,'digest':digest(operations),'total':result['total'],
            'receipt_count':1,'item_count':len(operations[1]['rows']),'import_count':1}


def ledger_snapshot(db):
    from .sheets import HEADERS
    values={};formulas={}
    for title,width in [('レシート','I'),('支出明細','M'),('取込データ','L')]:
        region=f"'{title}'!A1:{width}10000"
        values[title]=db.get_raw(region)
        if hasattr(db,'get_formula'):formulas[title]=db.get_formula(region)
        else:
            formulas[title]=db._execute_sheet_read(lambda:db.svc.spreadsheets().values().get(
                spreadsheetId=db.sid,range=region,valueRenderOption='FORMULA',dateTimeRenderOption='SERIAL_NUMBER')).get('values',[])
        if (not values[title] or values[title][0]!=HEADERS[title]
                or len(values[title])>=10000 or len(formulas[title])>=10000):
            raise StateError('pdf_intake_ledger_extent_or_schema_changed')
    return [values,formulas]


def exact_readback(db, expected, before_digest=None):
    """Not-written/complete/partial; no retries or automatic gap filling."""
    from .sheets import HEADERS
    snapshot=ledger_snapshot(db);clean=deepcopy(snapshot)
    matched=present=0
    for operation in expected['operations']:
        title=operation['table'];rows=snapshot[0][title][1:];by_id={}
        for row in rows:
            if row and row[0]:by_id.setdefault(str(row[0]),[]).append(row)
        for wanted in operation['rows']:
            actual=by_id.get(str(wanted[0]),[])
            if actual:present+=1
            width=len(HEADERS[title]);pad=lambda row:list(row)+['']*max(0,width-len(row))
            fmatches=[r for r in snapshot[1][title][1:] if r and str(r[0])==str(wanted[0])]
            if len(actual)==len(fmatches)==1 and pad(actual[0])==pad(wanted)==pad(fmatches[0]):matched+=1
            elif actual or fmatches:return 'partial_or_unknown'
            for part in clean:
                part[title]=[r for r in part[title] if not r or str(r[0])!=str(wanted[0])]
    total=sum(len(o['rows']) for o in expected['operations'])
    if before_digest is not None and digest(clean)!=before_digest:return 'partial_or_unknown'
    return 'complete' if matched==total else 'not_written' if present==0 else 'partial_or_unknown'


class ScopedWriter(PlanningDB):
    """Existing materializer, exact append plan, no update/delete/schema API."""
    def __init__(self,db,categories,expected,before_digest):
        super().__init__(categories)
        self.real,self.expected,self.before_digest=db,expected,before_digest
        self.offset=0
    def append(self,title,rows):
        operation={'table':title,'rows':rows}
        if self.offset>=len(self.expected['operations']) or operation!=self.expected['operations'][self.offset]:
            raise StateError('pdf_intake_writer_scope_violation')
        prefix={'operations':self.expected['operations'][:self.offset]}
        if self.offset:
            if exact_readback(self.real,prefix,self.before_digest)!='complete':raise StateError('pdf_intake_prefix_readback_required')
        elif digest(ledger_snapshot(self.real))!=self.before_digest:
            raise StateError('pdf_intake_existing_ledger_changed')
        self.real.append_raw(title,rows)
        self.offset+=1
        self.plan.extend((title,deepcopy(row)) for row in rows)


class PageIntake:
    def __init__(self,store,source,db,proof_client,analyzer_factory,*,clock=None,unit_limit=3):
        self.store,self.source,self.db,self.proof_client,self.analyzer_factory=store,source,db,proof_client,analyzer_factory
        self.clock=clock or (lambda:datetime.now(timezone.utc).isoformat(timespec='seconds'))
        if type(unit_limit) is not int or not 1<=unit_limit<=10:raise StateError('pdf_intake_unit_limit_invalid')
        self.unit_limit,self.written=unit_limit,0

    def current_page(self,sid,number):
        records=list(self.store.load()['pages'].values())
        if any(r['page']['source']['source_file_id']==sid and r['page']['page_number']==number and r['status']=='privacy_observation_changed' for r in records):
            raise StateError('pdf_intake_privacy_observation_changed')
        matches=[r['page'] for r in records
                 if r['page']['source']['source_file_id']==sid and r['page']['page_number']==number]
        if len(matches)!=1:raise StateError('pdf_intake_page_missing_or_ambiguous')
        return matches[0]

    def load_proof(self,key,operation,unit):
        record=self.store.load()['pages'].get(key)
        if record is None:return None
        if operation=='single_page_ai':
            saved=record['authority'].get(operation)
            return None if saved is None else saved['proof']
        saved=record['units'].get(unit['receipt_unit_id'])
        return None if saved is None else saved['posting_authority']

    def gate(self):
        return AuthorityAdapter(self.current_page,self.source,self.load_proof,self.proof_client.verify)

    def grant(self,page):
        self.gate().authorize(page.model_dump())
        return self.store.load()['pages'][page_key(page.model_dump())]['authority']['single_page_ai']['state']['grants'][page_key(page.model_dump())]

    def analyze(self,key):
        from .page_receipt_readonly import ReadonlyPageReceipts
        from .page_receipt_ai import authorize_payload
        record=self.store.load()['pages'][key];page=record['page']
        if record['status'] in TERMINAL:return {'status':'terminal','units':len(record['units'])}
        if (record['status']=='privacy_observation_changed' or page['automatic_classification'] in {'medical','payroll'} or page['clearly_sensitive']
                or not page['observation_complete']):
            return {'status':'medical_manual_pending' if page['automatic_classification']=='medical' else 'privacy_held','units':0}
        self.gate().authorize(page)
        if record['analysis']:
            # Keep approved human values and frozen identities; no silent reanalysis.
            return {'status':record['status'],'units':len(record['units'])}
        def permission(p,png):
            self.gate().authorize(p.model_dump())
            return authorize_payload(p,png,current_page=lambda sid,n:PageUnit.model_validate(self.current_page(sid,n)),
                                     load_source=self.source,load_grant=self.grant)
        analyzer=self.analyzer_factory(permission)
        runner=ReadonlyPageReceipts(lambda sid,n:PageUnit.model_validate(self.current_page(sid,n)),self.source,
                                   self.grant,analyzer,self.db.categories(),completion_drafts=True)
        report=runner.run(page['source']['source_file_id'],page['page_number'])
        if report['status'] not in {'would_import','would_need_review'}:
            return {'status':report['status'],'units':0}
        latest=self.store.load()
        if latest['pages'][key]!=record:raise StateError('pdf_intake_page_changed_during_analysis')
        candidates={}
        for draft,item_record in zip(report['completion_drafts'],report['item_records']):
            uid=draft['identity']['receipt_unit_id']
            candidates[uid]={'status':'needs_review' if draft['hard_issues'] else 'needs_human_completion',
                'candidate':{'record':item_record,'view':card(item_record,categories=self.db.categories()),
                             'manifest':report['manifest'],'prepared_at':self.clock()},
                'posting_authority':None,'intent':None,'readback':None}
        latest['pages'][key]['units']=candidates
        latest['pages'][key]['analysis']={'manifest':report['manifest'],'payload_hash':report['payload_sha256'],
                                        'analyzed_at':self.clock()}
        latest['pages'][key]['status']='units_observed';latest['generation']+=1
        self.store.save(latest)
        return {'status':'units_observed','units':len(candidates)}

    def post_normal(self,key,uid,*,apply=False):
        """Human-unknown posting requires a separately sealed posting decision.

        This path only auto-posts automatically normal, fully corroborated
        untouched candidates. Partial UI input cannot expand auto authority.
        """
        record=self.store.load()['pages'][key];page=record['page'];unit=record['units'][uid]
        if unit['intent'] is not None:
            return self.recover(key,uid,record,apply=apply)
        if unit['status'] in TERMINAL:return {'status':'terminal','write':0}
        if page['automatic_classification']!='normal':return {'status':'posting_authority_required','write':0}
        if record['status']=='privacy_observation_changed':return {'status':'privacy_held','write':0}
        self.gate().authorize(page)
        if unit['candidate'].get('ui_projected'):return {'status':'posting_authority_required','write':0}
        candidate=unit['candidate'];current=fields(candidate['record']);categories=self.db.categories()
        validation=evaluate(candidate['record'],current,categories)
        if validation['status']!='ready_to_confirm':return {'status':validation['status'],'write':0}
        expected=plan(validation['parsed'],uid,page,self.db.categories(),candidate['prepared_at'])
        if plan(validation['parsed'],uid,page,self.db.categories(),candidate['prepared_at'])!=expected:
            raise StateError('pdf_intake_plan_replay_changed')
        return self._post(key,uid,record,validation,categories,expected,apply=apply)

    def _post(self,key,uid,record,validation,categories,expected,*,apply,authorized=None):
        page=record['page'];unit=record['units'][uid];candidate=unit['candidate']
        check_duplicates(self.db,ledger_source_id(uid),ReceiptResult.model_validate(validation['parsed']))
        if not apply:return {'status':'ready_to_write','write':0,'plan':expected}
        if self.written>=self.unit_limit:return {'status':'unit_limit','write':0}
        # Fresh page, source, master and ledger after plan; claim exact plan once.
        self.gate().authorize(page)
        if authorized is not None:
            self.gate().authorize(page,'receipt_posting',**authorized)
        latest=self.store.load()
        if latest['pages'][key]!=record:raise StateError('pdf_intake_candidate_stale')
        if self.db.categories()!=categories:raise StateError('pdf_intake_category_master_changed')
        check_duplicates(self.db,ledger_source_id(uid),ReceiptResult.model_validate(validation['parsed']))
        before_digest=digest(ledger_snapshot(self.db))
        intent={'request_id':str(uuid4()),'plan':expected,'candidate_digest':candidate['record']['digest'],
                'source_hash':page['source']['source_content_hash'],'status':'claimed','claimed_at':self.clock(),
                'posting_binding':authorized}
        intent['before_ledger_digest']=before_digest
        latest['pages'][key]['units'][uid]['intent']=intent;latest['generation']+=1
        self.store.save(latest);self.written+=1
        from .receipt_pipeline import ReceiptPipeline
        try:
            scoped=ScopedWriter(self.db,categories,expected,before_digest)
            ReceiptPipeline(scoped,None,clock=lambda:candidate['prepared_at'])._materialize_result(
                ReceiptResult.model_validate(validation['parsed']),ledger_source_id(uid),
                original_uri(page['source']['source_file_id'],page['page_number']),[])
        except Exception:
            # A lost response is not proof that nothing was written.
            return {'status':exact_readback(self.db,expected,before_digest),'write_outcome':'unknown','retry_allowed':False}
        actual=exact_readback(self.db,expected,before_digest)
        if actual!='complete':return {'status':actual,'write_outcome':'unknown','retry_allowed':False}
        latest=self.store.load();saved=latest['pages'][key]['units'][uid]
        if saved['intent']!=intent:raise StateError('pdf_intake_posting_intent_changed')
        saved['status']='imported';saved['intent']['status']='complete'
        saved['readback']={'plan_digest':expected['digest'],'receipt_id':'R-'+ledger_source_id(uid),
                           'import_id':'receipt:'+ledger_source_id(uid),'verified_at':self.clock()}
        latest['generation']+=1;self.store.save(latest)
        return {'status':'imported','write':1,'items':expected['item_count'],'total':expected['total']}

    def prepare_manual(self,key,uid,snapshot):
        """Request input only. A simulated shape proof never enables posting."""
        from .receipt_item_confirmation import ConfirmedItems
        record=self.store.load()['pages'][key];unit=record['units'][uid];candidate=unit['candidate']
        if unit['intent'] is not None or unit['status'] in TERMINAL:return {'status':'terminal_or_claimed'}
        self.gate().authorize(record['page'])
        categories=self.db.categories();current=check_snapshot(snapshot,card(candidate['record'],categories=categories))
        if 'review_evidence' in candidate['record'] and current.get('structure_confirmation')!='確認済み':return {'status':'explicit_structure_confirmation_required'}
        if current['action']!='記帳する':return {'status':'held' if current['action']=='保留する' else 'awaiting_selection'}
        pseudo=ConfirmedItems(str(uuid4()),candidate['record']['digest'],digest(snapshot),digest(current),
                              digest(candidate['record']['legacy']['identity']),'0'*64,0,'0'*64)
        validation=evaluate(candidate['record'],current,categories,confirmation=pseudo)
        if validation['status']!='ready_to_confirm':return {'status':validation['status']}
        expected=plan(validation['parsed'],uid,record['page'],categories,candidate['prepared_at'])
        check_duplicates(self.db,ledger_source_id(uid),ReceiptResult.model_validate(validation['parsed']))
        prepared={'snapshot':deepcopy(snapshot),'parsed':validation['parsed'],'plan_digest':expected['digest'],
            'unit_binding':{'receipt_unit_id':uid,'segmentation_digest':candidate['manifest']['segmentation_digest'],
                            'item_identities':[i['item_id'] for i in candidate['record']['items']]}}
        if candidate.get('posting')==prepared:return {'status':'awaiting_authenticated_confirmation'}
        if unit['posting_authority'] is not None:raise StateError('pdf_intake_confirmed_snapshot_changed')
        latest=self.store.load()
        if latest['pages'][key]!=record:raise StateError('pdf_intake_candidate_stale')
        candidate['posting']=prepared
        latest['pages'][key]['units'][uid]['candidate']=candidate;latest['generation']+=1
        self.store.save(latest)
        return {'status':'awaiting_authenticated_confirmation'}

    def post_manual(self,key,uid,snapshot,*,apply=False):
        from .receipt_item_confirmation import ConfirmedItems
        record=self.store.load()['pages'][key];unit=record['units'][uid];candidate=unit['candidate']
        if unit['intent'] is not None:
            return self.recover(key,uid,record,apply=apply)
        prepared=candidate.get('posting');sealed=unit['posting_authority']
        if not prepared or not sealed:return {'status':'posting_authority_required','write':0}
        if prepared['snapshot']!=snapshot:raise StateError('pdf_intake_confirmed_snapshot_changed')
        page=record['page'];self.gate().authorize(page)
        bound={'unit':prepared['unit_binding'],'snapshot_digest':digest(snapshot),'plan_digest':prepared['plan_digest']}
        verified=self.gate().authorize(page,'receipt_posting',**bound)
        journal=candidate['confirmation_journal'];rid=verified['request_id']
        confirmation=journal['requests'].get(rid)
        if not confirmation:raise StateError('pdf_intake_confirmation_request_missing')
        proof=ConfirmedItems(**confirmation['proof'])
        if proof.actor_id!=verified['actor_id'] or proof.snapshot_digest!=digest(snapshot):
            raise StateError('pdf_intake_confirmation_actor_mismatch')
        current=check_snapshot(snapshot,card(candidate['record'],categories=self.db.categories()))
        if current['action']!='記帳する':raise StateError('pdf_intake_operation_not_posting')
        validation=evaluate(candidate['record'],current,self.db.categories(),confirmation=proof)
        if validation['status']!='ready_to_confirm' or validation['parsed']!=prepared['parsed']:
            raise StateError('pdf_intake_confirmation_plan_changed')
        expected=plan(validation['parsed'],uid,page,self.db.categories(),candidate['prepared_at'])
        if expected['digest']!=prepared['plan_digest']:raise StateError('pdf_intake_plan_digest_changed')
        return self._post(key,uid,record,validation,self.db.categories(),expected,apply=apply,authorized=bound)

    def recover(self,key,uid,record,*,apply):
        """Resolve a lost response by exact read-back; never repeat an append."""
        unit=record['units'][uid];intent=unit['intent'];page=record['page']
        old=None if intent['status']=='complete' else intent['before_ledger_digest']
        actual=exact_readback(self.db,intent['plan'],old)
        if actual=='complete' and intent['status']=='claimed' and apply:
            self.gate().authorize(page)
            if intent['posting_binding'] is not None:
                self.gate().authorize(page,'receipt_posting',**intent['posting_binding'])
            latest=self.store.load()
            if latest['pages'][key]!=record:raise StateError('pdf_intake_recovery_state_changed')
            saved=latest['pages'][key]['units'][uid]
            saved['status']='imported';saved['intent']['status']='complete'
            saved['readback']={'plan_digest':intent['plan']['digest'],'receipt_id':'R-'+ledger_source_id(uid),
                'import_id':'receipt:'+ledger_source_id(uid),'verified_at':self.clock()}
            latest['generation']+=1;self.store.save(latest)
        return {'status':actual,'write':0,'replay':True,'retry_allowed':False}
