# Scheduler end-to-end failure model

Verify the real Worker in Wrangler's workerd runtime, with a local HTTP service
implementing GitHub App token issuance, workflow state and workflow dispatch.
Record evidence separately from a real Cloudflare Cron event and GitHub run.

- Missing credentials must fail before transmitting a request.
- JWT signature, issuer and clock-skew limits must be valid.
- An installation token must be limited to the mirror repository and Actions write.
- Invalid, expired or underprivileged tokens must not dispatch a workflow.
- HTTP 429, 5xx and network timeouts must retry within a bounded limit.
- Authentication and permission failures must fail visibly without repeated writes.
- A disabled-by-inactivity workflow may be re-enabled; a manually disabled one must stay disabled.
- Both current HTTP 200 dispatch responses and legacy HTTP 204 must be handled.
- Public HTTP requests must never trigger a workflow or expose credentials.
- Failed requests must not print private keys, JWTs or installation tokens.
- Production credentials must never be sent to a configurable third-party host.

Evidence: reports/scheduler-e2e-report.json. A local passing result does not prove
Cloudflare Cron fired or that GitHub completed an automatically dispatched run.
