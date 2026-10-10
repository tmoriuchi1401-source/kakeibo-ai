"""Hash-pinned delta for the ALREADY deployed, minimal confirmation container.

Does not merge PR #91, deploy, fetch Secrets, provision state, or run a writer.
Tests extract a pinned historical tree on a credential-free runner. Builds
contain ONLY the deployed Docker whitelist plus the reviewed session delta.
"""
import argparse
from hashlib import sha256
from io import BytesIO
import json
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import sys
import tarfile
import tempfile

HERE=Path(__file__).resolve().parent
ROOT=HERE.parent
MANIFEST=json.loads((HERE/'baseline.json').read_text('utf8'))
SHA=MANIFEST['baseline_commit']


def git(*args):
    return subprocess.check_output(['git','-c','safe.directory='+ROOT.as_posix(),'-C',str(ROOT),*args])


def checked_path(name):
    path=PurePosixPath(name)
    if (not name or path.is_absolute() or '..' in path.parts
            or '\\' in name or ':' in name or path.as_posix() != name):
        raise ValueError('bundle_path_invalid')
    return path


def normalized(path):return path.read_bytes().replace(b'\r\n',b'\n')


def compose(output,*,test_tree=False):
    output=Path(output).resolve()
    output.mkdir(parents=True,exist_ok=False)
    if test_tree:
        with tarfile.open(fileobj=BytesIO(git('archive',SHA))) as archive:
            archive.extractall(output,filter='data')
    for name,expected in MANIFEST['baseline_files'].items():
        checked_path(name)
        data=git('show',SHA+':'+name)
        if sha256(data).hexdigest()!=expected:raise ValueError('deployed_baseline_hash_changed')
        destination=output/name
        destination.parent.mkdir(parents=True,exist_ok=True)
        destination.write_bytes(data)
    patch=normalized(HERE/'existing-service.patch')
    if sha256(patch).hexdigest()!=MANIFEST['patch_sha256']:raise ValueError('bundle_patch_changed')
    paths={line.split()[2][2:] for line in patch.decode().splitlines() if line.startswith('diff --git ')}
    if paths!=set(MANIFEST['changed_files']) or not paths<=set(MANIFEST['baseline_files']):
        raise ValueError('bundle_scope_changed')
    for name in paths:checked_path(name)
    # No repository index, 3-way fallback, fuzzy context or unsafe paths.
    subprocess.run(['git','apply','--check','--whitespace=error','-'],cwd=output,input=patch,check=True)
    subprocess.run(['git','apply','--whitespace=error','-'],cwd=output,input=patch,check=True)
    for name,expected in MANIFEST['changed_files'].items():
        if sha256(normalized(output/name)).hexdigest()!=expected:raise ValueError('bundle_result_changed')
    for name,expected in MANIFEST['added_files'].items():
        checked_path(name)
        data=normalized(HERE/Path(name).name)
        if sha256(data).hexdigest()!=expected:raise ValueError('bundle_addition_changed')
        (output/name).write_bytes(data)
    for name,expected in MANIFEST.get('repository_files',{}).items():
        checked_path(name)
        if name not in {'app/pdf_intake_authority.py','app/pdf_intake_registry.py'}:
            raise ValueError('container_repository_scope_invalid')
        data=normalized(ROOT/name)
        if sha256(data).hexdigest()!=expected:raise ValueError('container_repository_file_changed')
        (output/name).write_bytes(data)
    if test_tree:
        shutil.copyfile(HERE/'shared_login_contracts.py',output/'tests/test_google_shared_login.py')
        shutil.copyfile(HERE/'generic_contracts.py',output/'tests/test_registered_intake.py')
        shutil.copyfile(ROOT/'app/receipt_plan/drive.py',output/'app/receipt_plan/drive.py')
    files={p.relative_to(output).as_posix():sha256(normalized(p)).hexdigest()
           for p in output.rglob('*') if p.is_file()}
    if not test_tree and set(files)!=set(MANIFEST['baseline_files'])|set(MANIFEST['added_files'])|set(MANIFEST.get('repository_files',{})):
        raise ValueError('container_scope_changed')
    return files


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--output',type=Path)
    parser.add_argument('--test',action='store_true')
    args=parser.parse_args()
    if args.test:
        with tempfile.TemporaryDirectory(prefix='kakeibo-shared-session-') as temporary:
            destination=Path(temporary)/'fixture'
            compose(destination,test_tree=True)
            subprocess.run([sys.executable,'-m','pytest','-q','--tb=short',
                'tests/test_registered_intake.py','tests/test_google_shared_login.py','tests/test_human_general_http.py',
                'tests/test_human_general_auth_transport.py','tests/test_human_general_real_page.py',
                'tests/test_receipt_item_confirmation.py','tests/test_receipt_reconciliation_auth_transport.py',
                'tests/test_receipt_retention_audit.py','tests/test_human_general_container_boundary.py',
                'tests/test_p14_plan_only.py','tests/test_receipt_unit_plan.py'],cwd=destination,check=True)
            print(json.dumps({'status':'offline_session_and_existing_authority_contracts_passed','live_write':0}))
    elif args.output:
        files=compose(args.output)
        print(json.dumps({'status':'reviewed_container_composed','baseline':SHA,'file_count':len(files),
                          'container_digest':sha256(json.dumps(files,sort_keys=True).encode()).hexdigest(),'deployed':False}))
    else:parser.error('use --test or --output (no deploy interface)')


if __name__=='__main__':main()
