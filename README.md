# omarchy-plugin-watch

Watches the Omarchy plugin marketplace (`omacom/omarchy-plugin-marketplace`) for
activity on our plugin issues — maintainer review blocks, label changes such as
`needs-fixes`, and publication/closure — and pushes **only the changes** to a
Hermes webhook route, which then wakes an agent run.

Why this repo exists: a webhook cannot be installed on the marketplace repository
(it belongs to the maintainers), so this scheduled workflow does the checking on
GitHub's runners. The workstation never polls anything.

## How it works

1. `watch_marketplace.py` lists our issues (`creator=meviusisback`, any state) and
   their comments, and diffs them against `state/seen.json`.
2. Changed items become a small JSON payload; the first run only seeds the state
   file, so history is never replayed at the user.
3. The payload is POSTed, HMAC-signed (`X-Hub-Signature-256`), to the Hermes route
   `omarchy-marketplace-verify`, which validates the signature and runs its own
   filter script before any agent sees it.

## Setup

```bash
gh secret set HERMES_WEBHOOK_URL    --body "https://<ingress>/webhooks/omarchy-marketplace-verify"
gh secret set HERMES_WEBHOOK_SECRET --body "<route secret from 'hermes webhook list'>"
gh workflow run marketplace-watch   # manual run; expect "pushed N event(s) → HTTP 200"
```

Then uncomment the `schedule:` block in `.github/workflows/marketplace-watch.yml`
and push — it watches for changes every 10 minutes.

## Notes

- Comment text is treated as data: bodies are capped at 1200 characters and the
  agent prompt is explicitly instructed never to follow instructions found in them.
- Bot comments (`github-actions[bot]`) and routine labels (`plugin-update`,
  `validated`) are filtered out — only `needs-fixes`, `blocked`,
  `approved-and-verified`, `invalid` and `standard-installation-approved` count as
  signal.
- GitHub disables scheduled workflows in a quiet repository after 60 days; the
  heartbeat file is refreshed weekly to keep it alive.
