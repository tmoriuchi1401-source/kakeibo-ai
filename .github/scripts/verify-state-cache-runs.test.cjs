const {test} = require('node:test');
const assert = require('node:assert/strict');
const verify = require('./verify-state-cache-runs.cjs');

function fixture() {
  const sources = ['amazon-daily-import.yml', 'aupay-card-recurring-production.yml', 'bank-pdf-recurring.yml'];
  const runs = Object.fromEntries(sources.map((source, i) => [source, [{id: 101 + i, run_attempt: 1, status: 'completed', conclusion: 'success'}]]));
  const jobs = Object.fromEntries([101, 102, 103].map(id => [id, {total_count: 1, jobs: [{conclusion: 'success', steps: [{name: 'Save post-run durable state', conclusion: 'success'}]}]}]));
  const context = {repo: {owner: 'synthetic', repo: 'synthetic'}, sha: 'verified-sha'};
  const github = {rest: {repos: {getBranch: async () => ({data: {commit: {sha: context.sha}}})}, actions: {
    listWorkflowRuns: async ({workflow_id}) => ({data: {workflow_runs: runs[workflow_id]}}),
    listJobsForWorkflowRun: async ({run_id}) => ({data: jobs[run_id]}),
  }}};
  const args = {github, context, core: {info: () => {}}, keys: {
    amazon: 'amazon-production-state-101-1', card: 'aupay-card-production-state-102-1', bank: 'bank-pdf-recurring-state-103-1',
  }};
  return {args, runs, jobs};
}

test('accepts the exact completed state-producing runs', async () => {
  await verify(fixture().args);
});

test('later guard-only skip is not selected as a successful state update', async () => {
  const f = fixture();
  f.runs['amazon-daily-import.yml'].unshift({id: 104, status: 'completed', conclusion: 'success'});
  f.jobs[104] = {total_count: 1, jobs: [{conclusion: 'skipped', steps: []}]};
  await verify(f.args);
  f.args.keys.amazon = 'amazon-production-state-104-1';
  await assert.rejects(verify(f.args), /selected_run_not_confirmed|did_not_save_state/);
});

test('later failed or partly executed run prevents selecting an older success', async () => {
  const f = fixture();
  f.runs['amazon-daily-import.yml'].unshift({id: 104, status: 'completed', conclusion: 'failure'});
  f.jobs[104] = {total_count: 1, jobs: [{conclusion: 'failure', steps: [{name: 'Write', conclusion: 'failure'}]}]};
  await assert.rejects(verify(f.args), /later_run_requires_reconciliation/);
});

test('rerun attempt mismatch and cache-save failure fail closed', async () => {
  const f = fixture();
  f.runs['bank-pdf-recurring.yml'][0].run_attempt = 2;
  await assert.rejects(verify(f.args), /selected_run_not_confirmed/);
  f.runs['bank-pdf-recurring.yml'][0].run_attempt = 1;
  f.jobs[103].jobs[0].steps[0].conclusion = 'skipped';
  await assert.rejects(verify(f.args), /did_not_save_state/);
});

test('live source job or moved main prevents migration', async () => {
  const f = fixture();
  f.runs['bank-pdf-recurring.yml'][0].status = 'in_progress';
  await assert.rejects(verify(f.args), /source_run_not_finished/);
  f.args.github.rest.repos.getBranch = async () => ({data: {commit: {sha: 'other-sha'}}});
  await assert.rejects(verify(f.args), /maintenance_main_moved/);
});

test('prefix keys and missing selected history are rejected', async () => {
  const f = fixture();
  f.args.keys.amazon = 'amazon-production-state-';
  await assert.rejects(verify(f.args), /cache_reference_invalid/);
  f.args.keys.amazon = 'amazon-production-state-999-1';
  await assert.rejects(verify(f.args), /selected_run_not_in_recent_history/);
});
