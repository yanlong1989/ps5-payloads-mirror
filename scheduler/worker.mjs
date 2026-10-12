const VERSION = '2026-03-10';
const CRON = '7 * * * *';

function configuration(env) {
  const repository = env.REPOSITORY || 'yanlong1989/ps5-payloads-mirror';
  const workflow = env.WORKFLOW || 'update_mirror.yml';
  const branch = env.BRANCH || 'main';
  if (!/^[\w.-]+\/[\w.-]+$/.test(repository) || !/^[\w.-]+\.ya?ml$/.test(workflow)) throw new Error('invalid_target');
  const api = new URL(env.GITHUB_API_BASE || 'https://api.github.com');
  const local = env.ALLOW_LOCAL_TEST === 'true' && api.protocol === 'http:' && ['127.0.0.1', 'localhost', '[::1]'].includes(api.hostname);
  if (api.origin !== 'https://api.github.com' && !local) throw new Error('untrusted_api_origin');
  if (api.username || api.password || api.pathname !== '/' || api.search || api.hash) throw new Error('invalid_api_origin');
  return { repository, workflow, branch, api: api.origin, local };
}

function base64url(bytes) {
  let text = '';
  for (const byte of bytes) text += String.fromCharCode(byte);
  return btoa(text).replace(/=/g, '').replace(/\+/g, '-').replace(/\//g, '_');
}

async function appJwt(env) {
  if (!env.GITHUB_APP_ID || !/^\d+$/.test(env.GITHUB_INSTALLATION_ID || '') || !env.GITHUB_APP_PRIVATE_KEY) throw new Error('missing_app_credentials');
  const pem = env.GITHUB_APP_PRIVATE_KEY.replace(/\\n/g, '\n');
  if (!pem.includes('-----BEGIN PRIVATE KEY-----')) throw new Error('private_key_must_be_pkcs8');
  const encoded = pem.replace(/-----BEGIN PRIVATE KEY-----|-----END PRIVATE KEY-----|\s/g, '');
  let key;
  try {
    key = await crypto.subtle.importKey('pkcs8', Uint8Array.from(atob(encoded), value => value.charCodeAt(0)),
      { name: 'RSASSA-PKCS1-v1_5', hash: 'SHA-256' }, false, ['sign']);
  } catch { throw new Error('invalid_private_key'); }
  const now = Math.floor(Date.now() / 1000);
  const encode = value => base64url(new TextEncoder().encode(JSON.stringify(value)));
  const input = `${encode({ alg: 'RS256', typ: 'JWT' })}.${encode({ iat: now - 60, exp: now + 540, iss: env.GITHUB_APP_ID })}`;
  const signature = await crypto.subtle.sign('RSASSA-PKCS1-v1_5', key, new TextEncoder().encode(input));
  return `${input}.${base64url(new Uint8Array(signature))}`;
}

async function request(config, token, path, method = 'GET', body) {
  for (let attempt = 0; attempt < 3; attempt++) {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 10000);
    let response;
    try {
      response = await fetch(config.api + path, { method, redirect: 'manual', signal: controller.signal,
        headers: { Authorization: `Bearer ${token}`, Accept: 'application/vnd.github+json',
          'X-GitHub-Api-Version': VERSION, 'User-Agent': 'ps5-mirror-scheduler', 'Content-Type': 'application/json' },
        ...(body === undefined ? {} : { body: JSON.stringify(body) }) });
    } catch (error) {
      if (config.local) console.error(JSON.stringify({ local_transport_error: error?.name, detail: error?.message }));
      if (attempt === 2) throw new Error('github_network_failure');
    } finally { clearTimeout(timeout); }
    if (response && response.status !== 429 && response.status < 500) return response;
    if (attempt === 2) throw new Error(response ? `github_http_${response.status}` : 'github_network_failure');
    const retryAfter = Math.min(30, Math.max(0, Number(response?.headers.get('Retry-After')) || 0));
    await new Promise(resolve => setTimeout(resolve, Math.max(retryAfter * 1000, config.local ? 5 : 1000 * (attempt + 1))));
  }
}

async function dispatch(env, scheduledTime) {
  const config = configuration(env);
  const jwt = await appJwt(env);
  const name = config.repository.split('/')[1];
  const tokenResponse = await request(config, jwt, `/app/installations/${env.GITHUB_INSTALLATION_ID}/access_tokens`, 'POST',
    { repositories: [name], permissions: { actions: 'write' } });
  if (tokenResponse.status !== 201) throw new Error(`github_token_http_${tokenResponse.status}`);
  let auth;
  try { auth = await tokenResponse.json(); } catch { throw new Error('invalid_token_response'); }
  if (typeof auth.token !== 'string' || !auth.token) throw new Error('empty_installation_token');
  if (!(Date.parse(auth.expires_at) > Date.now() + 60000)) throw new Error('expired_installation_token');
  if (auth.permissions?.actions !== 'write') throw new Error('installation_requires_actions_write');
  const path = `/repos/${config.repository}/actions/workflows/${config.workflow}`;
  const info = await request(config, auth.token, path);
  if (info.status !== 200) throw new Error(`github_workflow_http_${info.status}`);
  let workflow;
  try { workflow = await info.json(); } catch { throw new Error('invalid_workflow_response'); }
  if (workflow.state === 'disabled_inactivity') {
    const enabled = await request(config, auth.token, path + '/enable', 'PUT');
    if (enabled.status !== 204) throw new Error(`github_enable_http_${enabled.status}`);
  } else if (workflow.state !== 'active') throw new Error('workflow_disabled_by_operator');
  const result = await request(config, auth.token, path + '/dispatches', 'POST', { ref: config.branch, inputs: { payload: '' } });
  if (result.status !== 200 && result.status !== 204) throw new Error(`github_dispatch_http_${result.status}`);
  let runId = null;
  if (result.status === 200) {
    let detail;
    try { detail = await result.json(); } catch { throw new Error('invalid_dispatch_response'); }
    if (!Number.isSafeInteger(detail.workflow_run_id) || detail.workflow_run_id <= 0) throw new Error('dispatch_run_id_missing');
    runId = detail.workflow_run_id;
  }
  return { event: 'github_dispatch_accepted', repository: config.repository, workflow: config.workflow,
    scheduled_time: new Date(scheduledTime).toISOString(), run_id: runId, http_status: result.status };
}

export default {
  async scheduled(event, env) {
    try {
      const result = await dispatch(env, event.scheduledTime);
      console.log(JSON.stringify(result));
    } catch (error) {
      const code = /^[a-z0-9_]+$/.test(error?.message || '') ? error.message : 'scheduler_failed';
      console.error(JSON.stringify({ event: 'github_dispatch_failed', code }));
      throw new Error(code);
    }
  },
  async fetch(request, env) {
    if (request.method !== 'GET' || new URL(request.url).pathname !== '/health') return new Response('Not found', { status: 404 });
    const configured = Boolean(env.GITHUB_APP_ID && env.GITHUB_INSTALLATION_ID && env.GITHUB_APP_PRIVATE_KEY);
    return Response.json({ service: 'ps5-mirror-scheduler', repository: env.REPOSITORY || 'yanlong1989/ps5-payloads-mirror',
      workflow: env.WORKFLOW || 'update_mirror.yml', cron: CRON, configured },
    { status: configured ? 200 : 503, headers: { 'Cache-Control': 'no-store' } });
  }
};
