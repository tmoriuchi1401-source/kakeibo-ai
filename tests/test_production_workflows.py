from pathlib import Path
from datetime import datetime, timedelta, timezone
from copy import deepcopy

import yaml

from app.production_run import DEPENDENCIES


ROOT = Path(__file__).resolve().parents[1]


def workflows():
    # BaseLoader leaves 'on' as a string (YAML 1.1 treats it as a boolean).
    return {path.name: yaml.load(path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
            for path in (ROOT / ".github/workflows").glob("*.yml")}


def assert_legacy_confirmation_stop_workflow(workflow):
    expected = deepcopy(workflows()['iphone-synthetic-native-writer-checks.yml'])
    expected['name'] = 'iPhone legacy confirmation connection stop'
    expected['on'] = {'pull_request': {'branches': ['codex/phase5b2b2b2b-synthetic-writer']}}
    expected['concurrency']['group'] = 'kakeibo-iphone-legacy-confirmation-stop-${{ github.ref }}'
    steps = expected['jobs']['synthetic-writer']['steps']
    steps.insert(3, {'name': 'Collect and execute legacy stopping evidence', 'run': (
        'python -m pytest --collect-only -q tests/test_legacy_confirmation_connection_stop.py\n'
        'python -m pytest -vv tests/test_legacy_confirmation_connection_stop.py\n')})
    steps[-1]['run'] = steps[-1]['run'].replace(
        'tests/test_synthetic_existing_writer.py\n',
        'tests/test_synthetic_existing_writer.py tests/test_legacy_confirmation_connection_stop.py\n')
    assert workflow == expected
    source = (ROOT / '.github/workflows/iphone-legacy-confirmation-stop-checks.yml').read_text('utf-8')
    assert all(x not in source for x in ('secrets.', 'vars.', 'environment:'))


def test_legacy_confirmation_stop_ci_is_exactly_isolated():
    assert_legacy_confirmation_stop_workflow(workflows()['iphone-legacy-confirmation-stop-checks.yml'])


def assert_legacy_contract_poc_workflow(workflow):
    expected = deepcopy(workflows()['iphone-legacy-confirmation-stop-checks.yml'])
    assert_legacy_confirmation_stop_workflow(expected)
    expected['name'] = 'iPhone legacy native contract PoC'
    expected['on'] = {'pull_request': {'branches': ['codex/phase5b2b2c-legacy-confirmation']}}
    expected['concurrency']['group'] = 'kakeibo-iphone-legacy-contract-poc-${{ github.ref }}'
    steps = expected['jobs']['synthetic-writer']['steps']
    steps.insert(3, {'name': 'Collect and execute contract PoC', 'run': (
        'python -m pytest --collect-only -q tests/test_legacy_contract_poc.py\n'
        'python -m pytest -vv tests/test_legacy_contract_poc.py\n')})
    steps[-1]['run'] = steps[-1]['run'].replace(
        'tests/test_legacy_confirmation_connection_stop.py\n',
        'tests/test_legacy_confirmation_connection_stop.py staging_shadow/legacy_contract_poc.py tests/test_legacy_contract_poc.py\n')
    assert workflow == expected
    source = (ROOT / '.github/workflows/iphone-legacy-contract-poc-checks.yml').read_text('utf-8')
    assert all(x not in source for x in ('secrets.', 'vars.', 'environment:'))


def test_legacy_contract_poc_ci_is_exactly_isolated():
    assert_legacy_contract_poc_workflow(workflows()['iphone-legacy-contract-poc-checks.yml'])


def assert_native_receipt_v2_workflow(workflow):
    expected = deepcopy(workflows()['iphone-legacy-contract-poc-checks.yml'])
    assert_legacy_contract_poc_workflow(expected)
    expected['name'] = 'iPhone isolated native receipt v2'
    expected['on'] = {'pull_request': {'branches': ['codex/phase5b2b2c1-native-contract-poc']}}
    expected['concurrency']['group'] = 'kakeibo-iphone-native-v2-${{ github.ref }}'
    steps = expected['jobs']['synthetic-writer']['steps']
    steps.insert(3, {'name': 'Collect and execute isolated native v2', 'run': (
        'python -m pytest --collect-only -q tests/test_receipt_confirmation_contract.py\n'
        'python -m pytest -vv tests/test_receipt_confirmation_contract.py\n')})
    steps[-2]['run'] += ' tests/test_receipt_reimport_production.py'
    steps[-1]['run'] = steps[-1]['run'].replace('tests/test_legacy_contract_poc.py\n',
        'tests/test_legacy_contract_poc.py app/receipt_confirmation_contract.py app/receipt_reimport_production.py tests/test_receipt_confirmation_contract.py\n')
    assert workflow == expected
    source = (ROOT / '.github/workflows/iphone-native-receipt-v2-checks.yml').read_text('utf-8')
    assert all(x not in source for x in ('secrets.', 'vars.', 'environment:'))


def test_native_receipt_v2_ci_is_exactly_isolated():
    assert_native_receipt_v2_workflow(workflows()['iphone-native-receipt-v2-checks.yml'])


def assert_synthetic_legacy_writer_workflow(workflow):
    expected = deepcopy(workflows()['iphone-native-receipt-v2-checks.yml'])
    assert_native_receipt_v2_workflow(expected)
    expected['name'] = 'iPhone synthetic legacy confirmation writer'
    expected['on'] = {'pull_request': {'branches': ['codex/phase5b2b2c2-isolated-native-v2']}}
    expected['concurrency']['group'] = 'kakeibo-iphone-legacy-writer-${{ github.ref }}'
    steps = expected['jobs']['synthetic-writer']['steps']
    steps.insert(3, {'name': 'Collect and execute guarded legacy writer', 'run': (
        'python -m pytest --collect-only -q tests/test_synthetic_legacy_writer.py\n'
        'python -m pytest -vv tests/test_synthetic_legacy_writer.py\n')})
    steps[-1]['run'] = steps[-1]['run'].replace('tests/test_receipt_confirmation_contract.py\n',
        'tests/test_receipt_confirmation_contract.py staging_shadow/synthetic_legacy_writer.py tests/test_synthetic_legacy_writer.py\n')
    assert workflow == expected
    source = (ROOT / '.github/workflows/iphone-synthetic-legacy-writer-checks.yml').read_text('utf-8')
    assert all(x not in source for x in ('secrets.', 'vars.', 'environment:'))


def test_synthetic_legacy_writer_ci_is_exactly_isolated():
    assert_synthetic_legacy_writer_workflow(workflows()['iphone-synthetic-legacy-writer-checks.yml'])


def assert_pdf_native_readonly_workflow(workflow):
    assert workflow == {
        'name': 'iPhone PDF native readonly investigation',
        'on': {'pull_request': {'branches': ['codex/phase5b2b2c3-synthetic-legacy-writer']}},
        'permissions': {'contents': 'read'}, 'env': {'UNIFIED_RECEIPT_WRITE_ENABLED': 'false'},
        'concurrency': {'group': 'kakeibo-iphone-pdf-readonly-${{ github.ref }}', 'cancel-in-progress': 'true'},
        'jobs': {'pdf-readonly': {'runs-on': 'ubuntu-24.04', 'timeout-minutes': '10', 'steps': [
            {'uses': 'actions/checkout@v4', 'with': {'ref': '${{ github.event.pull_request.head.sha }}',
                'fetch-depth': '0', 'persist-credentials': 'false'}},
            {'uses': 'actions/setup-python@v5', 'with': {'python-version': '3.12'}},
            {'name': 'Install synthetic test dependencies', 'run': 'pip install -r requirements-test.txt'},
            {'name': 'Collect and execute read-only PDF contract investigation', 'run':
                'python -m pytest --collect-only -q tests/test_pdf_native_readonly_inventory.py\n'
                'python -m pytest -vv tests/test_pdf_native_readonly_inventory.py\n'},
            {'name': 'Reuse pure one-tab draft and workflow boundary regressions', 'run':
                "python -m pytest -q tests/test_iphone_review_drafts.py tests/test_production_workflows.py -k 'not memory_cas_model'"},
            {'name': 'Compile and check whitespace', 'run':
                'python -m compileall -q tests/test_pdf_native_readonly_inventory.py tests/test_production_workflows.py\n'
                'git diff --check\n'},
        ]}},
    }


def test_pdf_native_readonly_ci_is_exactly_isolated():
    assert_pdf_native_readonly_workflow(workflows()['iphone-pdf-native-readonly-checks.yml'])


def test_all_existing_production_entries_share_top_level_lock_and_main_guard():
    for name, workflow in workflows().items():
        if name == 'r2-2-m5-m6.yml':
            from test_ledger_execution_static import assert_workflow
            assert_workflow(workflow)
            continue
        if name == 'r2-4-isolated-updates.yml':
            from test_isolated_update_static import assert_workflow
            assert_workflow(workflow)
            continue
        if name == 'r2-2-mandatory-gate.yml':
            from test_mandatory_gate_static import assert_workflow
            assert_workflow(workflow)
            continue
        if name == 'r2-1-static-audit.yml':
            from test_r2_1_static_audit import StaticAudit
            StaticAudit('test_dedicated_ci_has_no_production_capabilities').test_dedicated_ci_has_no_production_capabilities()
            assert workflow['on'] == {'pull_request': {
                'branches': ['codex/iphone-r1-confirmation-ui'],
                'paths': ['docs/r2-1-*', 'tests/r2_1_static_audit.py',
                          'tests/test_r2_1_static_audit.py', 'tests/test_production_workflows.py',
                          '.github/workflows/r2-1-static-audit.yml'],
            }}
            assert workflow['permissions'] == {'contents': 'read'}
            assert workflow['env'] == {'UNIFIED_RECEIPT_WRITE_ENABLED': 'false'}
            assert workflow['concurrency'] == {
                'group': 'kakeibo-r2-1-audit-${{ github.ref }}', 'cancel-in-progress': 'true'}
            assert set(workflow['jobs']) == {'inventory-and-synthetic-regression'}
            job = workflow['jobs']['inventory-and-synthetic-regression']
            assert job['runs-on'] == 'ubuntu-24.04'
            assert job['steps'][0] == {'uses': 'actions/checkout@v4', 'with': {
                'ref': '${{ github.event.pull_request.head.sha }}', 'fetch-depth': '0',
                'persist-credentials': 'false'}}
            continue
        if name == 'iphone-r1-confirmation-checks.yml':
            from test_iphone_r1_confirmation import test_ci_is_isolated
            test_ci_is_isolated()
            continue
        if name == 'iphone-v3-draft-writer-checks.yml':
            from test_synthetic_draft_writer import test_v3_writer_ci_is_dedicated_and_has_no_production_capability
            test_v3_writer_ci_is_dedicated_and_has_no_production_capability()
            continue
        if name == 'iphone-draft-runtime-checks.yml':
            from test_receipt_draft_runtime import test_runtime_workflow_is_isolated
            test_runtime_workflow_is_isolated()
            continue
        if name == 'iphone-draft-request-design-checks.yml':
            from test_iphone_draft_request_design import test_design_workflow_boundary
            test_design_workflow_boundary()
            continue
        if name == 'iphone-mvp-r0-checks.yml':
            from test_iphone_mvp_r0_adoption import test_r0_ci_has_no_production_capabilities
            test_r0_ci_has_no_production_capabilities()
            continue
        if name == 'iphone-pdf-native-readonly-checks.yml':
            assert_pdf_native_readonly_workflow(workflow)
            continue
        if name == 'iphone-synthetic-legacy-writer-checks.yml':
            assert_synthetic_legacy_writer_workflow(workflow)
            continue
        if name == "iphone-native-receipt-v2-checks.yml":
            assert_native_receipt_v2_workflow(workflow)
            continue
        if name == "iphone-legacy-contract-poc-checks.yml":
            assert_legacy_contract_poc_workflow(workflow)
            continue
        if name == "iphone-legacy-confirmation-stop-checks.yml":
            assert_legacy_confirmation_stop_workflow(workflow)
            continue
        if name == "iphone-auto-native-evidence-checks.yml":
            assert_auto_native_evidence_workflow(workflow)
            continue
        if name == "iphone-synthetic-native-writer-checks.yml":
            source = (ROOT / ".github/workflows" / name).read_text("utf-8")
            assert workflow["on"] == {"pull_request": {"branches": ["codex/phase5b2b2a-auto-native-evidence"]}}
            assert workflow["permissions"] == {"contents": "read"}
            assert workflow["env"] == {"UNIFIED_RECEIPT_WRITE_ENABLED": "false"}
            assert workflow["concurrency"] == {
                "group": "kakeibo-iphone-synthetic-native-writer-${{ github.ref }}",
                "cancel-in-progress": "true",
            }
            assert "secrets." not in source and "vars." not in source and "environment:" not in source
            assert workflow["jobs"] == {"synthetic-writer": {
                "runs-on": "ubuntu-24.04", "timeout-minutes": "30", "steps": [
                    {"uses": "actions/checkout@v4", "with": {
                        "ref": "${{ github.event.pull_request.head.sha }}", "fetch-depth": "0",
                        "persist-credentials": "false"}},
                    {"uses": "actions/setup-python@v5", "with": {"python-version": "3.12"}},
                    {"name": "Install synthetic test dependencies", "run": "pip install -r requirements-test.txt"},
                    {"name": "Collect and execute native evidence and existing writer tests", "run": (
                        "python -m pytest --collect-only -q tests/test_auto_native_evidence.py tests/test_synthetic_existing_writer.py\n"
                        "python -m pytest -vv tests/test_auto_native_evidence.py tests/test_synthetic_existing_writer.py\n")},
                    {"name": "Reuse existing safety and native-writer regressions", "run": (
                        "python -m pytest -q tests/test_iphone_confirmation.py tests/test_iphone_persistent_drafts.py tests/test_iphone_source_observation.py tests/test_iphone_item_drafts.py tests/test_iphone_phase5b1_host.py tests/test_iphone_readonly_host.py tests/test_iphone_sheet_reader.py tests/test_iphone_draft_journal.py tests/test_iphone_native_dry_run.py tests/test_iphone_review_drafts.py tests/test_iphone_review_preview.py tests/test_unified_receipt_review.py tests/test_unified_receipt_eligibility.py tests/test_unified_receipt_write.py tests/test_unified_receipt_purposes.py tests/test_unified_receipt_shadow_host.py tests/test_production_workflows.py tests/test_native_ingress_safety_stop.py tests/test_p14_plan_only.py tests/test_receipt_unit_plan.py tests/test_receipt_unit_posting.py tests/test_p14_posting.py tests/test_receipt_pipeline.py tests/test_receipt_confirmation.py tests/test_pdf_intake_runner.py tests/test_receipt_privacy_gate.py tests/test_receipt_text_extraction.py tests/test_accounting_ownership.py tests/test_accounting_enforced_gate.py tests/test_accounting_recovery.py")},
                    {"name": "Compile and check whitespace", "run": (
                        "python -m compileall -q staging_shadow/auto_native_evidence.py staging_shadow/synthetic_auto_writer.py tests/auto_native_fixtures.py tests/test_auto_native_evidence.py tests/test_synthetic_existing_writer.py\n"
                        "git diff --check\n")},
                ]}}
            continue
        if name == "iphone-accounting-recovery-checks.yml":
            assert_accounting_recovery_workflow(workflow)
            continue
        if name == "iphone-accounting-gate-checks.yml":
            assert_accounting_gate_workflow(workflow)
            continue
        if name == "iphone-phase5b2b1-checks.yml":
            assert_iphone_phase5b2b1_workflow(workflow)
            continue
        if name == "iphone-phase5b2a-checks.yml":
            assert_iphone_phase5b2a_workflow(workflow)
            continue
        if name == "iphone-phase5b1-checks.yml":
            assert_iphone_phase5b1_workflow(workflow)
            continue
        if name == "iphone-readonly-host-checks.yml":
            assert_iphone_readonly_workflow(workflow)
            continue
        if name == "iphone-sheet-adapter-checks.yml":
            assert_iphone_adapter_workflow(workflow)
            continue
        if name == "iphone-review-contract-checks.yml":
            assert_iphone_contract_workflow(workflow)
            continue
        if name == "unified-receipt-write-checks.yml":
            # A stacked, credential-free CI entry is not a production entry.
            # Assert the complete boundary before exempting its PR mutex.
            source = (ROOT / ".github/workflows" / name).read_text("utf-8")
            assert workflow["on"] == {"pull_request": {"branches": ["codex/unified-receipt-readonly"]}}
            assert workflow["permissions"] == {"contents": "read"}
            assert workflow["env"] == {"UNIFIED_RECEIPT_WRITE_ENABLED": "false"}
            assert "secrets." not in source and "vars." not in source and "environment:" not in source
            expected = deepcopy(workflows()["synthetic-tests.yml"]["jobs"])
            # The purpose contracts load the unchanged, hash-pinned historical
            # validators. Fix the exact checkout and host evidence delta;
            # every other job, step and command must remain identical.
            expected["test"]["steps"][0]["with"]["fetch-depth"] = "0"
            expected["test"]["steps"].insert(3, {
                "name": "Collect and execute isolated shadow host-level evidence",
                "run": (
                    "python -m pytest --collect-only -q tests/test_unified_receipt_shadow_host.py\n"
                    "python -m pytest -vv tests/test_unified_receipt_shadow_host.py\n"
                ),
            })
            expected["test"]["steps"].insert(4, {
                "name": "Verify blocked-egress synthetic shadow safety",
                "run": (
                    'for tool in unshare ip runuser sudo; do command -v "$tool" >/dev/null; done\n'
                    'task_user=$(id -un)\n'
                    'task_python=$(command -v python)\n'
                    'task_netns=$(readlink /proc/self/ns/net)\n'
                    "sudo unshare --net -- sh -eu -c '\n"
                    '  ip link set lo up\n'
                    '  exec runuser -u "$1" -- "$2" tests/shadow_visual_check.py --egress-safety --outer-netns "$3"\n'
                    "' sh \"$task_user\" \"$task_python\" \"$task_netns\"\n"
                ),
            })
            expected["test"]["steps"].insert(5, {
                "name": "Upload synthetic blocked-egress evidence only",
                "if": "${{ always() }}",
                "uses": "actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02",
                "with": {
                    "name": "shadow-blocked-egress-${{ matrix.suite }}",
                    "path": (
                        "artifacts/shadow-visual/*.png\n"
                        "artifacts/shadow-visual/network-records.json\n"
                        "artifacts/shadow-visual/network-summary.json\n"
                        "artifacts/shadow-visual/font-inventory.json\n"
                        "artifacts/shadow-visual/computed-fonts.json\n"
                        "artifacts/shadow-visual/viewport-metrics.json\n"
                        "artifacts/shadow-visual/safety-counts.json\n"
                        "artifacts/shadow-visual/browser-privacy.json\n"
                        "artifacts/shadow-visual/egress-safety.json\n"
                    ),
                    "retention-days": "1",
                    "if-no-files-found": "error",
                    "include-hidden-files": "false",
                },
            })
            expected["staging-local-build"] = {
                "runs-on": "ubuntu-24.04",
                "timeout-minutes": "15",
                "steps": [
                    {"uses": "actions/checkout@v4", "with": {
                        "ref": "${{ github.event.pull_request.head.sha }}",
                        "fetch-depth": "0", "persist-credentials": "false"}},
                    {"uses": "actions/setup-python@v5", "with": {"python-version": "3.12"}},
                    {"name": "Verify exact staging source and runner-local Docker", "run": (
                        'test "$(git rev-parse HEAD)" = "${{ github.event.pull_request.head.sha }}"\n'
                        'python -m staging_shadow.ci_build\n')},
                ],
            }
            assert workflow["jobs"] == expected
            continue
        if name in {"synthetic-tests.yml", "bank-pdf-diagnose.yml", "hga-shared-session-checks.yml"}:
            continue
        assert workflow["concurrency"] == {"group": "kakeibo-production", "cancel-in-progress": "false", "queue": "max"}, name
        for job in workflow["jobs"].values():
            assert "github.ref == 'refs/heads/main'" in job["if"], name
            assert "concurrency" not in job, name  # no parent/child lock deadlock
        assert set(workflow["on"]) <= {"workflow_dispatch", "schedule"}, name


def assert_iphone_contract_workflow(workflow):
    # Exact offline CI scope; no broad exemption from production entry guards.
    assert workflow == {
        'name': 'iPhone one-tab offline contracts',
        'on': {'pull_request': {'branches': ['codex/unified-receipt-write-bridge']}},
        'permissions': {'contents': 'read'},
        'env': {'UNIFIED_RECEIPT_WRITE_ENABLED': 'false'},
        'concurrency': {'group': 'kakeibo-iphone-contracts-${{ github.ref }}', 'cancel-in-progress': 'true'},
        'jobs': {'offline-contracts': {
            'runs-on': 'ubuntu-24.04', 'timeout-minutes': '15', 'steps': [
                {'uses': 'actions/checkout@v4', 'with': {
                    'ref': '${{ github.event.pull_request.head.sha }}', 'fetch-depth': '0', 'persist-credentials': 'false'}},
                {'uses': 'actions/setup-python@v5', 'with': {'python-version': '3.12'}},
                {'name': 'Install synthetic test dependencies', 'run': 'pip install -r requirements-test.txt'},
                {'name': 'Collect and execute new one-tab contracts', 'run':
                    'python -m pytest --collect-only -q tests/test_iphone_review_drafts.py tests/test_iphone_review_preview.py\n'
                    'python -m pytest -vv tests/test_iphone_review_drafts.py tests/test_iphone_review_preview.py\n'},
                {'name': 'Reuse native identity, purpose, replay and authenticated host contracts', 'run':
                    'python -m pytest -q tests/test_unified_receipt_review.py tests/test_unified_receipt_eligibility.py '
                    'tests/test_unified_receipt_write.py tests/test_unified_receipt_purposes.py '
                    'tests/test_unified_receipt_shadow_host.py tests/test_production_workflows.py'},
                {'name': 'Compile and check whitespace', 'run':
                    'python -m compileall -q app/iphone_review_drafts.py app/iphone_review_preview.py '
                    'tests/test_iphone_review_drafts.py tests/test_iphone_review_preview.py\n'
                    'git diff --check\n'},
            ]}},
    }


def test_iphone_contract_workflow_is_exact_offline_ci():
    assert_iphone_contract_workflow(workflows()['iphone-review-contract-checks.yml'])


def assert_iphone_adapter_workflow(workflow):
    assert workflow == {
        'name':'iPhone Sheets adapter offline checks',
        'on':{'pull_request':{'branches':['codex/iphone-one-tab-safety']}},
        'permissions':{'contents':'read'},
        'env':{'UNIFIED_RECEIPT_WRITE_ENABLED':'false'},
        'concurrency':{'group':'kakeibo-iphone-adapter-${{ github.ref }}','cancel-in-progress':'true'},
        'jobs':{'adapter-contracts':{
            'runs-on':'ubuntu-24.04','timeout-minutes':'15','steps':[
                {'uses':'actions/checkout@v4','with':{'ref':'${{ github.event.pull_request.head.sha }}',
                    'fetch-depth':'0','persist-credentials':'false'}},
                {'uses':'actions/setup-python@v5','with':{'python-version':'3.12'}},
                {'name':'Install synthetic test dependencies','run':'pip install -r requirements-test.txt'},
                {'name':'Collect and run raw reader, persistent CAS and native dry-run','run':
                    'python -m pytest --collect-only -q tests/test_iphone_sheet_reader.py tests/test_iphone_draft_journal.py tests/test_iphone_native_dry_run.py\n'
                    'python -m pytest -vv tests/test_iphone_sheet_reader.py tests/test_iphone_draft_journal.py tests/test_iphone_native_dry_run.py\n'},
                {'name':'Reuse existing safety boundaries','run':
                    'python -m pytest -q tests/test_iphone_review_drafts.py tests/test_iphone_review_preview.py '
                    'tests/test_unified_receipt_review.py tests/test_unified_receipt_eligibility.py tests/test_unified_receipt_write.py '
                    'tests/test_unified_receipt_purposes.py tests/test_unified_receipt_shadow_host.py tests/test_production_workflows.py'},
                {'name':'Compile and check whitespace','run':
                    'python -m compileall -q app/iphone_sheet_reader.py app/iphone_draft_journal.py app/iphone_native_dry_run.py '
                    'tests/test_iphone_sheet_reader.py tests/test_iphone_draft_journal.py tests/test_iphone_native_dry_run.py\n'
                    'git diff --check\n'},
            ]}},
    }


def test_iphone_adapter_workflow_has_exact_credential_free_ci_scope():
    assert_iphone_adapter_workflow(workflows()['iphone-sheet-adapter-checks.yml'])


def assert_iphone_readonly_workflow(workflow):
    # Reuse the already exact-checked credential-free CI structure. Only this
    # explicitly enumerated test delta is exempt from production entry guards.
    expected = deepcopy(workflows()['iphone-sheet-adapter-checks.yml'])
    assert_iphone_adapter_workflow(expected)
    expected['name'] = 'iPhone Phase 5A synthetic readonly host'
    expected['on'] = {'pull_request': {'branches':['codex/iphone-raw-draft-adapter']}}
    expected['concurrency']['group'] = 'kakeibo-iphone-readonly-${{ github.ref }}'
    job = expected['jobs'].pop('adapter-contracts')
    expected['jobs']['readonly-host'] = job
    job['steps'][3] = {'name':'Collect and execute authenticated read-only host contracts', 'run':
        'python -m pytest --collect-only -q tests/test_iphone_readonly_host.py\n'
        'python -m pytest -vv tests/test_iphone_readonly_host.py\n'}
    job['steps'][4] = {'name':'Reuse Phase 4B and native safety boundaries', 'run':
        'python -m pytest -q tests/test_iphone_sheet_reader.py tests/test_iphone_draft_journal.py '
        'tests/test_iphone_native_dry_run.py tests/test_iphone_review_drafts.py tests/test_iphone_review_preview.py '
        'tests/test_unified_receipt_review.py tests/test_unified_receipt_eligibility.py tests/test_unified_receipt_write.py '
        'tests/test_unified_receipt_purposes.py tests/test_unified_receipt_shadow_host.py tests/test_production_workflows.py'}
    job['steps'][5]['run'] = (
        'python -m compileall -q app/iphone_readonly_host.py tests/iphone_readonly_fixtures.py '
        'tests/test_iphone_readonly_host.py tests/iphone_readonly_visual.py\n'
        'git diff --check\n')
    assert workflow == expected


def test_iphone_readonly_workflow_is_exact_credential_free_ci():
    assert_iphone_readonly_workflow(workflows()['iphone-readonly-host-checks.yml'])


def assert_iphone_phase5b1_workflow(workflow):
    expected = deepcopy(workflows()['iphone-readonly-host-checks.yml'])
    assert_iphone_readonly_workflow(expected)
    expected['name'] = 'iPhone Phase 5B-1 synthetic foundations'
    expected['on'] = {'pull_request': {'branches':['codex/phase5a-synthetic-readonly']}}
    expected['concurrency']['group'] = 'kakeibo-iphone-phase5b1-${{ github.ref }}'
    job = expected['jobs'].pop('readonly-host')
    expected['jobs']['foundations'] = job
    files = ('tests/test_iphone_persistent_drafts.py tests/test_iphone_source_observation.py '
             'tests/test_iphone_item_drafts.py tests/test_iphone_phase5b1_host.py')
    job['steps'][3] = {'name':'Collect and execute Phase 5B-1 contracts', 'run':
        'python -m pytest --collect-only -q '+files+'\npython -m pytest -vv '+files+'\n'}
    job['steps'][4]['name'] = 'Reuse Phase 5A and native safety boundaries'
    job['steps'][4]['run'] = job['steps'][4]['run'].replace('python -m pytest -q ',
        'python -m pytest -q tests/test_iphone_readonly_host.py ', 1)
    job['steps'][5]['run'] = (
        'python -m compileall -q app/iphone_persistent_drafts.py app/iphone_source_observation.py '
        'app/iphone_item_drafts.py app/iphone_readonly_host.py tests/iphone_persistent_fixtures.py '+files+'\n'
        'git diff --check\n')
    assert workflow == expected


def test_iphone_phase5b1_workflow_is_exact_credential_free_ci():
    assert_iphone_phase5b1_workflow(workflows()['iphone-phase5b1-checks.yml'])


def assert_iphone_phase5b2a_workflow(workflow):
    expected=deepcopy(workflows()['iphone-phase5b1-checks.yml'])
    assert_iphone_phase5b1_workflow(expected)
    expected['name']='iPhone Phase 5B-2A synthetic owner acknowledgement'
    expected['on']={'pull_request':{'branches':['codex/phase5b1-draft-foundation']}}
    expected['concurrency']['group']='kakeibo-iphone-phase5b2a-${{ github.ref }}'
    job=expected['jobs'].pop('foundations');expected['jobs']['acknowledgement']=job
    job['steps'][3]={'name':'Collect and execute synthetic acknowledgement contracts','run':
        'python -m pytest --collect-only -q tests/test_iphone_confirmation.py\n'
        'python -m pytest -vv tests/test_iphone_confirmation.py\n'}
    job['steps'][4]['name']='Reuse Phase 5B-1 and native safety boundaries'
    prefix=('python -m pytest -q tests/test_iphone_persistent_drafts.py tests/test_iphone_source_observation.py '
            'tests/test_iphone_item_drafts.py tests/test_iphone_phase5b1_host.py ')
    job['steps'][4]['run']=job['steps'][4]['run'].replace('python -m pytest -q ',prefix,1)
    job['steps'][5]['run']=(
        'python -m compileall -q app/iphone_confirmation.py app/iphone_confirmation_host.py app/iphone_persistent_drafts.py '
        'tests/iphone_confirmation_fixtures.py tests/iphone_confirmation_visual.py tests/test_iphone_confirmation.py\n'
        'git diff --check\n')
    job['steps'][5:5]=[
        {'uses':'actions/setup-node@v4','with':{'node-version':'22'}},
        {'name':'Install isolated browser test dependencies','run':
            'npm install --no-save --prefix "$RUNNER_TEMP/phase5b2a-browser" playwright@1.62.1\n'
            'PLAYWRIGHT_BROWSERS_PATH="$RUNNER_TEMP/phase5b2a-browsers" "$RUNNER_TEMP/phase5b2a-browser/node_modules/.bin/playwright" install --with-deps chromium\n'
            'sudo apt-get install -y fonts-noto-cjk\n'},
        {'name':'Verify PC and 390px synthetic interactions without live network','run':
            'python tests/iphone_confirmation_visual.py --output .private/phase5b2a-visual\n'
            'PLAYWRIGHT_BROWSERS_PATH="$RUNNER_TEMP/phase5b2a-browsers" KAKEIBO_PLAYWRIGHT="$RUNNER_TEMP/phase5b2a-browser/node_modules/playwright" node tests/iphone_confirmation_browser.cjs .private/phase5b2a-visual\n'},
    ]
    assert workflow==expected


def test_iphone_phase5b2a_workflow_is_exact_credential_free_ci():
    assert_iphone_phase5b2a_workflow(workflows()['iphone-phase5b2a-checks.yml'])


def assert_iphone_phase5b2b1_workflow(workflow):
    expected = deepcopy(workflows()['iphone-phase5b2a-checks.yml'])
    assert_iphone_phase5b2a_workflow(expected)
    expected['name'] = 'iPhone Phase 5B-2B-1 native ingress safety stop'
    expected['on']['pull_request']['branches'] = ['codex/phase5b2a-synthetic-confirmation']
    expected['concurrency']['group'] = 'kakeibo-iphone-phase5b2b1-${{ github.ref }}'
    job = expected['jobs'].pop('acknowledgement'); expected['jobs']['safety-stop'] = job
    job['steps'][3] = {'name': 'Collect and execute safety-stop evidence', 'run':
        'python -m pytest --collect-only -q tests/test_native_ingress_safety_stop.py\n'
        'python -m pytest -vv tests/test_native_ingress_safety_stop.py\n'}
    job['steps'][4]['name'] = 'Reuse existing authentication validator writer and draft safety regressions'
    job['steps'][4]['run'] = job['steps'][4]['run'].replace(
        'python -m pytest -q ', 'python -m pytest -q tests/test_iphone_confirmation.py ', 1)
    job['steps'][5:] = [{'name': 'Compile and check whitespace', 'run':
        'python -m compileall -q tests/test_native_ingress_safety_stop.py\n'
        'git diff --check\n'}]
    assert workflow == expected


def test_iphone_phase5b2b1_workflow_is_exact_credential_free_ci():
    assert_iphone_phase5b2b1_workflow(workflows()['iphone-phase5b2b1-checks.yml'])


def assert_accounting_gate_workflow(workflow):
    expected=deepcopy(workflows()['iphone-phase5b2b1-checks.yml'])
    assert_iphone_phase5b2b1_workflow(expected)
    expected['name']='iPhone synthetic accounting ownership and enforced gates'
    expected['on']['pull_request']['branches']=['codex/phase5b2b1-shared-native-claims']
    expected['concurrency']['group']='kakeibo-iphone-accounting-gate-${{ github.ref }}'
    job=expected['jobs'].pop('safety-stop');expected['jobs']['ownership-and-gates']=job
    job['timeout-minutes']='25'
    job['steps'][3]={'name':'Collect and execute ownership and enforced gate contracts','run':
        'python -m pytest --collect-only -q tests/test_accounting_ownership.py tests/test_accounting_enforced_gate.py\n'
        'python -m pytest -vv tests/test_accounting_ownership.py tests/test_accounting_enforced_gate.py\n'}
    job['steps'][4]['name']='Reuse existing safety regressions and native validators'
    job['steps'][4]['run']+=(
        ' tests/test_native_ingress_safety_stop.py tests/test_p14_plan_only.py tests/test_receipt_unit_plan.py '
        'tests/test_receipt_unit_posting.py tests/test_p14_posting.py tests/test_receipt_pipeline.py '
        'tests/test_receipt_confirmation.py tests/test_pdf_intake_runner.py tests/test_receipt_privacy_gate.py tests/test_receipt_text_extraction.py')
    job['steps'][5]['run']=(
        'python -m compileall -q app/iphone_accounting_ownership.py app/iphone_accounting_gate.py '
        'staging_shadow/accounting_native_preflight.py staging_shadow/native_accounting_sandbox.py '
        'tests/accounting_ownership_fixtures.py tests/accounting_gate_fixtures.py '
        'tests/test_accounting_ownership.py tests/test_accounting_enforced_gate.py\n'
        'git diff --check\n')
    assert workflow==expected


def test_accounting_gate_workflow_is_exact_credential_free_ci():
    assert_accounting_gate_workflow(workflows()['iphone-accounting-gate-checks.yml'])


def assert_accounting_recovery_workflow(workflow):
    expected=deepcopy(workflows()['iphone-accounting-gate-checks.yml'])
    assert_accounting_gate_workflow(expected)
    expected['name']='iPhone synthetic accounting recovery checkpoints'
    expected['on']['pull_request']['branches']=['codex/phase5b2b1-ownership-enforced-gate']
    expected['concurrency']['group']='kakeibo-iphone-accounting-recovery-${{ github.ref }}'
    job=expected['jobs'].pop('ownership-and-gates');expected['jobs']['recovery-checkpoints']=job
    job['steps'][3]={'name':'Collect and execute synthetic recovery checkpoints','run':
        'python -m pytest --collect-only -q tests/test_accounting_recovery.py\n'
        'python -m pytest -vv tests/test_accounting_recovery.py\n'}
    job['steps'][4]['run']+=' tests/test_accounting_ownership.py tests/test_accounting_enforced_gate.py'
    job['steps'][5]['run']=(
        'python -m compileall -q app/iphone_accounting_gate.py staging_shadow/accounting_recovery_model.py '
        'tests/accounting_recovery_fixtures.py tests/test_accounting_recovery.py\n'
        'git diff --check\n')
    assert workflow==expected


def test_accounting_recovery_workflow_is_exact_credential_free_ci():
    assert_accounting_recovery_workflow(workflows()['iphone-accounting-recovery-checks.yml'])


def assert_auto_native_evidence_workflow(workflow):
    expected=deepcopy(workflows()['iphone-accounting-recovery-checks.yml'])
    assert_accounting_recovery_workflow(expected)
    expected['name']='iPhone synthetic auto native execution evidence'
    expected['on']['pull_request']['branches']=['codex/phase5b2b1-step-c-recovery']
    expected['concurrency']['group']='kakeibo-iphone-auto-native-evidence-${{ github.ref }}'
    job=expected['jobs'].pop('recovery-checkpoints');expected['jobs']['auto-native-evidence']=job
    job['steps'][3]={'name':'Collect and execute synthetic auto native evidence','run':
        'python -m pytest --collect-only -q tests/test_auto_native_evidence.py\n'
        'python -m pytest -vv tests/test_auto_native_evidence.py\n'}
    job['steps'][4]['run']+=' tests/test_accounting_recovery.py'
    job['steps'][5]['run']=(
        'python -m compileall -q staging_shadow/auto_native_evidence.py tests/auto_native_fixtures.py '
        'tests/test_auto_native_evidence.py\n'
        'git diff --check\n')
    assert workflow==expected


def test_auto_native_evidence_workflow_is_exact_credential_free_ci():
    assert_auto_native_evidence_workflow(workflows()['iphone-auto-native-evidence-checks.yml'])


def test_shared_login_contract_workflow_is_credential_free_ci_only():
    workflow = workflows()["hga-shared-session-checks.yml"]
    source = (ROOT / ".github/workflows/hga-shared-session-checks.yml").read_text("utf-8")
    assert set(workflow["on"]) == {"push", "pull_request"}
    assert workflow["permissions"] == {"contents": "read"}
    assert "secrets." not in source and "environment:" not in source
    assert "workflow_dispatch" not in source and "schedule:" not in source
    job = workflow["jobs"]["shared-session"]
    checkout = job["steps"][0]
    assert checkout["with"]["persist-credentials"] == "false"
    assert [step["run"] for step in job["steps"] if "run" in step] == [
        "pip install -r requirements-test.txt Flask==3.1.3 google-cloud-firestore==2.34.0",
        "python auth-session/compose.py --test",
        "python auth-session/compose.py --output /tmp/hga-session-container",
    ]


def test_bank_diagnostic_branch_is_read_only_and_fixed_to_validated_baseline():
    import ast
    workflow = workflows()["bank-pdf-diagnose.yml"]
    assert workflow["permissions"] == {"contents": "read"}
    assert workflow["concurrency"] == {"group": "kakeibo-production", "cancel-in-progress": "false", "queue": "max"}
    assert workflow["on"]["push"]["branches"] == ["fix/bank-pdf-hold-diagnostics"]
    job = workflow["jobs"]["diagnose"]
    assert "github.ref == 'refs/heads/fix/bank-pdf-hold-diagnostics'" in job["if"]
    assert "vars.KAKEIBO_VALIDATED_MAIN_SHA == 'ae4c8a4c91e4dd8b591782b72b029b9712925a83'" in job["if"]
    run = job["steps"][-1]
    assert run["run"] == "python -m app.bank_pdf_diagnostics"
    assert set(run["env"]) == {"KAKEIBO_VALIDATED_MAIN_SHA", "SPREADSHEET_ID", "GOOGLE_SERVICE_ACCOUNT_JSON",
        "BANK_PDF_RECURRING_AUTHORITY_JSON", "BANK_CONFIRMED_INTERNAL_TRANSFERS_JSON",
        "BANK_CONFIRMED_NON_OWN_CLASSIFICATIONS_JSON", "BANK_CONFIRMED_DOCOMO_SMTB_RULES_JSON",
        "KAKEIBO_STATE_FOLDER_ID", "BANK_STATE_FILE_ID"}
    source = ast.parse((ROOT / "app/bank_pdf_diagnostics.py").read_text(encoding="utf-8"))
    names = {node.func.id for node in ast.walk(source) if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)}
    assert {"read_only_drive_service", "read_only_sheets_service"} <= names
    assert not {"drive_service", "sheets_service", "_mark_processed", "move_processed"} & names
    methods = {node.func.attr for node in ast.walk(source) if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)}
    assert not {"update", "batchUpdate", "save", "commit", "begin"} & methods


def test_parent_is_disabled_by_default_and_only_runs_validated_main():
    parent = workflows()["kakeibo-production.yml"]
    assert parent["on"]["schedule"] == [
        {"cron": "17 9,21 * * *"}, {"cron": "31 */3 * * *"},
        {"cron": "47 21 * * *"}, {"cron": "11 23 * * *"},
    ]
    inputs = parent["on"]["workflow_dispatch"]["inputs"]
    assert inputs["mode"]["default"] == "preview"
    assert inputs["bank_apply"]["default"] == "false"
    assert inputs["scope"]["default"] == "all"
    assert inputs["scope"]["options"] == ["all", "core", "drive_receipts", "drive_paypay", "drive_bank", "amazon_canary", "receipt_reimport", "receipt_confirmation", "receipts", "projection", "daily", "ledger_order", "categories"]
    assert inputs["projection_bootstrap"]["default"] == "false"
    assert inputs["canary_source"]["options"]==["amazon","aupay_card"]
    assert inputs["canary_source"]["default"]=="amazon"
    assert inputs["receipt_operation"]["options"] == ["reanalyze", "replay"]
    assert inputs["receipt_limit"]["default"] == "1"
    assert inputs["receipt_limit"]["options"] == ["1", "2", "3"]
    job = parent["jobs"]["production"]
    assert "vars.KAKEIBO_PRODUCTION_ENABLED == 'true'" in job["if"]
    assert "vars.KAKEIBO_LEGACY_DISABLED == 'true'" in job["if"]
    assert "vars.KAKEIBO_SCHEDULE_ENABLED == 'true'" in job["if"]
    assert "github.sha == vars.KAKEIBO_VALIDATED_MAIN_SHA" in job["if"]
    script = next(step["run"] for step in job["steps"] if step.get("name") == "Execute sources and dependent accounting serially")
    for cron, scope in [("17 9,21 * * *", "core"), ("31 */3 * * *", "drive_receipts"),
                        ("11 23 * * *", "drive_paypay")]:
        assert f"'{cron}') scope={scope} ;;" in script
    assert "'47 21 * * *') scope=drive_bank; args+=(--bank-apply) ;;" in script
    assert 'args+=(--scope "$scope")' in script
    assert job["steps"][-1]["env"]["EVENT_SCHEDULE"] == "${{ github.event.schedule }}"
    uses = [step.get("uses", "") for step in job["steps"]]
    assert not any("cache/save" in value or "upload-artifact" in value for value in uses)
    assert all("continue-on-error" not in step for step in job["steps"])
    checkout = next(step for step in job["steps"] if step.get("uses", "").startswith("actions/checkout@"))
    assert checkout["with"]["ref"] == "main"
    guard = next(step["run"] for step in job["steps"] if step.get("name", "").startswith("Bind verified"))
    assert 'test "$(git rev-parse HEAD)" = "$KAKEIBO_VALIDATED_MAIN_SHA"' in guard
    ocr=next(step for step in job["steps"] if step.get("name")=="Install existing OCR runtime")
    assert "github.event.schedule == '31 */3 * * *'" in ocr["if"]
    assert "inputs.scope == 'drive_receipts'" in ocr["if"]


def test_drive_schedule_jst_times_and_receipt_period():
    jst = timezone(timedelta(hours=9))
    bank = datetime(2026, 9, 23, 21, 47, tzinfo=timezone.utc).astimezone(jst)
    paypay = datetime(2026, 9, 23, 23, 11, tzinfo=timezone.utc).astimezone(jst)
    receipt_hours = [datetime(2026, 9, 24, hour, 31, tzinfo=timezone.utc).astimezone(jst).hour
                     for hour in range(0, 24, 3)]
    assert (bank.hour, bank.minute) == (6, 47)
    assert (paypay.hour, paypay.minute) == (8, 11)
    assert receipt_hours == [9, 12, 15, 18, 21, 0, 3, 6]


def test_legacy_daily_entries_stop_before_new_entry_can_start():
    all_workflows = workflows()
    for name in ("aupay-card-recurring-production.yml", "bank-pdf-recurring.yml", "process-receipts.yml"):
        job = next(iter(all_workflows[name]["jobs"].values()))
        assert "vars.KAKEIBO_LEGACY_DISABLED != 'true'" in job["if"]


def test_serial_dependency_graph_has_no_forward_edges_or_cycles():
    seen = set()
    for source, dependencies in DEPENDENCIES.items():
        assert set(dependencies) <= seen
        seen.add(source)
    assert len(seen) == 11


def test_branch_and_sensitive_synthetic_jobs_have_no_production_secrets():
    path = ROOT / ".github/workflows/synthetic-tests.yml"
    text = path.read_text(encoding="utf-8")
    assert "secrets." not in text
    workflow = workflows()[path.name]
    assert set(workflow["on"]) == {"push", "pull_request"}
    assert workflow["permissions"] == {"contents": "read"}
    jobs = workflow["jobs"]["test"]["strategy"]["matrix"]["include"]
    assert {job["selection"] for job in jobs} == {"payroll or medical", "not payroll and not medical"}
