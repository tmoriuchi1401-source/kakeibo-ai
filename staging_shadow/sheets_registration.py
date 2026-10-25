"""One registration operation, no transaction approval, no financial dispatch.

Reuses the #157 polling/intent lease/journal/metadata CAS and #155 native
preflight. Production gates stay unchanged. This adapter stops at the existing
financial boundary: its standing-policy evidence is NOT VerifiedPost.
"""
from copy import deepcopy

from app.drive_run_state import StateError
from app.unified_receipt_review import digest
from .sheets_only_review import SheetsOnlyReview, LABELS, canon, uid
from .sheets_only_surface import fail, rule, IDLE
from .standing_permission import SyntheticStandingPermission, deny
from .medical_input_display import PREFIX, partial_reason, terminal_reason


class SheetsRegistration(SheetsOnlyReview):
    schema = 'synthetic-sheets-registration-v1'
    slot = 'sheets_registration'
    request_word = '登録'
    policy_rejection = 'PERMISSION_UNVERIFIED'
    labels = dict(LABELS, VALIDATED_UNPOSTED='登録検証済み・未記帳',
                  PERMISSION_UNVERIFIED='権限未検証・未記帳')

    def __init__(self, handoff, surface, permission, *, enabled=False, isolated_synthetic=False):
        super().__init__(handoff, surface, enabled=enabled, isolated_synthetic=isolated_synthetic)
        if (type(permission) is not SyntheticStandingPermission or not surface.registration
                or permission.runtime['owner'] != handoff.owner
                or permission.runtime['workbook'] != surface.contract['workbook_id']
                or permission.runtime['writer'] != 'existing-receipt-confirmation'):
            fail('registration_isolation_required')
        self.permission = permission
        self.stable = {}

    def _other_pending(self):
        # Old mode UNKNOWN remains a fence after the policy migration.
        old = SheetsOnlyReview(self.handoff, self.surface, enabled=True, isolated_synthetic=True)
        return any(r['status'] in {'ATTEMPTING','UNKNOWN'} for r in old._state()[1]['requests'].values())

    def _options(self, ledger, cid, record):
        if record['reason'].startswith(PREFIX) and (record['status'] == 'CONFLICT'
                or '受付済み・未記帳' in record['reason']):
            return []  # Presentation only; do not suggest another registration.
        return [] if record['status'] in {'UNKNOWN','VALIDATED_UNPOSTED','FINANCIAL_BOUNDARY_STOPPED'} else ['登録 '+str(self._sequence(ledger,cid)+1)]

    def _partial_reason(self, cid, error, partial):
        if self.handoff.confirmation.cases[cid].kind == 'medical':
            return partial_reason(error, partial['value']['fields'])
        return super()._partial_reason(cid, error, partial)

    def _finish(self, cid, rid, record, batch, status, reason='', frame=None, basis='', owns_lease=False):
        case = self.handoff.confirmation.cases.get(cid)
        if case is not None and case.kind == 'medical':
            reason = terminal_reason(status, reason)
        return super()._finish(cid, rid, record, batch, status, reason, frame, basis, owns_lease)

    def _project(self,cid,record,batch,ledger):
        # Durable publication intent, in the SAME encrypted journal. A worker
        # restart must not resend a lost Sheet write. Exact display read-back
        # is permitted; a mismatch requires operator reconciliation.
        text=self._display(cid,record)
        wanted={'userEnteredValue':{'formulaValue':self.surface.formula(cid,record['command'],text,batch)}}
        options=rule(list(dict.fromkeys([IDLE,record['command']]+self._options(ledger,cid,record))))
        cells=self.surface.row(cid)
        if cells[2]==wanted and cells[1].get('dataValidation')==options:
            return super()._project(cid,record,batch,ledger)
        state,_,tag=self.journal._state()
        details=deepcopy(state['details'])
        fences=details.setdefault('registration_projection_intents',{})
        if type(fences) is not dict or len(fences)>256:
            fail('projection_journal_quarantined')
        key=digest([cid,wanted,options])
        if key in fences:
            self.projection_unknown.add(cid)
            return
        if cid in self.projection_unknown:
            return
        fences[key]=dict(case_id=cid,output_digest=digest([wanted,options]))
        try:
            result=self.journal.save(batch,tag,uid('registration-projection:'+key),details=details)
        except Exception:
            self.projection_unknown.add(cid)
            return  # No publication may begin without an acknowledged intent.
        if result['status']!='draft_saved':
            self.projection_unknown.add(cid)
            return
        return super()._project(cid,record,batch,ledger)

    def _display(self, cid, record):
        status = record['status']
        if record['reason'].startswith(PREFIX):
            # Old immutable receipts keep the old projection, including on
            # restart. Only newly finished medical receipts carry this prefix.
            label = '受付済み・未記帳' if status == 'HELD' and '受付済み・未記帳' in record['reason'] else self.labels[status]
            text = label+'\n'+record['reason'][len(PREFIX):]
            if status == 'VALIDATED_UNPOSTED':
                text += '\n記帳完了ではありません。結果照会のみ。'
            if status == 'HELD' and record['reason'].startswith(PREFIX+'入力不足・未入力：'):
                ledger = self._state()[1]
                if not any(r['status'] in {'UNKNOWN', 'ATTEMPTING', 'VALIDATED_UNPOSTED'}
                           for _, r in self._records(ledger, cid)):
                    text += '\n修正・同期後の試験時は「登録 '+str(self._sequence(ledger, cid)+1)+'」。'
            return text+'\n案件 '+cid
        if status in {'VALIDATED_UNPOSTED','PERMISSION_UNVERIFIED'}:
            text = self.labels[status]+'\n案件 '+cid+'\n'+record['reason']
            if status == 'VALIDATED_UNPOSTED':
                f=record['frame']; p=f['standing_permission']; fields=f['value']['fields']
                text += '\n原本 '+f['source']['source_id']+' / 版 '+f['source']['drive_revision_id']
                text += '\n'+str(fields['date'])+' / '+str(fields['merchant'])+' / '+str(fields['amount'])+'円'
                text += '\n設定版 '+str(p['policy_revision'])+' / 入力版 '+record['core_digest'][:8]
                text += '\n記帳完了ではありません。結果照会のみ。'
            return text
        return super()._display(cid, record).replace('比較','登録').replace('確定不可','登録不可')

    def _checkpoint(self, cid, frame=None, *, before=None):
        c = self.handoff.confirmation.cases.get(cid)
        if c is None:
            deny('native_adapter_unbound')
        p = self.permission.check(cid, source_id=c.source_id, kind=c.kind, before=before)
        if p['route']['entry'] not in {'receipt_manual','medical_manual'}:
            deny('native_adapter_unbound')
        if frame is not None:
            if frame['source']['source_id'] != p['route']['source_id']:
                deny('source_not_allowed')
            amount = frame['value']['fields']['amount']
            if amount != '':  # Partial medical drafts may be saved, never validated.
                p = self.permission.check(cid, source_id=c.source_id, kind=c.kind, amount=amount, before=before)
        return p

    def _compare(self, cid, command, batch, core_digest, ledger, tag):
        rid, record = self._new(cid, command, core_digest)
        if command != '登録 '+str(self._sequence(ledger,cid)+1):
            return self._finish(cid,rid,record,batch,'CONFLICT','古い依頼または順序不一致です。次の登録番号を選択してください。')
        # Consume a valid request number even when held/unauthorized. A fixed
        # configuration can then use a NEW request; immutable denials are never
        # rewritten into permissions by changing a setting or replaying a cell.
        record['sequence_accepted']=True
        # Repeated registration of an already validated source never becomes
        # a new financial request, even with a different command/input digest.
        if any(r['status']=='VALIDATED_UNPOSTED' for _,r in self._records(ledger,cid)):
            return self._finish(cid,rid,record,batch,'HELD','この原本の登録検証は受付済みです。保存結果を照会してください。')
        if cid in self.handoff.confirmation.held:
            return self._finish(cid,rid,record,batch,'HELD','PDF・旧保留・未対応案件です。入力は保持しています。')
        try:
            self._checkpoint(cid)
        except StateError:
            return self._finish(cid,rid,record,batch,'PERMISSION_UNVERIFIED','初期設定の利用権限を確認できません。入力を保持して停止しました。')
        except OSError:
            return dict(status='WAITING',case_id=cid,accounting_permission=False,
                        writer_dispatches=0,authority_dispatches=0,synthetic_only=True)
        # A new/reconstructed worker must independently see stable input. This
        # reduces partial mobile sync; it does not claim offline-device sync proof.
        now = self.handoff.confirmation.clock()
        old = self.stable.get(cid)
        if not old or old[:2] != (command, core_digest) or now < old[2]:
            self.stable[cid] = (command,core_digest,now)
            return dict(status='SYNC_WAIT',case_id=cid,accounting_permission=False,
                        writer_dispatches=0,authority_dispatches=0,synthetic_only=True)
        if now-old[2] < 3:
            return dict(status='SYNC_WAIT',case_id=cid,accounting_permission=False,
                        writer_dispatches=0,authority_dispatches=0,synthetic_only=True)
        return super()._compare(cid, command, batch, core_digest, ledger, tag)

    def _validated(self, cid, rid, record, batch, frame, policy):
        self._checkpoint(cid,frame,before=policy)
        f=deepcopy(frame); f['standing_permission']=canon(policy)
        return self._finish(cid,rid,record,batch,'VALIDATED_UNPOSTED',
            '登録内容の検証が完了しました。合成検証のため未記帳です。',
            frame=f,owns_lease=True)

    def _confirm(self,cid,command,batch,core_digest,ledger,tag):
        rid,record=self._new(cid,command,core_digest)
        return self._finish(cid,rid,record,batch,'CONFLICT','個別承認操作は不要です。登録の選択肢を使用してください。')

    def inspect(self,rid):
        result=super().inspect(rid)
        result.update(per_transaction_approval_required=False,
                      standing_policy_verified=result['status']=='VALIDATED_UNPOSTED',
                      financial_boundary_connected=False,accounting_writes=0)
        return result
