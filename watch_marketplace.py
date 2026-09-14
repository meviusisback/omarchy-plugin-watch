#!/usr/bin/env python3
"""Watch the Omarchy plugin marketplace for activity on our plugin issues.

Runs on GitHub's runners (scheduled Action) so the workstation never polls.
Compares the current state of our issues against the state stored in
``state/seen.json`` and POSTs only CHANGES to the Hermes webhook route
``omarchy-marketplace-verify``, HMAC-signed exactly like a GitHub webhook.

stdlib only. Env:
  HERMES_WEBHOOK_URL     full route URL (https://<ingress>/webhooks/omarchy-marketplace-verify)
  HERMES_WEBHOOK_SECRET  route secret
  GH_TOKEN / GITHUB_TOKEN  read access to public issues
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import sys
import urllib.error
import urllib.request

API = "https://api.github.com"
REPO = "omacom/omarchy-plugin-marketplace"
CREATOR = "meviusisback"          # every [Verify]/[Plugin] issue of ours
MAX_BODY = 1200
MAX_EVENTS = 10

# Labels worth waking a human up for. plugin-update/validated are routine noise.
WATCHED_LABELS = {"needs-fixes", "blocked", "approved-and-verified", "invalid",
                  "standard-installation-approved"}


def api(path: str, token: str | None):
    request = urllib.request.Request(
        API + path,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "omarchy-plugin-watch",
            **({"Authorization": f"Bearer {token}"} if token else {}),
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read(2_000_000).decode("utf-8", errors="replace"))


def text(value, cap):
    if not isinstance(value, str):
        return ""
    return value.replace("\r\n", "\n").strip()[:cap]


def plugin_of(issue: dict) -> str:
    body = issue.get("body") or ""
    for line in body.splitlines():
        if line.strip().startswith("meviusisback.") or line.strip().startswith("io.github."):
            return text(line.strip(), 80)
    return text(issue.get("title", ""), 80)


def collect(issues, seen, token):
    """Return (events, new_state) for everything that changed since *seen*."""
    events, state = [], {}
    for issue in issues:
        number = issue.get("number")
        if not isinstance(number, int):
            continue
        labels = sorted(lbl.get("name", "") for lbl in issue.get("labels", [])
                        if isinstance(lbl, dict))
        prior = seen.get(str(number)) or {}
        plugin = plugin_of(issue)
        url = issue.get("html_url") or ""
        state[str(number)] = {
            "labels": labels,
            "state": issue.get("state"),
            "title": text(issue.get("title"), 200),
        }
        if prior.get("labels") != labels:
            added = [lbl for lbl in labels
                     if lbl not in (prior.get("labels") or []) and lbl in WATCHED_LABELS]
            for label in added:
                events.append({"kind": "label", "plugin": plugin, "issue": number,
                               "title": text(issue.get("title"), 200),
                               "actor": "", "labels": labels, "body": f"label added: {label}",
                               "url": url})
        elif prior.get("state") == "open" and issue.get("state") == "closed":
            events.append({"kind": "closed", "plugin": plugin, "issue": number,
                           "title": text(issue.get("title"), 200),
                           "actor": issue.get("closed_by", {}).get("login", "") if isinstance(
                               issue.get("closed_by"), dict) else "",
                           "labels": labels, "body": "", "url": url})

        last_id = prior.get("last_comment_id") or 0
        comments = api(f"/repos/{REPO}/issues/{number}/comments?per_page=100", token)
        state[str(number)]["last_comment_id"] = max(
            [c.get("id", 0) for c in comments if isinstance(c, dict)] + [last_id])
        for comment in comments if isinstance(comments, list) else []:
            if not isinstance(comment, dict) or (comment.get("id") or 0) <= last_id:
                continue
            actor = (comment.get("user") or {}).get("login", "") if isinstance(
                comment.get("user"), dict) else ""
            if actor.endswith("[bot]") or actor == "github-actions":
                continue  # validation/baseline bots: routine, never a review block
            body = text(comment.get("body"), MAX_BODY)
            if not body:
                continue
            events.append({
                "kind": "issue_comment",
                "plugin": plugin,
                "issue": number,
                "title": text(issue.get("title"), 200),
                "actor": actor,
                "labels": labels,
                "body": body,
                "url": comment.get("html_url") or url,
            })
    return events[:MAX_EVENTS], state


def post(url: str, secret: str, payload: dict) -> int:
    body = json.dumps(payload).encode("utf-8")
    signature = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    request = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": "application/json",
                 "X-Hub-Signature-256": signature,
                 "X-GitHub-Event": "marketplace-watch",
                 "X-GitHub-Delivery": hashlib.sha256(body).hexdigest()[:32]},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status
    except urllib.error.HTTPError as exc:
        return exc.code


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state", default="state/seen.json")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or ""
    first_run = not os.path.exists(args.state)
    seen = {}
    if os.path.exists(args.state):
        try:
            with open(args.state, encoding="utf-8") as fh:
                seen = json.load(fh)
        except (OSError, ValueError):
            seen = {}

    issues = api(f"/repos/{REPO}/issues?creator={CREATOR}&state=all&per_page=50", token)
    if not isinstance(issues, list):
        print("unexpected API response", file=sys.stderr)
        return 1

    events, state = collect(issues, seen if isinstance(seen, dict) else {}, token)
    payload = {"source": "marketplace-watch", "count": len(events), "events": events}
    print(f"{len(issues)} issues scanned, {len(events)} new event(s)")
    for event in events:
        print(f"  {event['kind']:14} {event['plugin']:28} #{event['issue']} "
              f"by {event['actor'] or '-'}: {event['body'][:70]!r}")

    wrote_state = False
    if not args.dry_run:
        os.makedirs(os.path.dirname(args.state) or ".", exist_ok=True)
        with open(args.state, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2, sort_keys=True)
            fh.write("\n")
        wrote_state = True

    url, secret = os.environ.get("HERMES_WEBHOOK_URL", ""), os.environ.get("HERMES_WEBHOOK_SECRET", "")
    if not events:
        print("nothing to push")
        return 0
    if args.dry_run:
        print("dry run — not pushing")
        return 0
    if first_run:
        print("first run — state seeded, nothing pushed (no backfill at the user)")
        return 0
    if not (url and secret):
        print("HERMES_WEBHOOK_URL / HERMES_WEBHOOK_SECRET not set — state saved, nothing pushed",
              file=sys.stderr)
        return 0
    status = post(url, secret, payload)
    print(f"pushed {len(events)} event(s) → HTTP {status}"
          f"{' (accepted)' if status < 300 else ' (FAILED — check the secret/URL)'}")
    return 0 if status < 300 else 1


if __name__ == "__main__":
    sys.exit(main())
