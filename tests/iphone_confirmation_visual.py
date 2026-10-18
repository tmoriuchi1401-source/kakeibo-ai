"""Export authenticated synthetic responses; no listener, real OAuth or Drive."""
from copy import deepcopy
import argparse
import json
from pathlib import Path
import socket
import sys
import tempfile

import pytest

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/'tests')]
from iphone_confirmation_fixtures import Rig
from app.iphone_confirmation_host import PREFIX


def export(output):
    output.mkdir(parents=True,exist_ok=True)
    with pytest.MonkeyPatch.context() as patch,tempfile.TemporaryDirectory(prefix='confirmation-visual-') as temp:
        def no_network(*a,**kw):raise AssertionError('live_network_forbidden')
        patch.setattr(socket.socket,'connect',no_network);patch.setattr(socket.socket,'connect_ex',no_network)
        r=Rig(patch,Path(temp));assert r.h.login().status_code==303
        views=[]
        def save(name,path,status=200):
            before=r.doc.payload,r.doc.writes,deepcopy(r.h.db.data)
            calls=sum(n['context'].source_reader.calls for n in r.native.values())
            response=r.h.get(PREFIX+path);assert response.status_code==status
            assert before==(r.doc.payload,r.doc.writes,r.h.db.data)
            assert calls==sum(n['context'].source_reader.calls for n in r.native.values())
            file=name+'.html';(output/file).write_bytes(response.data)
            views.append(dict(name=name,file=file,status=status))
        save('list','')
        save('general','/cases/SYN-G101');save('medical','/cases/SYN-M101');save('pdf','/cases/SYN-P201')
        save('held-general','/cases/SYN-G103');save('held-history','/cases/SYN-H301')
        assert r.save(entered=False).status_code==200
        save('partial-edit','/cases/SYN-G101')
        saved=r.save();assert saved.status_code==200
        (output/'save-success.json').write_text(json.dumps(saved.json),encoding='utf8')
        save('saved-edit','/cases/SYN-G101')
        save('reconfirm','/cases/SYN-G101/reconfirm')
        response,payload,csrf=r.accept();assert response.status_code==200
        save('result','/results/'+payload['request_id'])
        r.h.now+=600;save('expired','/cases/SYN-G101',410);r.h.now-=600
        r.saved.bundle_digest='f'*64;save('conflict','/cases/SYN-G101',409)
        (output/'ui.js').write_text((ROOT/'app/unified_shadow_templates/iphone_confirmation.js').read_text(encoding='utf8'),encoding='utf8')
        (output/'manifest.json').write_text(json.dumps(dict(views=views,evidence='authenticated Flask HTML; browser uses offline mock transport',
            get_writes=0,get_native_observations=0,accounting_writes=0,native_authority_updates=0,source_moves=0,ai_sends=0)),encoding='utf8')
        print(json.dumps(dict(views=len(views),get_writes=0,get_native_observations=0)))


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,required=True)
    export(parser.parse_args().output)
