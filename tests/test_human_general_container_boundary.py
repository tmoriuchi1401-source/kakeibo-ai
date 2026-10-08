"""The deployable import closure cannot contain a receipt/Medical writer."""
import ast
from pathlib import Path
import shutil
import subprocess
import sys


def test_whitelisted_container_can_import_all_confirmation_runtimes(tmp_path):
    root=Path(__file__).resolve().parents[1]
    docker=(root/'services/human_general/Dockerfile').read_text()
    copied=[]
    for line in docker.splitlines():
        if line.startswith('COPY app/'):
            copied.extend(line.split()[1:-1])
    (tmp_path/'app').mkdir()
    for name in copied:
        shutil.copyfile(root/name,tmp_path/name)
    shutil.copytree(root/'services/human_general',tmp_path/'services/human_general',ignore=shutil.ignore_patterns('__pycache__'))
    script="""
import sys
from services.human_general.page_router import PageRouter
from services.human_general.reconciliation_runtime import LiveReconciliationRuntime
from services.human_general.web import create_app
for name in sys.modules:
    assert not any(word in name for word in ('gemini','receipt_pipeline','pdf_page_medical','receipt_confirmation_production','google_clients'))
print('confirmation_container_imports_verified')
"""
    result=subprocess.run([sys.executable,'-c',script],cwd=tmp_path,capture_output=True,text=True)
    assert result.returncode==0,result.stderr
    assert result.stdout.strip()=='confirmation_container_imports_verified'


def test_readonly_wire_contract_matches_existing_sheet_and_source():
    from services.human_general import reconciliation_readers as reader
    from app.pdf_page_review import SCHEMA
    from app.pdf_unit_readonly_analysis import SOURCE_KEY
    assert reader.SCHEMA==SCHEMA and reader.SOURCE_KEY==SOURCE_KEY
