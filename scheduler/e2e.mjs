import assert from 'node:assert/strict';
import { generateKeyPairSync, verify } from 'node:crypto';
import { createServer } from 'node:http';
import { mkdir, mkdtemp, readFile, rm, writeFile } from 'node:fs/promises';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { spawn } from 'node:child_process';

const base = dirname(fileURLToPath(import.meta.url));
const root = resolve(base, '..');
const reports = join(root, 'reports');
await mkdir(reports, { recursive: true });
const temp = await mkdtemp(join(reports, '.scheduler-e2e-'));
const keys = generateKeyPairSync('rsa', { modulusLength: 2048 });
const fixtureToken = 'ghs_scheduler_e2e_fixture_never_log';
let scenario = 'ok';
let calls = [];
let tokenTries = 0;
let dispatchTries = 0;
let signatureValid = false;
let child;
let logs = '';
const evidence = [];

const server = createServer(async (req, res) => {
  let raw = '';
  for await (const part of req) raw += part;
  const body = raw ? JSON.parse(raw) : {};
  calls.push({ method: req.method, path: req.url, body });
  res.setHeader('Content-Type', 'application/json');
  if (req.url === '/app/installations/123/access_tokens') {
    tokenTries++;
    const jwt = req.headers.authorization?.replace('Bearer ', '');
    const [head, payload, signature] = jwt?.split('.') || [];
    signatureValid = Boolean(signature && verify('RSA-SHA256', Buffer.from(`${head}.${payload}`), keys.publicKey, Buffer.from(signature, 'base64url')));
    const claims = JSON.parse(Buffer.from(payload || '', 'base64url'));
    assert.equal(signatureValid, true);
    assert.equal(claims.iss, 'e2e-app');
    const now = Math.floor(Date.now() / 1000);
    assert.ok(claims.iat <= now - 50 && claims.exp > now && claims.exp <= now + 600);
    assert.deepEqual(body, { repositories: ['ps5-payloads-mirror'], permissions: { actions: 'write' } });
    if (scenario === 'token-retry' && tokenTries < 3) { res.writeHead(503); res.end('{}'); return; }
    if (scenario === 'unauthorized') { res.writeHead(401); res.end('{}'); return; }
    res.writeHead(201);
    res.end(JSON.stringify({ token: scenario === 'empty-token' ? '' : fixtureToken,
      expires_at: new Date(Date.now() + (scenario === 'expired-token' ? -1000 : 3600000)).toISOString(),
      permissions: { actions: scenario === 'read-only-token' ? 'read' : 'write' } }));
    return;
  }
  assert.equal(req.headers.authorization, `Bearer ${fixtureToken}`);
  const workflow = '/repos/yanlong1989/ps5-payloads-mirror/actions/workflows/update_mirror.yml';
  if (req.url === workflow && req.method === 'GET') {
    res.end(JSON.stringify({ state: scenario === 'inactive' ? 'disabled_inactivity' : scenario === 'disabled' ? 'disabled_manually' : 'active' }));
  } else if (req.url === workflow + '/enable') {
    assert.equal(req.method, 'PUT'); res.writeHead(204); res.end();
  } else if (req.url === workflow + '/dispatches') {
    dispatchTries++;
    assert.equal(req.method, 'POST');
    assert.deepEqual(body, { ref: 'main', inputs: { payload: '' } });
    if (scenario === 'dispatch-retry' && dispatchTries < 3) { res.writeHead(429, { 'Retry-After': '0' }); res.end('{}'); return; }
    if (scenario === 'forbidden') { res.writeHead(403); res.end('{}'); return; }
    if (scenario === 'legacy') { res.writeHead(204); res.end(); return; }
    res.end(JSON.stringify({ workflow_run_id: 456, html_url: 'https://github.com/yanlong1989/ps5-payloads-mirror/actions/runs/456' }));
  } else { res.writeHead(404); res.end('{}'); }
});
await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
const apiPort = server.address().port;
const workerPort = 18787;
const config = { name: 'scheduler-e2e', main: join(base, 'worker.mjs'), compatibility_date: '2026-10-12',
  vars: { REPOSITORY: 'yanlong1989/ps5-payloads-mirror', WORKFLOW: 'update_mirror.yml', BRANCH: 'main',
    GITHUB_APP_ID: 'e2e-app', GITHUB_INSTALLATION_ID: '123', GITHUB_API_BASE: `http://127.0.0.1:${apiPort}`,
    ALLOW_LOCAL_TEST: 'true', RETRY_DELAY_MS: '5' } };

async function start(privateKey) {
  await writeFile(join(temp, 'wrangler.json'), JSON.stringify(config));
  await writeFile(join(temp, '.dev.vars'), `GITHUB_APP_PRIVATE_KEY=${JSON.stringify(privateKey || '')}\n`);
  child = spawn(process.execPath, [join(base, 'node_modules/wrangler/bin/wrangler.js'), 'dev', '--config', join(temp, 'wrangler.json'),
    '--local', '--test-scheduled', '--ip', '127.0.0.1', '--port', String(workerPort)],
  { cwd: temp, windowsHide: true, env: { ...process.env, WRANGLER_SEND_METRICS: 'false', TMP: temp, TEMP: temp,
    XDG_CONFIG_HOME: join(temp, 'config') } });
  child.stdout.on('data', data => logs += data);
  child.stderr.on('data', data => logs += data);
  for (let i = 0; i < 160; i++) {
    if (child.exitCode !== null) throw new Error(`Worker exited with ${child.exitCode}`);
    try { const response = await fetch(`http://127.0.0.1:${workerPort}/health`, { signal: AbortSignal.timeout(1000) }); if (response.status === 200 || response.status === 503) return; } catch {}
    await new Promise(resolve => setTimeout(resolve, 250));
  }
  throw new Error('Worker did not become ready');
}

async function stop() {
  if (!child || child.exitCode !== null || child.signalCode !== null) return;
  const stopped = child;
  const exited = new Promise(resolve => { stopped.once('exit', resolve); stopped.once('close', resolve); });
  if (process.platform === 'win32') {
    const kill = spawn('C:\\Windows\\System32\\taskkill.exe', ['/pid', String(stopped.pid), '/t', '/f'], { windowsHide: true, stdio: 'ignore' });
    await Promise.race([new Promise(resolve => { kill.on('error', resolve); kill.on('exit', resolve); kill.on('close', resolve); }), new Promise(resolve => setTimeout(resolve, 2000))]);
    kill.unref();
  } else child.kill('SIGTERM');
  await Promise.race([exited, new Promise(resolve => setTimeout(resolve, 2000))]);
  stopped.kill('SIGKILL');
  stopped.stdout.destroy();
  stopped.stderr.destroy();
  stopped.unref();
}

async function scheduled(kind, success) {
  scenario = kind; calls = []; tokenTries = 0; dispatchTries = 0;
  const response = await fetch(`http://127.0.0.1:${workerPort}/cdn-cgi/local/scheduled?format=json&cron=7+*+*+*+*`, { signal: AbortSignal.timeout(45000) });
  const result = await response.json();
  assert.equal(result.outcome === 'ok', success, `${kind}: ${JSON.stringify(result)}`);
  evidence.push({ scenario: kind, passed: true, outcome: result.outcome, token_requests: tokenTries, dispatch_requests: dispatchTries });
  console.log(JSON.stringify({ scenario: kind, passed: true }));
}

try {
  await start(keys.privateKey.export({ type: 'pkcs8', format: 'pem' }));
  for (const kind of ['ok', 'legacy', 'token-retry', 'dispatch-retry', 'inactive']) {
    await scheduled(kind, true);
    assert.equal(dispatchTries, kind === 'dispatch-retry' ? 3 : 1);
    assert.equal(tokenTries, kind === 'token-retry' ? 3 : 1);
    if (kind === 'inactive') assert.ok(calls.some(call => call.path.endsWith('/enable')));
  }
  for (const kind of ['unauthorized', 'empty-token', 'expired-token', 'read-only-token', 'disabled', 'forbidden']) {
    await scheduled(kind, false);
    assert.equal(dispatchTries, kind === 'forbidden' ? 1 : 0);
    if (kind === 'disabled') assert.ok(!calls.some(call => call.path.endsWith('/enable')));
  }
  const before = calls.length;
  assert.equal((await fetch(`http://127.0.0.1:${workerPort}/trigger`, { method: 'POST' })).status, 404);
  assert.equal(calls.length, before);
  assert.ok(!logs.includes(fixtureToken));
  assert.ok(!logs.includes('-----BEGIN PRIVATE KEY-----'));
  evidence.push({ scenario: 'public endpoint cannot dispatch and logs do not expose credentials', passed: true });
  await stop();
  await start('');
  calls = [];
  await scheduled('missing-credentials', false);
  assert.equal(calls.length, 0);
  await writeFile(join(reports, 'scheduler-e2e-report.json'), JSON.stringify({ kind: 'workerd-and-github-http-end-to-end', passed: true, scenarios: evidence }, null, 2) + '\n');
  console.log(JSON.stringify({ passed: true, scenarios: evidence.length }));
} finally {
  await stop();
  server.closeAllConnections();
  await new Promise(resolve => server.close(resolve));
  assert.ok(resolve(temp).startsWith(resolve(reports) + '\\') || resolve(temp).startsWith(resolve(reports) + '/'));
  await rm(temp, { recursive: true, force: true, maxRetries: 10, retryDelay: 500 });
}
