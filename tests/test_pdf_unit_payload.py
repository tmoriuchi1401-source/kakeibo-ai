from copy import deepcopy
from hashlib import sha256
from io import BytesIO
from unittest.mock import Mock

from PIL import Image
import pytest
from app import pdf_unit_payload as payload, receipt_pdf_units as pdf
from app.pdf_bounded_rendering import WorkBudget, RenderHold
from app.pdf_production_authority import DrivePdfAuthority
from app.drive_run_state import StateError
from pdf_production_test_support import context,request
from test_receipt_pdf_units import local_ocr


def setup(group=False):
    g,live,drive=context();view=g.display('drive-source-id')
    if group:
        g.review(request(view,operation='edit',partition=[[1,2]]))
        view=g.view(next(iter(g.store.load()['records'].values())))
    g.review(request(view,number=2))
    s=DrivePdfAuthority(g.store,lambda _:live.content).current('drive-source-id').units[0]
    return live.content,s,live.observations


@pytest.mark.parametrize('group',[False,True])
def test_fresh_rgb_png_only_and_budget_held_across_downstream(group,local_ocr):
    content,s,obs=setup(group);budget=WorkBudget()
    with payload.rendered_unit(content,s,obs,budget) as (png,digest):
        assert budget.live_pages==1 and digest==sha256(png).hexdigest()
        assert b'PRIVATE_ATTACHMENT' not in png and not png.startswith(b'%PDF')
        with Image.open(BytesIO(png)) as im:
            assert im.mode=='RGB' and im.info=={} and im.size==(216,432 if group else 216)
        assert budget.render_calls==len(s['page_numbers'])
    assert budget.live_pages==0 and budget.peak_live_pages==1


def test_renderer_encoding_differences_do_not_change_source_or_unit_authority(local_ocr,monkeypatch):
    content,s,obs=setup();original=payload._render_png
    def changed(*args,**kwargs):
        png=original(*args,**kwargs);out=BytesIO()
        with Image.open(BytesIO(png)) as image:image.save(out,format='PNG',compress_level=0)
        return out.getvalue()
    monkeypatch.setattr(payload,'_render_png',changed)
    with payload.rendered_unit(content,s,obs,WorkBudget()) as (png,h):
        assert h!=obs.pages[0].page_hash and s['member_page_identities']!=[h]


def test_sdk_or_writer_error_is_not_relabelled_as_privacy_retry(local_ocr):
    content,s,obs=setup();budget=WorkBudget()
    with pytest.raises(StateError,match='synthetic_write_unknown'):
        with payload.rendered_unit(content,s,obs,budget):raise StateError('synthetic_write_unknown')
    assert budget.live_pages==0


@pytest.mark.parametrize('change',['source','page','human','automatic'])
def test_wrong_source_page_or_restrictive_kind_never_renders(change,local_ocr,monkeypatch):
    content,s,obs=setup();s=deepcopy(s)
    if change=='source':content+=b'changed'
    if change=='page':s['member_page_identities']=['0'*64]
    if change=='human':s['human_classifications']=['medical']
    if change=='automatic':s['automatic_classifications']=['sensitive_unknown']
    render=Mock(side_effect=AssertionError('never render'));monkeypatch.setattr(payload,'_render_png',render)
    with pytest.raises(payload.PayloadHold):
        with payload.rendered_unit(content,s,obs,WorkBudget()):pass
    render.assert_not_called()


def test_cumulative_budget_is_shared_with_observation_not_reset_per_payload(local_ocr):
    content,s,obs=setup();budget=WorkBudget();budget.render_calls=100
    with pytest.raises(RenderHold,match='total_work_budget'):
        with payload.rendered_unit(content,s,obs,budget):pass
    assert budget.render_calls==100 and budget.live_pages==0
