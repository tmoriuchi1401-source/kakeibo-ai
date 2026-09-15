// State-producing runs are explicit; later guard-only skips are not checkpoints.
module.exports = async function verify({github, context, core, keys}) {
  const main = await github.rest.repos.getBranch({...context.repo, branch: 'main'});
  if (main.data.commit.sha !== context.sha) throw new Error('maintenance_main_moved');
  const sources = [
    ['amazon-daily-import.yml', 'amazon-production-state', keys.amazon],
    ['aupay-card-recurring-production.yml', 'aupay-card-production-state', keys.card],
    ['bank-pdf-recurring.yml', 'bank-pdf-recurring-state', keys.bank],
  ];
  for (const [workflow_id, prefix, key] of sources) {
    const match = new RegExp('^' + prefix + '-([1-9][0-9]*)-([1-9][0-9]*)$').exec(key);
    if (!match) throw new Error('cache_reference_invalid');
    const response = await github.rest.actions.listWorkflowRuns({...context.repo, workflow_id, branch: 'main', per_page: 100});
    const runs = response.data.workflow_runs;
    const index = runs.findIndex(run => String(run.id) === match[1]);
    if (index < 0) throw new Error('selected_run_not_in_recent_history');
    for (const run of runs.slice(0, index + 1)) {
      if (run.status !== 'completed') throw new Error('source_run_not_finished');
      const jobs = (await github.rest.actions.listJobsForWorkflowRun({...context.repo, run_id: run.id, filter: 'latest', per_page: 100})).data;
      if (!jobs.jobs.length || jobs.total_count !== jobs.jobs.length) throw new Error('source_jobs_not_complete');
      if (String(run.id) !== match[1]) {
        if (!jobs.jobs.every(job => job.conclusion === 'skipped' && (job.steps || []).every(step => step.conclusion === 'skipped'))) {
          throw new Error('later_run_requires_reconciliation');
        }
        continue;
      }
      if (String(run.run_attempt) !== match[2] || run.conclusion !== 'success') throw new Error('selected_run_not_confirmed');
      if (!jobs.jobs.some(job => job.conclusion === 'success' && job.steps.some(step =>
          step.name === 'Save post-run durable state' && step.conclusion === 'success'))) {
        throw new Error('selected_run_did_not_save_state');
      }
    }
  }
  core.info('Exact state-producing runs and later guard-only skips verified.');
};
