"""Level-triggered synthetic Sheet inbox. No HTTP/auth/financial capability.

Poll the current cells, never replay edit events. Reuse #155 fresh capture,
existing draft CAS/native comparison and encrypted journal. Cell requests can
save untrusted metadata, but can NEVER establish owner approval. In particular
we do not forge a session/CSRF gate in order to call the authenticated adapter.
"""
from copy import deepcopy
import re
from uuid import NAMESPACE_URL, uuid5

from app.drive_run_state import StateError
from app.iphone_sheet_reader import DraftBatch
from app.receipt_confirmation_contract import encoded, load, memory_only
from app.unified_receipt_review import digest
from .iphone_confirmation_ui import DraftCAS
from .iphone_sheet_handoff import SheetDraftHandoff
from .sheets_only_surface import SyntheticSheetSurface, IDLE, fail

SCHEMA = 'synthetic-sheets-only-review-v1'
SLOT = 'sheets_only_review'
LABELS = dict(COMPARED='比較済み・未承認', HELD='保留・未記帳', CONFLICT='競合・未記帳',
              APPROVAL_UNESTABLISHED='承認未成立・未記帳', UNKNOWN='UNKNOWN：結果不明・照合待ち',
              FINANCIAL_BOUNDARY_STOPPED='確認済み・未記帳（既存版の保存結果）')
VALIDATION = {'draft_v2_integer', 'draft_v2_text', 'draft_v2_line_identity',
              'draft_v2_line_category', 'draft_v2_complete_manual_only', 'draft_v2_receipt_validator'}


def canon(value):
    return load(encoded(value))


def uid(value):
    return str(uuid5(NAMESPACE_URL, value))


class SheetsOnlyReview:
    def __init__(self, handoff, surface, *, enabled=False, isolated_synthetic=False):
        if (enabled is not True or isolated_synthetic is not True
                or type(handoff) is not SheetDraftHandoff or type(surface) is not SyntheticSheetSurface
                or surface.contract != handoff.reader.contract
                or handoff.reader.read_raw != surface.core
                or handoff.reader.read_sources != surface.observe_sources):
            fail('isolation_required')
        memory_only(handoff.journal.transport.io)
        memory_only(surface)
        self.handoff, self.surface = handoff, surface
        self.journal = handoff.journal
        self.uncertain = None
        self.projection_unknown = set()

    def _state(self):
        state, _, tag = self.journal._state()
        ledger = deepcopy(state['details'].get(SLOT, dict(schema=SCHEMA, requests={})))
        try:
            if set(ledger) != {'schema', 'requests'} or ledger['schema'] != SCHEMA or len(ledger['requests']) > 64:
                fail('journal_quarantined')
            for rid, r in ledger['requests'].items():
                if (set(r) != {'fingerprint','case_id','command','core_digest','status','frame','comparison_digest',
                              'basis','reason','approval_established','sequence_accepted','created_at','expires_at'}
                        or rid != uid(r['fingerprint']) or r['case_id'] not in self.surface.contract['cases']
                        or r['status'] not in set(LABELS) | {'ATTEMPTING'}
                        or r['comparison_digest'] != digest([rid, r['frame']])
                        or r['expires_at']-r['created_at'] != 600
                        or type(r['sequence_accepted']) is not bool or r['approval_established'] is not False):
                    fail('journal_quarantined')
            return state, ledger, tag
        except (KeyError, TypeError, ValueError, AttributeError):
            fail('journal_quarantined')

    def _save(self, batch, ledger, rid, stage, tag):
        state, _, _ = self._state()
        details = deepcopy(state['details']); details[SLOT] = ledger
        expected = ledger['requests'][rid]['status']
        try:
            result = self.journal.save(batch, tag, uid(rid+':'+stage), details=details)
            if result['status'] != 'draft_saved':
                fail('attempt_read_only_no_resend')
        except Exception:
            self.uncertain = (rid, expected)
            raise

    def _records(self, ledger, cid):
        return [(rid, r) for rid, r in ledger['requests'].items() if r['case_id'] == cid]

    def _sequence(self, ledger, cid):
        return max([int(r['command'].split()[1]) for _, r in self._records(ledger, cid)
                    if r['sequence_accepted'] and re.fullmatch(r'比較 [1-9][0-9]{0,2}', r['command'])]+[0])

    def _options(self, ledger, cid, record):
        if record['status'] in {'UNKNOWN', 'FINANCIAL_BOUNDARY_STOPPED'}:
            return []
        options = ['比較 '+str(self._sequence(ledger, cid)+1)]
        if record['status'] == 'COMPARED':
            options.append('確定 '+record['comparison_digest'][:8])
        return options

    def _display(self, cid, record):
        text = LABELS[record['status']]
        if record['status'] == 'UNKNOWN':
            return text+'。自動再実行しません。入力を保持しています。'
        if record['status'] == 'FINANCIAL_BOUNDARY_STOPPED':
            return text+'\n案件 '+cid+'\n既存版 '+record['basis'][:8]+'\n今回のセル入力の承認ではありません。再取込は停止しています。'
        if record['status'] == 'APPROVAL_UNESTABLISHED':
            return text+'\n案件 '+cid+'\n比較版 '+record['basis'][:8]+'\n編集者の本人認証が確認できません。金融処理は保留です。'
        if record['status'] in {'HELD', 'CONFLICT'}:
            return text+'\n案件 '+cid+'\n'+record['reason']
        frame = record['frame']; source = frame['source']; snapshot = frame['snapshot']
        before, after = snapshot['inputs']['proposed']['original_parsed'], snapshot['inputs']['proposed']['parsed']
        text += '\n案件 '+cid+' / '+('医療・完全手入力' if self.handoff.confirmation.cases[cid].kind == 'medical' else '一般・明細別カテゴリ')
        text += '\n比較版 '+record['comparison_digest'][:8]+' / 原本版 '+source['drive_revision_id']
        text += '\n原本 '+source['source_id']+' / hash '+source['sha256'][:12]
        for key, label in [('date', '日付'), ('merchant', '店舗・施設'), ('total', '金額'), ('payment_method', '支払')]:
            old = (before or {}).get(key, '未入力'); new = after[key]
            text += '\n'+label+'：'+str(old)+(' → '+str(new) if old != new else '（変更なし）')
        for n, item in enumerate(after['items'], 1):
            text += '\n明細'+str(n)+' '+item['name']+' '+str(item['amount'])+'円 '+item['major_category']+'｜'+item['minor_category']
        text += '\nメモ：'+frame['value']['fields']['memo']
        return text+'\n確認後「確定 '+record['comparison_digest'][:8]+'」を別操作で依頼'

    def _project(self, cid, record, batch, ledger):
        text = self._display(cid, record)
        formula = {'userEnteredValue': {'formulaValue': self.surface.formula(cid, record['command'], text, batch)}}
        options = self._options(ledger, cid, record)
        from .sheets_only_surface import rule
        expected_rule = rule(list(dict.fromkeys([IDLE, record['command']]+options)))
        cells = self.surface.row(cid)
        if cells[2] == formula and cells[1].get('dataValidation') == expected_rule:
            # Read-only reconciliation, including lost publication responses.
            self.projection_unknown.discard(cid)
            self.surface.expected[cid] = dict(command=record['command'], text=text, batch=deepcopy(batch),
                                              formula=formula, options=expected_rule)
            return
        if cid in self.projection_unknown:
            return  # UNKNOWN projection write is not automatically resent
        try:
            self.surface.publish(cid, record['command'], text, batch, options)
        except StateError as error:
            if str(error) == 'sheets_only_projection_conflict':
                rejected = dict(record, status='CONFLICT', reason='比較表示中に入力・案件対応が変化しました。新しい比較を依頼してください。')
                self._project(cid, rejected, batch, ledger)
            else:
                self.projection_unknown.add(cid)
        except Exception:
            self.projection_unknown.add(cid)

    def _new(self, cid, command, core_digest):
        fingerprint = digest([SCHEMA, self.journal.scope, cid, command, core_digest])
        rid = uid(fingerprint)
        now = self.handoff.confirmation.clock()
        return rid, dict(fingerprint=fingerprint, case_id=cid, command=command,
            core_digest=core_digest, status='ATTEMPTING', frame=None, comparison_digest=digest([rid, None]),
            basis='', reason='', approval_established=False, sequence_accepted=False,
            created_at=now, expires_at=now+600)

    def _finish(self, cid, rid, record, batch, status, reason='', frame=None, basis=''):
        _, ledger, tag = self._state()
        after = deepcopy(record)
        after.update(status=status, reason=reason, frame=canon(frame), comparison_digest=digest([rid, canon(frame)]), basis=basis)
        ledger['requests'][rid] = after
        self._save(batch, ledger, rid, 'result', tag)
        self._project(cid, after, batch, ledger)
        return self.inspect(rid)

    def inspect(self, rid):
        # Internal saved-state query only. No editor/session claims, fresh
        # observations, retries, native qualification or authority issuance.
        _, ledger, _ = self._state()
        r = ledger['requests'].get(rid)
        status = 'UNKNOWN' if not r or r['status'] == 'ATTEMPTING' else r['status']
        return dict(request_id=rid, case_id=r['case_id'] if r else None, status=status,
            approval_established=False, accounting_permission=False, writer_dispatches=0,
            authority_dispatches=0, synthetic_only=True)

    def _compare(self, cid, command, batch, core_digest, ledger, tag):
        rid, record = self._new(cid, command, core_digest)
        expected = self._sequence(ledger, cid)+1
        if command != '比較 '+str(expected):
            return self._finish(cid, rid, record, batch, 'CONFLICT', '古い依頼または順序不一致。次の比較版を選択してください。')
        record['sequence_accepted'] = True
        try:
            captured, frame = self.handoff._capture(cid, 'import_draft')
        except StateError as error:
            if 'UNKNOWN' in str(error):
                return self._finish(cid, rid, record, batch, 'UNKNOWN', '既存native状態が不明です。')
            held = 'held_or_unmapped' in str(error) or str(error) in VALIDATION
            return self._finish(cid, rid, record, batch, 'HELD' if held else 'CONFLICT',
                                '保留案件・入力不足のため確定不可。' if held else '原本・入力・案件対応が変化しました。')
        except Exception:
            return dict(status='WAITING', case_id=cid, reason='独立snapshotの読取通信待ち・依頼は未受付',
                        approval_established=False, accounting_permission=False, writer_dispatches=0, authority_dispatches=0)
        if captured != batch:
            return self._finish(cid, rid, record, batch, 'CONFLICT', '同時編集を検出しました。')
        ledger['requests'][rid] = record
        self._save(batch, ledger, rid, 'attempt', tag)
        # Intent CAS is the only lease to enter this existing metadata draft CAS.
        if self.handoff._capture(cid, 'import_draft') != (batch, frame):
            fail('attempt_UNKNOWN_snapshot_changed')
        d = frame['draft']; c = self.handoff.confirmation.cases[cid]
        if d['value'] != frame['value']:
            before, raw, draft_tag = DraftCAS(c.readers.draft).read()
            if before != d or draft_tag != frame['tag']:
                fail('attempt_UNKNOWN_draft_conflict')
            after = dict(d, revision=d['revision']+1, value=deepcopy(frame['value']), input_digest=digest(frame['value']))
            DraftCAS(c.readers.draft).replace(d, raw, draft_tag, after)
        try:
            fresh, compared = self.handoff._capture(cid, 'confirm_handoff')
        except StateError as error:
            if str(error) not in VALIDATION:
                raise
            fresh, partial = self.handoff._capture(cid, 'import_draft')
            if fresh != batch or partial['value'] != frame['value']:
                fail('attempt_UNKNOWN_partial_changed')
            return self._finish(cid, rid, record, batch, 'HELD', '必須項目または明細別入力が不足しています。部分入力は保存済みです。')
        if fresh != batch or compared['value'] != frame['value']:
            fail('attempt_UNKNOWN_snapshot_changed')
        return self._finish(cid, rid, record, batch, 'COMPARED', frame=compared)

    def _confirm(self, cid, command, batch, core_digest, ledger, tag):
        rid, record = self._new(cid, command, core_digest)
        candidates = [(i, r) for i, r in self._records(ledger, cid)
                      if r['status'] == 'COMPARED' and command == '確定 '+r['comparison_digest'][:8]]
        if len(candidates) != 1:
            return self._finish(cid, rid, record, batch, 'CONFLICT', '比較版と案件IDが一致しません。')
        basis_id, basis = candidates[0]
        if self.handoff.confirmation.clock() >= basis['expires_at']:
            return self._finish(cid, rid, record, batch, 'CONFLICT', '比較期限切れ。新しい比較版を依頼してください。')
        if int(basis['command'].split()[1]) != self._sequence(ledger, cid):
            return self._finish(cid, rid, record, batch, 'CONFLICT', '最新の比較版ではありません。')
        try:
            fresh, frame = self.handoff._capture(cid, 'confirm_handoff')
            expected = self.surface.formula(cid, basis['command'], self._display(cid, basis), batch)
            from .sheets_only_surface import rule
            expected_rule = rule([IDLE, basis['command']]+self._options(ledger, cid, basis))
            if (fresh != batch or canon(frame) != basis['frame'] or core_digest != basis['core_digest']
                    or self.surface.row(cid)[2] != {'userEnteredValue': {'formulaValue': expected}}
                    or self.surface.row(cid)[1].get('dataValidation') != expected_rule):
                fail('comparison_changed')
        except StateError:
            return self._finish(cid, rid, record, batch, 'CONFLICT', '入力・原本・表示・比較版が変化しました。確定を拒否しました。')
        except Exception:
            return dict(status='WAITING', case_id=cid, reason='独立snapshotの読取通信待ち・依頼は未受付',
                        approval_established=False, accounting_permission=False, writer_dispatches=0, authority_dispatches=0)
        ledger['requests'][rid] = record
        self._save(batch, ledger, rid, 'attempt', tag)
        if self.handoff._capture(cid, 'confirm_handoff') != (batch, frame):
            fail('attempt_UNKNOWN_snapshot_changed')
        # NO call to authenticated post, gate, qualify, authority or writer.
        # A cell value, editor email, ACL, trigger owner and worker credential
        # are not verified owner consent bound to this exact comparison.
        return self._finish(cid, rid, record, batch, 'APPROVAL_UNESTABLISHED', basis=basis['comparison_digest'])

    def poll(self):
        state, ledger, tag = self._state()
        _, previous_handoff, _ = self.handoff._state()
        saved = DraftBatch(**state['legacy']['batch'])
        if self.uncertain:
            rid, expected = self.uncertain
            observed = ledger['requests'].get(rid, {}).get('status')
            if observed in LABELS and (expected == 'ATTEMPTING' or observed == expected):
                self.uncertain = None  # exact terminal read-back only
        pending = [rid for rid, r in ledger['requests'].items() if r['status'] in {'ATTEMPTING', 'UNKNOWN'}]
        previous_pending = [rid for rid, r in previous_handoff['requests'].items() if r['status'] == 'ATTEMPTING']
        if pending or self.uncertain or previous_pending or self.handoff.uncertain:
            rid = pending[0] if pending else self.uncertain[0] if self.uncertain else previous_pending[0] if previous_pending else 'unavailable'
            for cid in self.surface.contract['cases']:
                try: command = self.surface.command(cid)
                except StateError: continue
                _, record = self._new(cid, command, digest(None))
                record['status'] = 'UNKNOWN'
                self._project(cid, record, saved, ledger)
            return [dict(self.inspect(rid), status='UNKNOWN')]
        try:
            first = self.surface.read()
        except Exception:
            return [dict(status='WAITING', reason='読取通信待ち・依頼は未受付',
                         approval_established=False, accounting_permission=False,
                         writer_dispatches=0, authority_dispatches=0)]
        try:
            core = self.surface.core(first)
            batch = self.handoff.reader.read()
            if core != self.surface.core() or batch.snapshot_digest != digest(core):
                fail('concurrent_sheet')
        except StateError:
            # Invalid structure/identity must not adopt inputs. Write only an
            # error projection for unambiguous IDs using the last valid batch.
            results = []
            for cid in self.surface.contract['cases']:
                try:
                    command = self.surface.command(cid, first)
                except StateError:
                    continue  # cannot map duplicate/missing IDs; never guess
                if command == IDLE:
                    continue
                rid, record = self._new(cid, command, digest(first))
                existing = ledger['requests'].get(rid)
                try:
                    results.append(self.inspect(rid) if existing else self._finish(cid, rid, record, saved,
                        'CONFLICT', 'Sheet構造・案件対応・原本版が一致しません。入力を採用していません。'))
                except Exception:
                    self.uncertain = self.uncertain or (rid, 'CONFLICT')
                    return self.poll()
            return results
        except Exception:
            return [dict(status='WAITING', reason='独立原本・nativeの読取通信待ち・依頼は未受付',
                         approval_established=False, accounting_permission=False,
                         writer_dispatches=0, authority_dispatches=0)]
        results = []
        for cid in self.surface.contract['cases']:
            command = self.surface.command(cid, first)
            _, previous_handoff, _ = self.handoff._state()
            if self.handoff.uncertain or self.handoff._unresolved(previous_handoff):
                return self.poll()
            existing_confirmation = [r for r in previous_handoff['requests'].values()
                if r['case_id'] == cid and r['status'] == 'FINANCIAL_BOUNDARY_STOPPED']
            if existing_confirmation:
                _, record = self._new(cid, command, digest(None))
                record.update(status='FINANCIAL_BOUNDARY_STOPPED', basis=existing_confirmation[0]['sheet_snapshot'])
                self._project(cid, record, saved, ledger)
                results.append(dict(status='FINANCIAL_BOUNDARY_STOPPED', case_id=cid,
                    previous_owner_confirmation=True, approval_established=False,
                    accounting_permission=False, writer_dispatches=0, authority_dispatches=0))
                continue
            if command == IDLE:
                continue
            state, ledger, tag = self._state()
            if any(r['status'] in {'ATTEMPTING', 'UNKNOWN'} for r in ledger['requests'].values()):
                return self.poll()
            rid, _ = self._new(cid, command, batch.snapshot_digest)
            if rid in ledger['requests']:
                record = ledger['requests'][rid]
                if (record['sequence_accepted'] and int(command.split()[1]) < self._sequence(ledger, cid)):
                    rejected = dict(record, status='CONFLICT', reason='古い比較依頼です。次の比較版を選択してください。')
                    self._project(cid, rejected, batch, ledger)
                    results.append(dict(self.inspect(rid),status='CONFLICT'));continue
                self._project(cid, record, batch, ledger)
                results.append(self.inspect(rid)); continue
            try:
                if re.fullmatch(r'比較 [1-9][0-9]{0,2}', command):
                    result = self._compare(cid, command, batch, batch.snapshot_digest, ledger, tag)
                elif re.fullmatch(r'確定 [a-f0-9]{8}', command):
                    result = self._confirm(cid, command, batch, batch.snapshot_digest, ledger, tag)
                else:
                    _, record = self._new(cid, command, batch.snapshot_digest)
                    result = self._finish(cid, rid, record, batch, 'CONFLICT', '依頼値が不正です。標準の選択肢を使ってください。')
                results.append(result)
            except Exception:
                # Never automatically repeat an uncertain metadata/journal CAS.
                self.uncertain = self.uncertain or (rid, 'ATTEMPTING')
                return self.poll()
        return results
