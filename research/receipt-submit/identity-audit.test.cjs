// Characterization of the EXISTING adapters, not a receipt-submit implementation.
// A passing audit reproduces an identity gap; it does not certify authorization.
// Reuse their isolated fake Sheets/UrlFetch harnesses. No real dispatch or writer.
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const root = path.resolve(__dirname, '../..');

function existingHarness(name) {
  const directory = path.join(root, 'apps-script', name);
  const context = vm.createContext({
    require: name => name === 'node:test' ? {test() {}} : require(name),
    __dirname: directory, module: {exports: {}},
  });
  // Do not register the original suites twice. Only their fake harness is reused.
  vm.runInContext(fs.readFileSync(path.join(directory, 'Code.test.cjs'), 'utf8') +
                  '\nmodule.exports = harness;', context);
  return context.module.exports();
}

function submitEdit(h, user) {
  const event = {range: {getSheet: () => ({getName: () => 'カテゴリ操作'}),
                         getA1Notation: () => 'B1'}, value: 'TRUE'};
  if (user !== undefined) event.user = {getEmail: () => user};
  h.context.categorySubmitEdited(event);
}

test('audit: category checkbox dispatches without an event user', () => {
  const h = existingHarness('category-submit');
  submitEdit(h);
  assert.equal(h.calls.length, 1);
  const sent = JSON.parse(h.calls[0].options.payload);
  assert.deepEqual(Object.keys(sent.inputs), ['request_id']);
  assert.equal(h.data['A2:J2'][0].length, 10); // Existing schema has no actor proof.
});

test('audit: category ignores editor identity even when Google event supplies it', () => {
  const a = existingHarness('category-submit');
  const b = existingHarness('category-submit');
  submitEdit(a, 'owner@example.invalid');
  submitEdit(b, 'other-editor@example.invalid');
  assert.equal(a.calls.length, 1);
  assert.equal(b.calls.length, 1);
  assert.equal(a.data['A2:J2'][0][8], b.data['A2:J2'][0][8]);
  assert.equal(a.calls[0].options.payload, b.calls[0].options.payload);
  assert.ok(!JSON.stringify(b.data).includes('other-editor@example.invalid'));
});

test('audit: configured trigger credentials are used without reading active/effective identity', () => {
  const h = existingHarness('category-submit');
  let reads = 0;
  h.context.Session = {
    getActiveUser() { reads++; throw Error('unavailable active user'); },
    getEffectiveUser() { reads++; return {getEmail: () => 'installer@example.invalid'}; },
  };
  submitEdit(h);
  assert.equal(reads, 0);
  assert.equal(h.calls.length, 1);
});

test('audit: manual payload has no editor proof; deployment access is outside this harness', () => {
  const h = existingHarness('manual-entry');
  let reads = 0;
  h.ctx.Session = {getActiveUser() { reads++; throw Error('no browser identity'); }};
  h.ctx.manualSubmit({...h.input, actor_email: 'self-asserted@example.invalid'});
  assert.equal(h.calls.length, 1);
  assert.equal(reads, 0);
  assert.ok(!('actor_email' in JSON.parse(h.queue[1][2])));
  assert.deepEqual(Object.keys(JSON.parse(h.calls[0].options.payload).inputs), ['request_id']);
});

test('audit: neither existing manifest requests an OIDC identity-token scope', () => {
  for (const name of ['category-submit', 'manual-entry']) {
    const manifest = JSON.parse(fs.readFileSync(path.join(root, 'apps-script', name, 'appsscript.json')));
    assert.ok(!manifest.oauthScopes.includes('openid'));
  }
});
