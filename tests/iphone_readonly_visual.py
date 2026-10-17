"""Export only authenticated synthetic GET HTML; never export cookies/state.

Run with --output <private-dir>, then iphone_readonly_browser.cjs. The browser
checks exported server responses, not real OAuth/iPhone/Drive navigation.
"""
from copy import deepcopy
import argparse
import json
from pathlib import Path
import socket
import sys
import tempfile

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT/'tests')]
from iphone_readonly_fixtures import ReadonlyRig
from app.iphone_readonly_host import PREFIX
from app.unified_receipt_review import digest
from unified_shadow_fixtures import Document


def export(output):
    output.mkdir(parents=True, exist_ok=True)
    def forbidden(*a, **kw): raise AssertionError('visual_side_effect_forbidden')
    with pytest.MonkeyPatch.context() as patch, tempfile.TemporaryDirectory(prefix='iphone-5a-') as temp:
        patch.setattr(socket.socket, 'connect', forbidden)
        patch.setattr(socket.socket, 'connect_ex', forbidden)
        r = ReadonlyRig(patch, Path(temp)); h = r.h
        assert h.login().status_code == 303
        before = deepcopy(h.db.data), r.journal.path.read_bytes(), r.bundle_path.read_bytes()
        patch.setattr(Document, 'create', forbidden); patch.setattr(Document, 'update', forbidden)
        patch.setattr(r.journal, 'save', forbidden)
        patch.setattr(h.f.port, 'observe', forbidden); patch.setattr(h.f.port, 'execute', forbidden)
        paths = {'list':'', **{cid:'/cases/'+cid for cid in [
            'SYN-G101','SYN-M101','SYN-P201','SYN-G103','SYN-H301']}}
        views = []
        for name, path in paths.items():
            response = h.get(PREFIX+path); assert response.status_code == 200
            filename = name+'.html'; (output/filename).write_bytes(response.data)
            views.append(dict(name=name, file=filename, html_digest=digest(response.get_data(as_text=True)),
                              cases=8 if name == 'list' else 1))
        assert before == (h.db.data, r.journal.path.read_bytes(), r.bundle_path.read_bytes())
        assert h.f.native_calls == h.f.accounting_appends == h.f.ai_sends == h.f.source_moves == 0
        result = dict(views=views, evidence='authenticated Flask GET HTML; synthetic only',
                      get_writes=0, native_observations=0, authority_updates=0, accounting_writes=0, ai_sends=0, source_moves=0)
        (output/'manifest.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
        print(json.dumps(dict(exported=len(views), get_writes=0, native_observations=0)))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(); parser.add_argument('--output', type=Path, required=True)
    export(parser.parse_args().output)
