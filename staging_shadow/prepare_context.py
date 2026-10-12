"""Generate a separate minimal staging bundle; never edit production sources."""
import ast
from hashlib import sha256
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
NATIVE_APP = ('__init__.py', 'drive_run_state.py', 'conditional_drive_state_v2.py',
    'human_general_auth_transport.py', 'human_general_authority.py', 'page_receipt_model.py',
    'models.py', 'pdf_page_identity.py', 'receipt_validation.py')
UNIFIED_APP = ('unified_receipt_shadow.py', 'unified_receipt_purposes.py', 'unified_receipt_review.py')


def derive_storage(source):
    original = source
    source = source.replace('from google.api_core.exceptions import FailedPrecondition, NotFound',
        'from staging_shadow.memory_cas import FailedPrecondition, NotFound')
    source = source.replace('from google.cloud.firestore_v1 import LastUpdateOption',
        'from staging_shadow.memory_cas import LastUpdateOption')
    def body(value):
        return ast.dump(ast.Module(body=[n for n in ast.parse(value).body
            if not isinstance(n, (ast.Import, ast.ImportFrom))], type_ignores=[]))
    assert body(source) == body(original), 'native_auth_or_CAS_body_changed'
    assert 'google.cloud' not in source and 'google.api_core' not in source
    return source


def prepare(destination):
    destination = Path(destination).resolve()
    if destination.exists(): raise ValueError('staging_destination_must_be_new')
    if not destination.is_relative_to(ROOT) and not destination.is_relative_to(ROOT.parent):
        raise ValueError('staging_destination_outside_workspace')
    destination.mkdir(parents=True)
    native = destination/'native-source'
    spec = importlib.util.spec_from_file_location('fixed_native_bundle', ROOT/'auth-session/compose.py')
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    module.compose(native)  # Hash-fixed git show + strict patch; no archive expansion.
    app = destination/'bundle/app'; host = destination/'bundle/services/human_general'
    stage = destination/'bundle/staging_shadow'
    app.mkdir(parents=True); host.mkdir(parents=True); stage.mkdir(parents=True)
    (host.parent/'__init__.py').write_text('', encoding='utf-8')
    (host/'__init__.py').write_text('', encoding='utf-8')
    for name in NATIVE_APP: shutil.copyfile(native/'app'/name, app/name)
    for name in UNIFIED_APP: shutil.copyfile(ROOT/'app'/name, app/name)
    shutil.copytree(ROOT/'app/unified_shadow_templates', app/'unified_shadow_templates', ignore=shutil.ignore_patterns('__pycache__'))
    shutil.copyfile(native/'services/human_general/web.py', host/'web.py')
    derived = {}
    for name in ('backend.py', 'shared_login.py'):
        raw = (native/'services/human_general'/name).read_text('utf-8')
        result = derive_storage(raw)
        (host/name).write_text(result, encoding='utf-8', newline='\n')
        derived[name] = {'original_sha256':sha256(raw.encode()).hexdigest(),
            'staging_sha256':sha256(result.encode()).hexdigest(), 'non_import_AST_unchanged':True}
    # Original native make_application still calls configured_runtime. Only this
    # separate staging bundle supplies an explicit memory factory at composition.
    (host/'runtime.py').write_text('def configured_runtime():\n    raise RuntimeError("staging_factory_not_composed")\n', encoding='utf-8')
    for name in ('__init__.py', 'config.py', 'memory_cas.py', 'runtime.py', 'smoke.py', 'serve.py'):
        shutil.copyfile(ROOT/'staging_shadow'/name, stage/name)
    for name in ('Dockerfile', 'requirements.lock'):
        shutil.copyfile(ROOT/'staging_shadow'/name, destination/name)
    manifest = {'source_revision':subprocess.check_output(['git','rev-parse','HEAD'], cwd=ROOT, text=True).strip(),
        'derived_storage':derived, 'files':{p.relative_to(destination/'bundle').as_posix():sha256(p.read_bytes()).hexdigest()
            for p in sorted((destination/'bundle').rglob('*')) if p.is_file()}}
    (destination/'bundle/staging-manifest.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')
    print('STAGING CONTEXT: native hash check + authentication/CAS body identity passed')
    return destination


if __name__ == '__main__': prepare(sys.argv[1])
