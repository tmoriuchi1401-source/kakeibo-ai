"""Main registration must exclude page records from whole-file processing."""
from copy import deepcopy
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import Mock
import yaml

from app.receipt_confirmation import ReceiptConfirmation
from app.receipt_confirmation_archive import archive_confirmations
from tests.test_receipt_confirmation import medical


def test_whole_file_worker_cannot_see_or_render_a_pdf_page_record():
    review,store,db,verify,source=medical()
    key=next(iter(review.items));old=deepcopy(store.value['confirmation_items'][key])
    page=deepcopy(old);page['source']['pdf_page']={'original_file_id':'synthetic-parent','page_number':1}
    store.value['confirmation_items']['synthetic-page']=page
    filtered=ReceiptConfirmation(store,db,verify)
    assert list(filtered.items)==[key] and filtered.items[key]==old
    before=deepcopy(store.value);filtered.render()
    assert all(row[0]!= 'synthetic-page' for row in db.rows['領収書確認'])
    assert store.value==before and filtered.apply_confirmations()==0


def test_even_a_page_adapter_cannot_move_the_parent_from_page_completion():
    page={'source':{'pdf_page':{'original_file_id':'synthetic-parent','page_number':1}}}
    review=SimpleNamespace(items={'synthetic-page':page})
    drive=Mock(side_effect=AssertionError('No source move'));download=Mock()
    assert archive_confirmations(review,'synthetic-inbox','synthetic-processed',drive,download)==0
    assert not drive.mock_calls and not download.mock_calls


def test_registered_entry_is_manual_only_default_preflight_and_no_ai_or_move():
    root=Path(__file__).parents[1]
    text=(root/'.github/workflows/pdf-page-manual-canary.yml').read_text(encoding='utf-8')
    workflow=yaml.load(text,Loader=yaml.BaseLoader)
    assert set(workflow['on'])=={'workflow_dispatch'}
    assert workflow['concurrency']=={'group':'kakeibo-production','cancel-in-progress':'false','queue':'max'}
    assert workflow['on']['workflow_dispatch']['inputs']['mode']['default']=='preflight'
    job=workflow['jobs']['canary'];env=job['steps'][-1]['env']
    assert "github.sha == vars.KAKEIBO_VALIDATED_MAIN_SHA" in job['if']
    assert "github.ref == 'refs/heads/main'" in job['if']
    assert env['PDF_ARCHIVE_ENABLED']==env['PDF_AUTOMATIC_ENABLED']=='false'
    assert 'GEMINI_API_KEY' not in text and 'PROCESSED' not in text and 'upload-artifact' not in text
    checkout=next(s for s in job['steps'] if s.get('uses','').startswith('actions/checkout'))
    assert checkout['with']['ref']=='${{ inputs.approved_sha }}'
    assert checkout['with']['persist-credentials']=='false'
