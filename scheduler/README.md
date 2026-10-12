# Cloudflare hourly scheduler

Cloudflare Cron calls the existing GitHub Actions workflow at minute 7 of every
hour (`7 * * * *`, UTC). The minute is also 7 in Asia/Shanghai. GitHub's native
minute-17 schedule remains available as a backup. Each workflow checks for
upstream changes and skips unchanged files.

The Worker creates a GitHub App JWT for each invocation, exchanges it for a
short-lived installation token, then dispatches `update_mirror.yml` on `main`.
An accepted dispatch is logged with its GitHub run ID. A failed dispatch raises
an error in Cloudflare. The scheduler does not claim that GitHub finished the
workflow merely because dispatch was accepted.

## Permissions and secrets

Create a private GitHub App, with webhooks disabled and these permissions:

- Repository Actions: read and write.
- Repository Metadata: read, which GitHub includes automatically.
- No account permissions, organization permissions or Contents permission.
- Install it only on `yanlong1989/ps5-payloads-mirror`.

Set `GITHUB_APP_ID` and `GITHUB_INSTALLATION_ID` as Worker variables or secrets.
Convert the downloaded GitHub private key to PKCS#8 and store it as the encrypted
Worker secret `GITHUB_APP_PRIVATE_KEY`. Never commit a private key, `.dev.vars`
or a token to this repository. Never put production keys in the test fixtures.

For the official Wrangler OAuth login, the deployment uses `account:read`,
`workers_scripts:write` and `workers_tail:read`, with `offline_access` for refresh.
Cloudflare grants these at account scope. This deployment creates only the
`ps5-mirror-scheduler` Worker. Wrangler's saved login belongs in a private local
configuration directory outside shared or synchronized folders.

## Reproduce and deploy

Use Node.js 22 or newer. Wrangler is pinned by `package-lock.json`.

```sh
cd scheduler
npm ci
npm run test:e2e
npx wrangler deploy --dry-run
npx wrangler login --scopes account:read workers_scripts:write workers_tail:read
# Select the intended account or set account_id in wrangler.jsonc.
npx wrangler deploy
npx wrangler secret put GITHUB_APP_ID
npx wrangler secret put GITHUB_INSTALLATION_ID
npx wrangler secret put GITHUB_APP_PRIVATE_KEY
```

Supply secrets through standard input or Wrangler's hidden prompt. The private
key must have a `BEGIN PRIVATE KEY` header, rather than `BEGIN RSA PRIVATE KEY`.
Node.js `crypto.createPrivateKey()` can export a GitHub key as PKCS#8.

## Verify the live schedule

`GET /health` reports the target, configured flag and expected cron. It does
not test GitHub permissions or prove that Cron fired. Every other HTTP path
returns 404; there is no public dispatch endpoint.

After deploying, confirm the hourly Cron Trigger in Cloudflare. Changes may
take up to 15 minutes to propagate. Observe a real scheduled invocation in
Cloudflare and match its logged run ID to a completed GitHub Actions run.
Cloudflare Cron Events history can take up to 30 minutes to appear. Local tests
and manual dispatches must be recorded separately from natural Cron events.

The end-to-end test runs the real workerd runtime against a local HTTP GitHub
fixture and writes `reports/scheduler-e2e-report.json`. It covers dispatch,
credential checks, bounded retries and disabled workflows. It uses disposable
test keys. See [FAILURE_MODES.md](FAILURE_MODES.md) for the failure model.

Official references: [Cron Triggers](https://developers.cloudflare.com/workers/configuration/cron-triggers/),
[GitHub App authentication](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app),
[workflow dispatch](https://docs.github.com/en/rest/actions/workflows#create-a-workflow-dispatch-event).
