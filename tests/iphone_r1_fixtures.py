"""New integrated entrance; old #131 fixture and v3 writer fixture stay intact."""
from copy import deepcopy
from types import SimpleNamespace

from cryptography.fernet import Fernet

from app.iphone_item_drafts import image_issue
from app.receipt_confirmation_contract import load, encoded
from app.receipt_confirmation import review_id
from app.unified_receipt_review import digest
from app.unified_receipt_shadow import ShadowConfig
from app.iphone_confirmation_host import make_isolated_confirmation_application, PREFIX
from staging_shadow.iphone_confirmation_ui import OneTabConfirmation
from draft_runtime_fixtures import harness, set_value, RAW
from accounting_gate_fixtures import reader_for
from iphone_confirmation_fixtures import Rig as OldRig


class Rig:
    def __init__(self, monkeypatch, tmp_path):
        self.old = OldRig(monkeypatch, tmp_path)
        self.h = self.old.h
        self.native = {}
        for kind in ('normal','medical'):
            # Provision distinct originals before taking any baseline. #145's
            # two alternative fixtures otherwise deliberately use the SAME ID.
            import iphone_draft_request_design_examples as design
            original_fixture=design.native_fixture
            def provision(k):
                service,store,db,metadata,key=original_fixture(k)
                old=service.items[key]['source']['source_id'];new=old+'-'+k
                def renamed(v):
                    if isinstance(v,str):return v.replace(old,new)
                    if isinstance(v,list):return [renamed(x) for x in v]
                    if isinstance(v,dict):return {renamed(a):renamed(b) for a,b in v.items()}
                    return v
                store.value=renamed(store.value);db.rows=renamed(db.rows);metadata['id']=new
                item=store.value['confirmation_items'].pop(key);newkey=review_id(k,item['source'])
                store.value['confirmation_items'][newkey]=item
                db.rows={t:[[newkey if x==key else x for x in row] for row in rows] for t,rows in db.rows.items()}
                return service,store,db,metadata,newkey
            with monkeypatch.context() as provisioning:
                provisioning.setattr(design,'native_fixture',provision)
                h = harness(kind, monkeypatch)
            # Host identity is the real existing synthetic Google actor.
            d = load(h.ports['draft'].read_versioned()[0])
            d['owner'] = self.old.service.owner
            value = image_issue(h.store.value, kind, h.f.sid)
            if kind == 'medical': value['fields'] = {k:'' for k in value['fields']}
            d.update(value=value, input_digest=digest(value))
            set_value(h.ports['draft'],d)
            def ownership(inputs, plan, h=h):
                source=inputs['source_binding']['source']
                h.own=reader_for('legacy_confirmation',source,RAW,[],dict(namespace='receipt_confirmation',scope='whole',
                    unit_id=None,receipt_index=None,input_snapshot=digest(inputs),authority_revision=7,
                    ledger_targets=[r[0] for _,r in plan],case_id=h.f.case_id,owner=self.old.service.owner))
                return h.own.reader.observe('legacy_confirmation','u1').value()
            self.native[h.f.case_id] = SimpleNamespace(store=h.store, shared=h.shared, readers=h.readers,
                kind=kind, source_id=h.f.sid, prepared_at='2026-10-19 00:00:00', ownership=ownership,
                source=deepcopy(h.c['source']), authority_revision=7, review_id=h.f.key, rig=h)
        general,medical=self.native.values()
        common=deepcopy(load(general.readers.ledger.read_versioned()[0]))
        other=load(medical.readers.ledger.read_versioned()[0])
        for projection in (0,1):
            for table,rows in other['snapshot'][projection].items():
                for row in rows:
                    if not any(existing[0]==row[0] for existing in common['snapshot'][projection][table]):
                        common['snapshot'][projection][table].append(deepcopy(row))
        set_value(general.readers.ledger,common);general.readers.ledger.writes=0
        medical.readers.ledger=general.readers.ledger
        medical.readers.categories=general.readers.categories
        medical.shared=general.shared
        self.key=Fernet.generate_key()
        self.ui=self.rebuild()
        # Surface synthetic-only exception stacks in CI without exposing input.
        original_post=self.ui.post
        def audited_post(*args,**kwargs):
            try:return original_post(*args,**kwargs)
            except Exception:
                import traceback
                traceback.print_exc()
                raise
        self.ui.post=audited_post
        self.h.app=make_isolated_confirmation_application(self.h.web.make_application,self.h.configured,self.old.service,
            config=ShadowConfig(True,'isolated_synthetic'),
            cookies={'login':self.h.web.LOGIN_COOKIE,'ticket':self.h.web.COOKIE},one_tab=self.ui)
        self.h.app.testing=True
        self.h.client=self.h.app.test_client()

    def rebuild(self):
        held={cid:dict(reason=reason,inputs=[]) for cid,reason in {'SYN-G103':'既存入力とメモを保持',
            'SYN-H301':'LEGACY_UNMAPPED_HOLD', 'OLD-MEDICAL-P1':'対応不明の旧医療p1',
            'SYN-P201':'PDF構造確認のみ・金融接続対象外','SYN-P202':'PDF保留'}.items()}
        cards=self.old.service.read()[0]
        for cid,v in held.items():
            if cid in cards:v['inputs']=deepcopy(cards[cid]['display']['inputs'])
        return OneTabConfirmation(self.native, held, self.key,
            owner=self.old.service.owner,clock=lambda:self.h.now,enabled=True,isolated_synthetic=True)

    def proof(self,cid,action):
        r=self.h.get(PREFIX+'/r1/'+cid+'/'+action)
        assert r.status_code==200,r.get_data(as_text=True)
        return r.json

    def post(self,p,value,confirmed=False,**headers):
        from unified_shadow_fixtures import ORIGIN
        return self.h.client.post(PREFIX+'/r1/request',base_url=ORIGIN,
            json=dict(proof=p['proof'],value=value,confirmed=confirmed),
            headers={'Origin':ORIGIN,'X-CSRF-Token':p['csrf'],**headers})

    def save(self,cid):
        p=self.proof(cid,'save_draft'); c=self.native[cid]
        value=dict(fields=deepcopy(c.rig.f.fields),items=deepcopy(c.rig.f.items),provenance='human-entered')
        r=self.post(p,value);assert r.status_code==200,r.get_data(as_text=True)
        return value

    def frozen(self,cid):
        c=self.native[cid]
        return [p.read_versioned() for p in (c.store.io,c.shared.io,c.readers.ledger,c.readers.authority)]
