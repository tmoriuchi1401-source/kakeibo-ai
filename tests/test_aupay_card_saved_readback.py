"""GET-only post-save observations; real existing writer is never retried."""
from copy import deepcopy
from datetime import datetime,timedelta
import hashlib
import json
from types import SimpleNamespace

import pytest

from app.aupay_card_saved_readback import SavedReadback,RepairReadbacks,standard
from app.aupay_card_window_preview import ReadOnlyState,Stop,digest
from app.aupay_card_reconciliation_diagnostics import ReconciliationDiagnostics
from app.drive_run_state import StateBinding
from test_aupay_card_reconciliation_apply import case,C0
from app import aupay_card_reconciliation_apply as repair


class Source:
    def __init__(self,raw):self.payload=raw;self.writes=[]
    def write(self,raw):self.writes.append(raw);self.payload=raw


class Drive:
    def __init__(self,source,binding,mode='stable',metadata=None):
        self.source,self.binding,self.mode=source,binding,mode
        self.original=source.payload;self.start=len(source.writes)
        self.calls=[];self.meta_reads={};self.now=0.;self.sleeps=[]
        self.base=dict(id=binding.file_id,parents=[binding.folder_id],trashed=False,mimeType='application/json',version='100',modifiedTime='2026-11-27T00:00:00+00:00')
        if metadata:self.base.update(metadata)
    def offset(self):return len(self.source.writes)-self.start
    def raw(self,index):return self.original if index==0 else self.source.writes[self.start+index-1]
    def meta(self,index):
        raw=self.raw(index);v=deepcopy(self.base)
        v.update(version=str(int(self.base['version'])+2*index),size=str(len(raw)),md5Checksum=hashlib.md5(raw).hexdigest(),headRevisionId=f'{self.binding.file_id}-r{index}')
        if index:v['modifiedTime']=(datetime.fromisoformat(self.base['modifiedTime'])+timedelta(seconds=index)).isoformat()
        return v
    def request(self,fn):
        owner=self
        class Request:
            method='GET'
            def execute(self,**kwargs):owner.calls.append('GET');return fn()
        return Request()
    def files(self):return self
    def revisions(self):return self
    def get(self,**kwargs):
        assert kwargs['fileId']==self.binding.file_id
        def value():
            offset=self.offset();count=self.meta_reads.get(offset,0);self.meta_reads[offset]=count+1
            m=self.meta(offset)
            if offset==1:
                if self.mode=='transient' and count==0:m=self.meta(0)
                elif self.mode=='persistent' and count%2==0:
                    m.update(size=self.meta(0)['size'],md5Checksum=self.meta(0)['md5Checksum'])
                elif self.mode=='target':m['mimeType']='SECRET'
                elif self.mode=='folder':m['parents']=['SECRET']
                elif self.mode=='id':m['id']='SECRET'
                elif self.mode=='unexpected_field':m['SECRET']='SECRET'
                elif self.mode=='checksum':m['md5Checksum']='SECRET'
                elif self.mode=='other_writer':m.update(version=str(int(self.base['version'])+4),headRevisionId='other-r2')
                elif self.mode=='rollback':m.update(version=str(int(self.base['version'])+6),headRevisionId='rollback-r3')
            if 'headRevisionId' not in kwargs['fields']:m.pop('headRevisionId')
            return m
        return self.request(value)
    def get_media(self,**kwargs):
        assert kwargs['fileId']==self.binding.file_id
        def value():
            if self.offset()==1 and self.mode=='deadline':self.now=1000.
            return b'SECRET foreign bytes' if self.offset()==1 and self.mode=='other_writer' else self.source.payload
        return self.request(value)
    def list(self,**kwargs):
        assert kwargs['fileId']==self.binding.file_id
        def value():
            rows=[]
            for i in range(self.offset()+1):
                m=self.meta(i);rows.append(dict(id=m['headRevisionId'],modifiedTime=m['modifiedTime'],size=m['size'],md5Checksum=m['md5Checksum']))
            if self.mode=='rollback' and self.offset()==1:
                rows += [dict(id='other-r2'),dict(id='rollback-r3')]
            return dict(revisions=rows)
        return self.request(value)
    def sleep(self,seconds):self.sleeps.append(seconds);self.now+=seconds


def fixture(mode='stable',guard=lambda:None):
    source=Source(b'old bytes');binding=StateBinding('au_pay_card_gmail','sid','folder','card')
    drive=Drive(source,binding,mode);reader=ReadOnlyState(drive,binding);trace=ReconciliationDiagnostics()
    pin=dict(sha256=hashlib.sha256(source.payload).hexdigest(),metadata_digest=digest(standard(drive.meta(0))))
    policy=SavedReadback(reader,pin,guard=guard,deadline=100,diagnostics=trace,clock=lambda:drive.now,sleeper=drive.sleep)
    return source,drive,policy,trace


@pytest.mark.parametrize('mode,attempts',[('stable',1),('transient',2)])
def test_temporary_metadata_mismatch_recovers_using_only_bounded_gets(mode,attempts):
    source,drive,policy,trace=fixture(mode)
    ticket=policy.begin(source.payload);source.write(b'new bytes')
    assert ticket.verify(b'new bytes')==b'new bytes' and len(source.writes)==1
    assert trace.report()['readback_attempts']==attempts and len(drive.sleeps)==attempts-1
    assert set(drive.calls)=={'GET'}
    assert policy.read()==b'new bytes'
    with pytest.raises(Stop):ticket.verify(b'new bytes')


@pytest.mark.parametrize('mode,code',[('persistent','saved_readback_unstable'),('target','saved_readback_target_changed'),
    ('folder','saved_readback_target_changed'),('id','saved_readback_target_changed'),('unexpected_field','saved_readback_target_changed'),
    ('checksum','saved_readback_checksum_changed'),('other_writer','saved_readback_content_changed'),
    ('rollback','saved_readback_revision_changed'),('deadline','repair_timeout')])
def test_unexplained_or_continuing_changes_never_change_expected_value(mode,code):
    source,drive,policy,trace=fixture(mode)
    ticket=policy.begin(source.payload);source.write(b'new bytes')
    with pytest.raises(Stop,match=code) as error:ticket.verify(b'new bytes')
    report=trace.report(error.value)
    assert 'SECRET' not in json.dumps(report) and len(source.writes)==1
    assert policy.pin['sha256']==hashlib.sha256(b'old bytes').hexdigest()
    if mode=='persistent':assert report['readback_attempts']==3 and len(drive.sleeps)==2


def test_expected_hash_mismatch_is_not_retried_or_updated():
    source,drive,policy,trace=fixture();ticket=policy.begin(source.payload);source.write(b'new bytes')
    with pytest.raises(Stop,match='content_changed'):ticket.verify(b'wrong expectation')
    assert not drive.sleeps and len(source.writes)==1


def test_general_observe_still_single_shot_and_only_metadata_names_escape():
    source,drive,policy,trace=fixture('transient');source.write(b'new bytes')
    with pytest.raises(Stop,match='state_changed_during_read') as error:policy.reader.observe()
    report=trace.report(error.value)
    assert set(report['metadata_mismatch_fields'])<= {'version','size','md5Checksum','modifiedTime'}
    assert not drive.sleeps and 'new bytes' not in json.dumps(report)


def install_readbacks(p,mode):
    native=Drive(p.nt,p.cb,mode);parent=Drive(p.pt,p.pb)
    for prefix,drive in (('native',native),('parent',parent)):
        p.record[prefix+'_before_metadata_digest']=digest(standard(drive.meta(0)))
    p.args['record_sha256']=repair.sha(repair.encode(p.record));trace=ReconciliationDiagnostics()
    bundle=RepairReadbacks(ReadOnlyState(native,p.cb),ReadOnlyState(parent,p.pb),p.record,guard=p.args['guard'],deadline=100,
        diagnostics=trace,clock=lambda:native.now,sleeper=native.sleep)
    p.args.update(readbacks=bundle,native_read=bundle.sessions['native'].read,parent_read=bundle.sessions['parent'].read,diagnostics=trace)
    return native,parent,trace


@pytest.mark.parametrize('mode',['stable','transient'])
def test_existing_writer_counts_do_not_increase_after_get_recovery(tmp_path,mode):
    p=case(tmp_path);native,parent,trace=install_readbacks(p,mode)
    result=repair.execute_reconciliation(**p.args)
    assert result['success'] and p.db.calls==1 and len(p.nt.writes)==3 and len(p.pt.writes)==2
    assert result['written']==2 and result['import_yen']==4506
    assert set(native.calls+parent.calls)=={'GET'}


@pytest.mark.parametrize('mode',['persistent','other_writer','rollback','deadline','target'])
def test_bad_first_save_readback_cannot_reach_financial_writer(tmp_path,mode):
    p=case(tmp_path);native,parent,trace=install_readbacks(p,mode)
    with pytest.raises(Stop):repair.execute_reconciliation(**p.args)
    assert p.db.calls==0 and len(p.nt.writes)==1 and not p.pt.writes
    assert json.loads(p.nt.payload)['phase']=='pending'
    assert repair._rows(json.loads(p.nt.payload),'recurring.sqlite3','recurring_checkpoint')[0]['successful_window_end']==C0


def test_guard_or_deadline_is_never_bypassed_between_read_attempts():
    checks=[]
    def guard():
        checks.append(1)
        if len(checks)>2:raise Stop('repair_competing_run')
    source,drive,policy,trace=fixture('persistent',guard)
    ticket=policy.begin(source.payload);source.write(b'new bytes')
    with pytest.raises(Stop,match='repair_competing_run'):ticket.verify(b'new bytes')
    assert len(source.writes)==1


def test_unknown_write_response_is_not_a_reason_to_retry_write_or_get(tmp_path):
    p=case(tmp_path);native,parent,trace=install_readbacks(p,'stable');p.nt.mode='unknown'
    with pytest.raises(TimeoutError):repair.execute_reconciliation(**p.args)
    assert len(p.nt.writes)==1 and not p.pt.writes and p.db.calls==0
    assert not native.sleeps and native.meta_reads.get(1,0)==0


@pytest.mark.parametrize('malicious',[['SECRET','version'],123,'SECRET'])
def test_diagnostics_accept_only_allowlisted_metadata_names(malicious):
    error=Stop('state_changed_during_read');error.metadata_fields=malicious
    report=ReconciliationDiagnostics().report(error)
    assert 'SECRET' not in json.dumps(report) and report.get('metadata_mismatch_fields',[]) in ([],['version'])
