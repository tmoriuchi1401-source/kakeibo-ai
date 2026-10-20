"""Static evidence checks; importing the application is deliberately unnecessary."""
import ast
import json
from pathlib import Path
import unittest

from r2_1_static_audit import (
    ALLOWED, BASE, HISTORICAL, ROOT, census, digest, git, paths, scan_python, source,
)


class StaticAudit(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.report = json.loads((ROOT / 'docs/r2-1-static-census.json').read_text('utf-8'))
        cls.inventory = json.loads((ROOT / 'docs/r2-1-ingresses.json').read_text('utf-8'))
        cls.rows = {r['id']: r for r in cls.inventory['ingresses']}

    def test_immutable_current_census_reproduces(self):
        self.assertEqual(census(BASE), self.report['baseline'])

    def test_immutable_historical_census_reproduces(self):
        self.assertEqual(census(HISTORICAL), self.report['historical'])

    def test_production_r1_and_bundle_sources_unchanged(self):
        for group in ('python', 'execution_refs', 'scripts'):
            for path, record in self.report['baseline'][group].items():
                with self.subTest(path=path):
                    self.assertEqual(digest((ROOT / path).read_text('utf-8-sig')), record['sha256_lf'])

    def test_only_authorized_additions(self):
        changed = git('diff', '--name-status', BASE).decode().splitlines()
        for entry in changed:
            status, path = entry.split('\t')
            self.assertEqual(status, 'A', entry)
            self.assertIn(path, ALLOWED)
        for path in git('ls-files', '--others', '--exclude-standard').decode().splitlines():
            self.assertIn(path, ALLOWED)

    def test_every_ingress_has_nine_dimensions_and_unknown_deployment(self):
        self.assertEqual(len(self.rows), len(self.inventory['ingresses']))
        for row in self.rows.values():
            for field in ('trigger', 'execution', 'chain', 'sinks', 'identity', 'guards',
                          'bypass', 'retry', 'drain', 'minimal', 'evidence'):
                self.assertTrue(row[field], (row['id'], field))
            self.assertIn(row['grade'], 'ABCDE')
            self.assertEqual(row['production_status'], '本番未確認')
        self.assertIn('完全棚卸しとは報告しない', self.inventory['scope_limit'])

    def test_no_production_entry_is_marked_a(self):
        classified_a = [r for r in self.rows.values() if r['grade'] == 'A']
        self.assertEqual([r['id'] for r in classified_a], ['S01'])
        self.assertIn('memory', classified_a[0]['execution'])
        self.assertIn('composition', classified_a[0]['bypass'])

    def test_file_line_and_reference_evidence_is_exact(self):
        cache = {}
        for row in self.rows.values():
            for evidence in row['evidence']:
                ref, path = evidence['ref'], evidence['path']
                self.assertIn(ref, (BASE, HISTORICAL))
                if (ref, path) not in cache:
                    cache[ref, path] = source(ref, path).splitlines()
                with self.subTest(ingress=row['id'], path=path):
                    self.assertIn(evidence['needle'], cache[ref, path][evidence['line'] - 1])

    def test_all_materializer_and_confirmation_delegates_are_mapped(self):
        documented_paths = {e['path'] for r in self.rows.values() for e in r['evidence']}
        for path, file in self.report['baseline']['python'].items():
            for call in file['calls']:
                if call['call'].endswith(('._materialize_result', '._write_accounting_plan')):
                    self.assertIn(path, documented_paths, (path, call))

    def test_raw_sdk_bypasses_are_present_and_classified(self):
        for ingress, path in (
            ('A06', 'app/aupay_card_production.py'),
            ('A06', 'app/aupay_card_batch.py'),
            ('A07', 'app/canonical_one_row_production.py'),
            ('A12', 'app/sheets.py'),
        ):
            calls = self.report['baseline']['python'][path]['calls']
            self.assertTrue(any('.values().append' in c['call'] for c in calls), path)
            self.assertEqual(self.rows[ingress]['grade'], 'B')
        for path, method in (('app/bank_status_repair.py', '.values().update'),
                             ('app/aupay_card_repair.py', '.values().batchUpdate')):
            self.assertTrue(any(method in c['call'] for c in self.report['baseline']['python'][path]['calls']))
            self.assertIn(path, {e['path'] for e in self.rows['A13']['evidence']})
        self.assertEqual(self.rows['R15']['grade'], 'C')

    def test_legacy_optional_gate_and_v1_unconditional_save_remain_detectable(self):
        confirmation = (ROOT / 'app/receipt_confirmation.py').read_text('utf-8')
        self.assertIn("if gate is not None and not gate.in_native_execution(self):", confirmation)
        production = (ROOT / 'app/receipt_confirmation_production.py').read_text('utf-8')
        self.assertNotIn('bind_shared_gate', production)
        tree = ast.parse((ROOT / 'app/receipt_reimport_production.py').read_text('utf-8'))
        klass = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'ReimportStore')
        save = next(n for n in klass.body if isinstance(n, ast.FunctionDef) and n.name == 'save')
        self.assertTrue(any(c['call'] == 'self.transport.write' for c in scan_python(ast.unparse(save))))
        self.assertEqual(self.rows['R03']['grade'], 'C')

    def test_historical_workflow_can_select_old_code(self):
        refs = self.report['baseline']['execution_refs']['.github/workflows/pdf-unit-write-canary.yml']['references']
        self.assertTrue(any('inputs.approved_sha' in r['text'] for r in refs))
        self.assertIn('app/pdf_unit_write_canary.py', self.report['historical']['python'])
        self.assertNotIn('app/pdf_unit_write_canary.py', self.report['baseline']['python'])
        self.assertEqual(self.rows['R11']['grade'], 'C')
        self.assertIn('test_tree', source(BASE, 'auth-session/compose.py'))

    def test_financial_and_nonfinancial_entry_boundaries_are_distinct(self):
        for ingress in ('R13', 'E01', 'E02', 'E03', 'E04', 'E05', 'E06', 'E07'):
            self.assertEqual(self.rows[ingress]['grade'], 'E')
        for ingress in ('A09', 'A10', 'A11', 'A12'):
            self.assertEqual(self.rows[ingress]['grade'], 'B')
        self.assertIn('return 0', source(BASE, 'app/medical_auto_posting.py'))
        self.assertNotIn('secrets.', source(BASE, '.github/workflows/amazon-daily-import.yml'))

    def test_scanner_detects_new_direct_and_wrapped_sinks(self):
        samples = (
            'def f(db): db.append_raw("支出明細", [])',
            'def f(db): db.svc.spreadsheets().values().append(body={}).execute()',
            'class Adapter:\n def f(self): self.target.update_expense_fields(2, [])',
        )
        for snippet in samples:
            self.assertTrue(scan_python(snippet), snippet)

    def test_static_limits_are_explicit(self):
        self.assertIn('No dynamic reachability', self.report['limits'])
        # A lexical census cannot resolve an arbitrary runtime-selected callable.
        self.assertEqual(scan_python('def f(db, method): getattr(db, method)([])'), [])

    def test_pr_dependency_snapshot_and_fixed_parent(self):
        frozen = json.loads((ROOT / 'docs/r2-1-pr-baseline.json').read_text('utf-8'))
        self.assertEqual(frozen['fixed_head'], BASE)
        self.assertEqual([p['number'] for p in frozen['pull_requests']], list(range(123, 147)))
        for p in frozen['pull_requests']:
            self.assertIn(p['head_sha'], git('rev-list', '--all').decode().splitlines())
            if p['number'] not in (125, 126):
                self.assertTrue(p['draft'])
                self.assertEqual(p['state'], 'open')

    def test_dedicated_ci_has_no_production_capabilities(self):
        text = (ROOT / '.github/workflows/r2-1-static-audit.yml').read_text('utf-8')
        for forbidden in ('secrets.', 'vars.', 'environment:', 'schedule:', 'workflow_dispatch:',
                          'pull_request_target:', 'google-github-actions', 'app.production_flow'):
            self.assertNotIn(forbidden, text)
        for required in ('ubuntu-24.04', 'contents: read', 'persist-credentials: false',
                         '${{ github.event.pull_request.head.sha }}', 'fetch-depth: 0'):
            self.assertIn(required, text)


if __name__ == '__main__':
    unittest.main()
