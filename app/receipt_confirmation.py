"""Durable, human-confirmed receipt review; no OCR or AI in this module."""
from copy import deepcopy
from datetime import date
from hashlib import sha256

from .drive_run_state import StateError
from .models import ReceiptResult, ReceiptItem
from .receipt_reimport import accounting_equal_keeping_labels, _date, _money
from .receipt_reimport_production import TABLES, target_snapshot, digest
from .utils import now_jst_string, canonical_hash

TITLE = "領収書確認"
SUPERSEDED_REVIEW_REASON = '同じ原本の新しい確認行あり。この旧行への追加判断は不要。'
HEADERS = ["確認ID", "種別", "状態", "原本リンク", "確認する内容", "既存値", "候補（未確定）",
           "支払日（医療）", "発行施設（医療）", "実支払額（医療）", "カテゴリ（医療）",
           "支払方法（医療・任意）", "判断", "統合先支出ID（重複時）", "本人メモ", "反映結果"]
CHOICES = ["保留", "既存値を維持", "候補明細で確定", "医療費を確定", "候補で医療費を確定", "既存支出と重複（紐付け）", "重複候補と別の支出として確定"]
INPUT_START, INPUT_END = 7, 15


def medical_categories(categories):
    return [tuple(c) for c in categories if c[0]=='医療・保険' and c[1] in {'病院','薬','その他'}]


def review_id(kind, source):
    return "review-" + digest([kind, source["source_id"], source["version"], source["sha256"]])[:24]


def same_row(title, left, right):
    sizes={"レシート":9,"取込データ":12,"支出明細":13}
    def normalized(row):
        row=list(row)+[""]*max(0,sizes[title]-len(row))
        d,m={"レシート":(1,3),"取込データ":(4,6),"支出明細":(1,4)}[title]
        row[d]=_date(row[d]);row[m]=_money(row[m])
        return [str(x) if x is not None else "" for x in row]
    return normalized(left)==normalized(right)


def _rows(db, title):
    return db.get(f"'{title}'!A2:T")


class ReceiptConfirmation:
    def __init__(self, store, db, verify_source):
        self.store,self.db,self.verify_source=store,db,verify_source
        for key,item in self.items.items():
            if (item.get('kind') not in {'normal','medical','intake'} or key!=review_id(item['kind'],item['source'])
                    or item.get('status') not in {'waiting','pending','applied','closed_machine','closed_user','superseded'}
                    or len(item.get('inputs',[]))!=8):
                raise StateError('confirmation_store_invalid')

    @property
    def items(self): return self.store.value.get("confirmation_items",{})

    def save_item(self, key, item):
        value=deepcopy(self.store.value)
        value.setdefault("confirmation_items",{})[key]=item
        self.store.save(value)

    def tables(self): return {key:_rows(self.db,title) for key,title in TABLES.items()}

    def prepare_general(self):
        tables=self.tables();categories=self.db.categories()
        for source in self.store.value['manifest']['sources']:
            sid=source['source_id'];record=self.store.value['records'][sid]
            if record['phase']!='complete':continue
            key=review_id('normal',source)
            if key in self.items:continue
            self.verify_source(source,self.store.value['manifest']['folder_id'])
            parsed=ReceiptResult.model_validate(record['parsed'])
            before=target_snapshot(tables,sid)
            if digest(before)!=digest(record['before']):
                disposition='waiting';reason='既存行が比較後に変更。本人判断を保護して再確認'
            elif accounting_equal_keeping_labels(sid,parsed,**tables,categories=categories):
                disposition='closed_machine';reason='同じ固定原本。会計内容一致・既存表記と照合を維持（機械判断）'
            else:
                disposition='waiting';reason='明細未計上：原本と候補明細を優先確認' if not before['expense_rows'] else '商品名・カテゴリ等に差分。既存値維持または候補を確認'
            self.save_item(key,dict(kind='normal',source=source,folder_id=self.store.value['manifest']['folder_id'],
                status=disposition,reason=reason,before=before,inputs=['']*8))

    def observe_medical(self,source,folder_id):
        key=review_id('medical',source)
        if key in self.items:return False
        for old_key,old in list(self.items.items()):
            if old['kind']=='medical' and old['source']['source_id']==source['source_id']:
                if old['status']=='pending':raise StateError('confirmation_pending_source_changed')
                if old['status'] in {'applied','closed_user','closed_machine'}:continue
                changed=deepcopy(old);changed['status']='superseded';changed['reason']='原本の版が変更。新しい行で再確認'
                self.save_item(old_key,changed)
        self.save_item(key,dict(kind='medical',source=source,folder_id=folder_id,status='waiting',
            reason='原本の支払日・発行施設・本人の実支払額を入力。候補は未確定',inputs=['']*8))
        return True

    def resolve_general_without_writes(self):
        """Close redundant questions only; never change receipt/import/expense rows."""
        from .receipt_review_policy import manual_expense_already_recorded
        tables=self.tables();categories=self.db.categories();closed=0
        for key,old in list(self.items.items()):
            if old['kind']!='normal' or old['status']!='waiting' or any(old['inputs']):continue
            if old.get('error') or old.get('require_reconfirm'):continue
            try:self.verify_source(old['source'],old['folder_id'])
            except StateError as error:
                if str(error)!='confirmation_source_changed':raise
                continue  # Changed originals keep their question; never close from stale evidence.
            sid=old['source']['source_id'];parsed=self._parsed(old)
            if manual_expense_already_recorded(sid,parsed,**tables,categories=categories):
                reason='既存の本人判断による手動計上を維持。候補明細は追加しません。'
            elif digest(target_snapshot(tables,sid))==digest(old['before']) and accounting_equal_keeping_labels(sid,parsed,**tables,categories=categories):
                reason='同一原本・日付・金額・明細・分類が一致。店舗表記と商品名の空白差は既存値を維持。'
            else:continue
            # Only rows eligible for closure need a fresh UI comparison.
            # Unchanged held differences must not each scan the whole UI.
            live=self.ui_rows().get(key)
            if live and (any(live[1][7:15]) or live[1][:2]+live[1][3:7]!=old.get('presentation')):continue
            # Re-read before closing in case the owner began typing during reads.
            latest=self.ui_rows().get(key)
            if latest!=live:continue
            item=deepcopy(old);item.update(status='closed_machine',reason=reason)
            self.save_item(key,item);closed+=1
        return closed

    def observe_intake_hold(self,source,folder_id,gate):
        """Record only safe gate metadata; no OCR text, AI candidate or write authority."""
        key=review_id('intake',source)
        if key in self.items:return False
        for old_key,old in list(self.items.items()):
            if old['kind']=='intake' and old['source']['source_id']==source['source_id'] and old['status']=='waiting':
                item=deepcopy(old);item.update(status='superseded',reason='原本の版が変更。新しい受付行を確認。')
                self.save_item(old_key,item)
        self.save_item(key,dict(kind='intake',source=source,folder_id=folder_id,status='waiting',
            reason='種類を判定できずAI未送信。原本を見て、本人メモに「一般の買物／医療／対象外」のいずれかを記入。読めない画像だけ再撮影。',
            gate={'classification':gate.classification,'reason':getattr(gate,'reason_code','insufficient_evidence'),
                  'extraction':getattr(gate,'extraction_status','unknown')},inputs=['']*8))
        return True

    def finish_intake_scan(self,blocked_sources):
        for key,old in list(self.items.items()):
            if old['kind']=='intake' and old['status']=='waiting' and old['source']['source_id'] not in blocked_sources:
                if any(old['inputs']):continue
                item=deepcopy(old);item.update(status='closed_machine',reason='受付保留の対象外になりました。原本・記帳は変更していません。')
                self.save_item(key,item)

    def resolve_intake_kind(self, source, folder_id, gate):
        """Retire an exact-version kind question without granting posting authority."""
        if gate.classification not in {'normal','medical'}:return False
        key=review_id('intake',source);old=self.items.get(key)
        if not old or old['status']!='waiting' or any(old['inputs']):return False
        live=self.ui_rows().get(key)
        if live and any(live[1][7:15]):return False
        self.verify_source(source,folder_id)
        if self.ui_rows().get(key)!=live:return False
        item=deepcopy(old)
        item.update(status='closed_machine',gate={'classification':gate.classification,
            'reason':gate.reason_code,'extraction':gate.extraction_status},
            reason=('種類を自動識別：一般の買物・利用明細。通常の取込処理へ。' if gate.classification=='normal'
                    else '種類を自動識別：医療。専用の医療確認行へ。')+'この判定ではAI送信・記帳を行っていません。')
        self.save_item(key,item)
        return True

    def route_owner_intake(self, source, folder_id):
        """An exact-version kind answer can narrow routing, never permit AI/posting."""
        key=review_id('intake',source);old=self.items.get(key)
        if not old or old['status'] not in {'waiting','closed_user'}:return ''
        inputs=old['inputs']
        if any(inputs[:7]):return ''  # Includes an explicit hold.
        answer=str(inputs[7]).strip()
        if answer not in {'医療','対象外'}:return ''
        live=self.ui_rows().get(key)
        if live is None or live[1][7:15]!=inputs:return ''
        if live[1][:2]+live[1][3:7]!=old.get('presentation'):return ''
        self.verify_source(source,folder_id)
        if self.ui_rows().get(key)!=live:return ''
        if answer=='医療':
            self.observe_medical(source,folder_id)
        if old['status']=='waiting':
            item=deepcopy(old)
            item.update(status='closed_user',decision='本人の種類回答: '+answer,
                reason=('本人が医療と確認。医療確認行で支払日・施設・実支払額・カテゴリを入力してください。'
                        if answer=='医療' else '本人が対象外と確認。会計処理せず原本を保持します。'))
            item.pop('error',None);self.save_item(key,item)
        return answer

    def close_keep_existing(self):
        """Keeping a verified existing ledger does not adopt a changed original."""
        tables=self.tables();closed=0
        for key,old in list(self.items.items()):
            if (old['kind']!='normal' or old['status'] not in {'waiting','superseded'}
                    or old.get('require_reconfirm') or old['inputs'][5]!='既存値を維持'):continue
            current=target_snapshot(tables,old['source']['source_id'])
            if not current['expense_rows'] or digest(current)!=digest(old['before']):continue
            live=self.ui_rows().get(key)
            if (live is None or live[1][7:15]!=old['inputs']
                    or live[1][:2]+live[1][3:7]!=old.get('presentation')):continue
            if self.ui_rows().get(key)!=live:continue
            if digest(target_snapshot(self.tables(),old['source']['source_id']))!=digest(current):continue
            item=deepcopy(old);item.update(status='closed_user',decision='本人が既存値維持を選択')
            item.pop('error',None);self.save_item(key,item);closed+=1
        return closed

    def needs_attention(self,key):
        item=self.items[key]
        if item['status'] in {'waiting','pending'}:return True
        if item['status']=='superseded':
            # Keep an unresolved changed-source warning until a successor exists.
            return not any(k!=key and x['kind']==item['kind'] and x['source']['source_id']==item['source']['source_id']
                           and x['status']!='superseded' for k,x in self.items.items())
        return False

    def review_counts(self):
        active=[x for k,x in self.items.items() if self.needs_attention(k)]
        return {'review_pending':len(active),
                **{kind+'_review_pending':sum(x['kind']==kind for x in active) for kind in ('normal','medical','intake')}}

    def ui_rows(self):
        if TITLE not in self.db.sheet_titles():return {}
        if self.db.get(f"'{TITLE}'!1:1") != [HEADERS]:
            raise StateError('confirmation_sheet_header_mismatch')
        rows={}
        for n,row in enumerate(self.db.get(f"'{TITLE}'!A2:P"),2):
            if not row or not any(x != '' for x in row):continue
            if not row[0]:raise StateError('confirmation_missing_ui_identity')
            if row[0] in rows:raise StateError('confirmation_duplicate_ui_identity')
            rows[row[0]]=(n,list(row)+['']*max(0,16-len(row)))
        return rows

    def _write_new_ui_row(self, row):
        self._write_new_ui_rows([row])

    def _write_new_ui_rows(self, rows):
        if not rows:return
        # values.append can infer a table above/beside the intended data and
        # INSERT_ROWS can displace the header. Keep every existing row in place.
        self.ui_rows()
        occupied=self.db.get(f"'{TITLE}'!A2:P")
        n=len(occupied)+2
        target=f"'{TITLE}'!A{n}:P{n+len(rows)-1}"
        if any(cell != '' for cells in self.db.get(target) for cell in cells):
            raise StateError('confirmation_new_row_occupied')
        def matches():
            actual=self.db.get(target)
            return len(actual)==len(rows) and [r+['']*max(0,16-len(r)) for r in actual]==rows
        try:
            self.db.set_raw_range(target,rows)
        except Exception:
            # A lost response is not permission to repeat the write.
            if not matches():
                raise StateError('confirmation_new_row_write_unknown') from None
        if not matches():
            raise StateError('confirmation_new_row_readback_mismatch')
        self.ui_rows()  # Verify the header and unique identities after writing.

    def capture_inputs(self):
        for key,(_,row) in self.ui_rows().items():
            if key not in self.items:raise StateError('confirmation_unknown_ui_identity')
            old=self.items[key]
            if old['status'] in {'applied','closed_user','closed_machine','superseded'}:continue
            inputs=row[INPUT_START:INPUT_END]
            if inputs!=old.get('inputs'):
                if old['status']=='pending':raise StateError('confirmation_pending_inputs_changed')
                item=deepcopy(old);item['inputs']=inputs
                if str(inputs[5]) in {'','保留'}:item.pop('require_reconfirm',None)
                item.pop('error',None);self.save_item(key,item)

    def _parsed(self,item):
        if item['kind']=='normal':
            return ReceiptResult.model_validate(self.store.value['records'][item['source']['source_id']]['parsed'])
        v=list(item['inputs'])
        if v[5]=='候補で医療費を確定':
            candidate=item.get('medical_candidates',{})
            if candidate.get('source')!=item['source'] or not candidate.get('candidate_id'):
                raise ValueError('現在の原本に対応する候補がありません')
            # Explicit adoption uses only missing fields. Never edits H:O and
            # never overwrites the user's independently entered values.
            for i,name in enumerate(('date','issuer','amount_yen','category')):
                if v[i]=='':v[i]=candidate.get(name,'')
        day=_date(v[0]);amount=_money(str(v[2]).replace(',',''));category=str(v[3]).split('｜')
        if not day:
            import re
            match=re.fullmatch(r'(\d{4})[-/](\d{1,2})[-/](\d{1,2})',str(v[0]))
            if match:
                try:day=date(*map(int,match.groups())).isoformat()
                except ValueError:pass
        if not day or not str(v[1]).strip() or amount is None or amount<=0 or len(category)!=2:
            raise ValueError('支払日・発行施設・本人の実支払額・カテゴリを入力してください')
        if tuple(category) not in medical_categories(self.db.categories()):
            raise ValueError('既存マスタの医療費カテゴリを選択してください')
        return ReceiptResult(date=day,merchant=str(v[1]).strip(),total=int(amount),payment_method=str(v[4]),
            items=[ReceiptItem(name='医療費（本人確認）',amount=int(amount),major_category=category[0],minor_category=category[1])])

    def _plan(self,item,parsed,linked,distinct=False,automatic=False,local_duplicate=None):
        source=item['source'];sid=source['source_id'];rid='R-'+sid;iid='receipt:'+sid
        tables=self.tables();before=target_snapshot(tables,sid)
        if item['kind']=='normal' and digest(before)!=digest(item['before']):
            raise ValueError('既存行・照合・判断が変更されたため再確認が必要です')
        if item['kind']=='medical' and any(before.values()):
            raise ValueError('既存取込または支出があるため同一性を再確認してください')
        if any(len(r)>9 and r[0]!=iid and r[9] for r in before['import_rows']):
            raise ValueError('照合済み支払いを保護しています。既存値維持を選ぶか照合を確認してください')
        if any(any(r[9:15]) for r in before['review_rows']):
            raise ValueError('既存の本人判断を保護しています')
        candidates=[]
        if automatic and item['kind']=='medical':
            from .medical_payment_units import compare_payments
            matches=compare_payments(parsed,tables,days=7,unknown_dates=False)
            from .medical_local_duplicate import LocalReconciliation
            verified_duplicate=(isinstance(local_duplicate,LocalReconciliation)
                and local_duplicate.valid(source,parsed,tables,linked))
            if distinct or (linked and not verified_duplicate):
                raise ValueError('自動処理では既存支払いの紐付け・別取引の強制指定はできません')
            if not verified_duplicate and any(x.classification!='different' for x in matches):
                raise ValueError('同日付近・同額の既存支払い、または支払い単位が未確定です')
            if verified_duplicate:
                candidates=[list(r)+['']*max(0,13-len(r)) for r in tables['expense_rows']
                            if r[0]==linked and len(r)>12 and r[12]=='active'
                            and _date(r[1])==parsed.date and _money(r[4])==parsed.total]
        else:
            for row in tables['expense_rows']:
                r=list(row)+['']*max(0,13-len(row))
                if r[12]!='active' or r[9]==rid or r[10]==iid:continue
                day=_date(r[1])
                if day and abs((date.fromisoformat(day)-date.fromisoformat(parsed.date)).days)<=7 and _money(r[4])==parsed.total:
                    candidates.append(r)
        if linked:
            selected=[r for r in candidates if r[0]==linked]
            if len(selected)!=1:raise ValueError('統合先は同日付近・同額の有効な支出IDから選択してください')
        elif candidates and not distinct:
            raise ValueError('同日付近・同額の既存支出あり。重複を確認して統合先支出IDを入力してください: '+', '.join(r[0] for r in candidates))
        expected=[]
        origin='自動検証済み（medical-auto-v1）' if automatic else '本人確認済み'
        old_receipt=before['receipt_rows'][0] if before['receipt_rows'] else None
        old_import=next((r for r in before['import_rows'] if r[0]==iid),None)
        receipt=[rid,parsed.date,parsed.merchant,parsed.total,parsed.payment_method,
                 'https://drive.google.com/file/d/'+sid+'/view','解析済',
                 old_receipt[7] if old_receipt and len(old_receipt)>7 else now_jst_string(),
                 ((str(old_receipt[8])+'; ') if old_receipt and len(old_receipt)>8 and old_receipt[8] else '')+origin+'（'+item['kind']+'）']
        imported=[iid,old_import[1] if old_import else now_jst_string(),'receipt',sid,parsed.date,parsed.merchant,
                  parsed.total,parsed.payment_method,'matched_receipt' if linked else '解析済',linked or '',canonical_hash(parsed.model_dump()),
                  ((str(old_import[11])+'; ') if old_import and len(old_import)>11 and old_import[11] else '')+origin]
        expected.append(['レシート',receipt])
        if not linked:
            if len(before['expense_rows'])>len(parsed.items):raise ValueError('既存明細の削除を伴う変更は自動反映しません')
            for i,x in enumerate(parsed.items,1):
                eid=f'{rid}-{i:02d}'
                old=next((r for r in before['expense_rows'] if r[0]==eid),None)
                if old and (len(old)<13 or old[12]!='active'):raise ValueError('既存の無効明細を保護しています')
                expected.append(['支出明細',[eid,parsed.date,parsed.merchant,x.name,x.amount,x.major_category,x.minor_category,
                    parsed.payment_method,'receipt',rid,iid,old[11] if old else origin,'active']])
        elif before['expense_rows']:raise ValueError('既存明細がある対象の重複統合は自動変更しません')
        expected.append(['取込データ',imported])
        for title,row in expected:
            matches=[r for r in _rows(self.db,title) if r and r[0]==row[0]]
            if len(matches)>1:raise ValueError('既存IDが重複しています')
            if title=='支出明細' and matches and matches[0] not in before['expense_rows']:
                raise ValueError('別取引で同じ支出IDが使われています')
        return expected

    def _complete(self,plan):
        for title,expected in plan:
            matches=[r for r in _rows(self.db,title) if r and r[0]==expected[0]]
            if len(matches)!=1 or not same_row(title,matches[0],expected):return False
        return True

    def apply_confirmations(self, *, dry_run=False):
        self.close_keep_existing()
        written=0
        for key,old in list(self.items.items()):
            if (old['status']=='superseded' and old.get('error')=='原本の版が変更。新しい対象で再確認してください'
                    and self.needs_attention(key)):
                # A Drive metadata version can change while the original bytes
                # remain identical. Revive only the same displayed owner choice.
                try:self.verify_source(old['source'],old['folder_id'])
                except StateError as error:
                    if str(error)!='confirmation_source_changed':raise
                else:
                    live=self.ui_rows().get(key)
                    if (live is not None and live[1][7:15]==old['inputs']
                            and live[1][:2]+live[1][3:7]==old.get('presentation')):
                        item=deepcopy(old);item['status']='waiting';item.pop('error',None)
                        self.save_item(key,item);old=item
            if old['status']=='pending':
                from .medical_local_duplicate import verify_saved_targets
                verify_saved_targets(old,self.tables)
                self.verify_source(old['source'],old['folder_id'])
                if not self._complete(old['plan']):raise StateError('confirmation_write_reconciliation_required')
                item=deepcopy(old);item['status']='applied';self.save_item(key,item);continue
            if old['status']!='waiting':continue
            action=str(old['inputs'][5]);item=deepcopy(old)
            if old['kind']=='intake':
                if action not in {'','保留'}:
                    item['error']='受付保留の行では計上しません。判断は保留に戻し、本人メモに原本の種類を記入してください。'
                    self.save_item(key,item)
                continue
            if item.get('require_reconfirm'):continue
            if action in {'','保留'}:continue
            try:
                if action=='既存値を維持':
                    if item['kind']!='normal':raise ValueError('医療費は必要項目と確定判断を入力してください')
                    if not item['before']['expense_rows']:raise ValueError('明細が未計上です。候補明細の確認、重複先の指定、または保留を選んでください')
                    raise ValueError('既存行または表示・入力が変更されています。現在の記帳を再確認してください')
                self.verify_source(item['source'],item['folder_id'])
                if action not in ({'候補明細で確定','既存支出と重複（紐付け）','重複候補と別の支出として確定'} if item['kind']=='normal' else {'医療費を確定','候補で医療費を確定','既存支出と重複（紐付け）','重複候補と別の支出として確定'}):
                    raise ValueError('種別に対応する確定判断を選択してください')
                parsed=self._parsed(item)
                from .receipt_pipeline import validate_receipt_result
                valid,_=validate_receipt_result(parsed,self.db.categories())
                if not valid:raise ValueError('候補の明細・日付・カテゴリが検証条件を満たしません')
                linked=str(item['inputs'][6]).strip() if action=='既存支出と重複（紐付け）' else ''
                if action=='既存支出と重複（紐付け）' and not linked:raise ValueError('統合先支出IDを入力してください')
                plan=self._plan(item,parsed,linked,action=='重複候補と別の支出として確定')
                live=self.ui_rows().get(key)
                if live is None or live[1][7:15]!=item['inputs']:raise ValueError('入力が変更されたため次回再確認します')
                if live[1][:2]+live[1][3:7]!=item.get('presentation'):
                    item['require_reconfirm']=True
                    raise ValueError('表示内容が変更されています。判断を保留に戻して原本・候補を再確認してください')
            except ValueError as e:
                item['error']=str(e);self.save_item(key,item);continue
            except StateError as e:
                if str(e)!='confirmation_source_changed':raise
                item.update(status='superseded',error='原本の版が変更。新しい対象で再確認してください')
                self.save_item(key,item);continue
            # Durable intent before any accounting call. Unknown/partial writes
            # remain pending; next invocation can only reconcile a full readback.
            if dry_run:
                written+=1
                continue
            item.update(status='pending',plan=plan,confirmation_hash=digest(item['inputs']))
            self.save_item(key,item)
            try:
                self.verify_source(item['source'],item['folder_id'])
                live=self.ui_rows().get(key)
                if live is None or live[1][7:15]!=item['inputs']:
                    raise StateError('confirmation_changed_before_write')
            except StateError as error:
                if str(error) not in {'confirmation_source_changed','confirmation_changed_before_write'}:raise
                # This invocation has made zero accounting calls. Record that
                # fact and require a fresh human confirmation; do not clear an
                # earlier pending/unknown write through this path.
                item.update(status='waiting',require_reconfirm=True,aborted_before_accounting=True,
                            error='反映前に対象または入力が変更。保留に戻してから再確定してください')
                item.pop('plan',None);item.pop('confirmation_hash',None)
                self.save_item(key,item);continue
            self._write_accounting_plan(key,item);written+=1
        return written

    def _write_accounting_plan(self,key,item):
        if item['status']!='pending':raise StateError('confirmation_intent_required')
        from .medical_local_duplicate import verify_saved_targets
        verify_saved_targets(item,self.tables)
        for title,row in item['plan']:
            matches=[(n,r) for n,r in enumerate(_rows(self.db,title),2) if r and r[0]==row[0]]
            if len(matches)>1:raise StateError('confirmation_duplicate_accounting_identity')
            try:
                if matches:
                    if not same_row(title,matches[0][1],row):self.db.update_row_raw(title,matches[0][0],row)
                else:self.db.append_raw(title,[row])
            except Exception:
                actual=[r for r in _rows(self.db,title) if r and r[0]==row[0]]
                if len(actual)!=1 or not same_row(title,actual[0],row):raise StateError('confirmation_write_unknown') from None
        if not self._complete(item['plan']):raise StateError('confirmation_readback_mismatch')
        verify_saved_targets(item,self.tables)
        item['status']='applied';self.save_item(key,item)

    def refresh_needed(self):
        return any(x['status']=='applied' and not x.get('display_refreshed') for x in self.items.values())

    def mark_refreshed(self):
        for key,old in list(self.items.items()):
            if old['status']=='applied' and not old.get('display_refreshed'):
                item=deepcopy(old);item['display_refreshed']=True;self.save_item(key,item)

    def render(self):
        if TITLE in self.db.sheet_titles() and self.db.get(f"'{TITLE}'!1:1")!=[HEADERS]:
            raise StateError('confirmation_sheet_header_mismatch')
        self.db.ensure_sheet(TITLE,HEADERS)
        existing=self.ui_rows()
        tables=self.tables();categories=self.db.categories()
        new_rows=[]
        # INSERT_ROWS may shift displayed rows. Finish every positional update
        # before appending so captured row numbers cannot target a new identity.
        for key,item in sorted(self.items.items(),key=lambda pair:pair[0] not in existing):
            if item['status']=='closed_machine' and key not in existing:continue
            medical=item['kind']=='medical'
            if item['kind']=='intake':
                prior='未記帳（この受付から会計行は追加しません）'
                candidate='AI未送信・候補なし。種類の回答だけではAI送信・計上しません。医療は専用確認、対象外は受信フォルダ外で保管。患者名・病名・診療内容は入力不要。'
            elif medical:
                prior='本人未確定（入力値はH:O）'
                values=item.get('medical_candidates',{})
                if item.get('local_decision') and item['status'] in {'pending','applied'}:
                    local=item['local_decision']['parsed']
                    candidate='【自動反映内容】\n日付: '+local['date']+'\n施設: '+local['merchant']+'\n実支払額: '+str(local['total'])+'\n原本をローカルで照合。外部送信なし。'
                elif values:
                    candidate='【未確定候補】\n日付（非AI）: '+str(values.get('date') or '不足')+'\n施設（非AI）: '+str(values.get('issuer') or '不足')+'\n実支払額（画像AI）: '+str(values.get('amount_yen') or '不足')+'\nカテゴリ候補: '+str(values.get('category') or '不足')+'\n'+str(values.get('review_message',''))
                else:candidate='候補なし。患者名・病名・診療内容は入力不要'
            else:
                r=self.store.value['records'][item['source']['source_id']];a=r['parsed']
                current=target_snapshot(tables,item['source']['source_id'])
                h=current['receipt_rows'][0] if current['receipt_rows'] else item['before']['receipt_rows'][0]
                prior=f'{h[1]} / {h[2]} / {h[3]} / {h[4]}\n'+ '\n'.join(f'{x[3]}: {x[4]} ({x[5]}｜{x[6]})' for x in current['expense_rows'])
                candidate=f'{a["date"]} / {a["merchant"]} / {a["total"]} / {a["payment_method"]}\n'+'\n'.join(f'{x["name"]}: {x["amount"]} ({x["major_category"]}｜{x["minor_category"]})' for x in a['items'])
            state={'waiting':'未確認','pending':'確定待ち','applied':'反映済み','closed_user':'変更不要（本人判断）','closed_machine':'変更不要（機械判断）','superseded':'原本変更・再確認'}[item['status']]
            if item['status']=='waiting' and any(item.get('inputs',[])):state='入力中' if not item['inputs'][5] else '確定待ち'
            if item.get('error'):state='要再確認'
            if item['status']=='applied' and item.get('decision_origin')=='automatic':state='自動反映済み'
            reason=item['reason']
            if item['kind']=='normal' and item['status']=='waiting':
                from .receipt_review_policy import general_review_guidance
                reason=general_review_guidance(item['source']['source_id'],self._parsed(item),**tables,categories=categories)
            if item['status']=='superseded' and not self.needs_attention(key):reason=SUPERSEDED_REVIEW_REASON
            if item.get('automatic_hold') and item['status']=='waiting':
                from .medical_auto_posting import HOLD_TEXT
                reason='自動保留: '+HOLD_TEXT.get(item['automatic_hold'],'安全な匿名化・記帳条件を確認できません。本人確認は任意です。')
                reason+=' 原本を確認し、候補が正しければ「候補で医療費を確定」。不足・誤りだけH:Kへ入力。判断できなければ保留。'
            if state=='自動反映済み':
                reason='機械検証・反映内容の読戻し済み。本人の操作は不要です。'
                prior='自動検証済み（本人入力H:Oは保持）'
                candidate=candidate.replace('【未確定候補】','【自動反映内容】',1)
            managed=[key,'医療' if medical else '受付保留' if item['kind']=='intake' else '一般',state,'https://drive.google.com/file/d/'+item['source']['source_id']+'/view',reason,prior,candidate]
            presentation=managed[:2]+managed[3:7]
            if item.get('presentation')!=presentation:
                item=deepcopy(item);item['presentation']=presentation;self.save_item(key,item)
            if key in existing:
                n,row=existing[key]
                # Never rewrite H:O. The user can be typing while refresh runs.
                if row[:7]!=managed:self.db.set_raw_range(f"'{TITLE}'!A{n}:G{n}",[managed])
                result=item.get('error','') or ('確認内容を読戻し済み' if item['status']=='applied' else '')
                if row[15]!=result:self.db.set_raw_range(f"'{TITLE}'!P{n}",[[result]])
            else:
                new_rows.append(managed+item.get('inputs',['']*8)+[item.get('error','')])
        self._write_new_ui_rows(new_rows)
        return len(self.ui_rows())
