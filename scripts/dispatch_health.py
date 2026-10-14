"""
Reports how reliably the GCP VM backup poller (kalshi-bot-vm's
~/metaculus-trigger/, outside this repo) is actually dispatching
run_bot_on_minibench.yaml and run_bot_on_seasonal.yaml, plus a PAT-expiry
warning -- both surfaced in the weekly summary. See docs/STAGE_B_PLAN.md
"Backup poller" for why GitHub's own `schedule:` trigger needed a backup in
the first place (it was silently skipping ~96% of ticks, confirmed
2026-10-13).

Why this reads GitHub's API instead of the VM's own dispatch.log: every
successful VM dispatch call creates a run with event="workflow_dispatch";
GitHub's own cron creates event="schedule" runs. These are distinguishable,
so counting workflow_dispatch-event runs is a faithful proxy for "how many
of the VM's dispatch attempts actually landed" -- without the VM ever
needing to push anything into this repo (it dispatches via a PAT scoped to
Actions only; this reads via the Actions job's own built-in GITHUB_TOKEN,
no new credential on either side). A failed curl call on the VM (expired
PAT, network blip, cron not firing at all) never creates a run, so it shows
up here as a gap either way.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import requests

logger = logging.getLogger(__name__)

_TIMEOUT_SECONDS = 15
_DISPATCH_INTERVAL_MINUTES = 10  # each workflow is dispatched once per cycle
_MONITORED_WORKFLOWS = ["run_bot_on_minibench.yaml", "run_bot_on_seasonal.yaml"]
_DEFAULT_WARN_WITHIN_DAYS = 14


def count_dispatch_runs(repo: str, workflow_file: str, github_token: str, since: datetime) -> int | None:
    """Number of event=workflow_dispatch runs for this workflow created at
    or after `since`. Returns None (caller should treat as "unknown", not
    zero) on any API failure."""
    try:
        response = requests.get(
            f"https://api.github.com/repos/{repo}/actions/workflows/{workflow_file}/runs",
            headers={
                "Authorization": f"Bearer {github_token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            params={
                "event": "workflow_dispatch",
                "created": f">={since.strftime('%Y-%m-%d')}",
                "per_page": 1,  # only need the total_count, not the runs themselves
            },
            timeout=_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        return response.json()["total_count"]
    except Exception as e:
        logger.warning(f"Could not count dispatch runs for {workflow_file}: {e}")
        return None


def expected_dispatch_count(days: int, interval_minutes: int = _DISPATCH_INTERVAL_MINUTES) -> int:
    return int(days * 24 * 60 / interval_minutes)


def pat_expiry_warning(
    expires_on: str | None, today: datetime, warn_within_days: int = _DEFAULT_WARN_WITHIN_DAYS
) -> str | None:
    """expires_on: 'YYYY-MM-DD' or None/empty (repo variable not set yet).
    Returns a warning string if the PAT is within `warn_within_days` of
    expiring or already expired, else None."""
    if not expires_on:
        return None
    try:
        expiry = datetime.strptime(expires_on, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return f"Could not parse METACULUS_DISPATCH_PAT_EXPIRES={expires_on!r} (expected YYYY-MM-DD)."
    days_left = (expiry - today).days
    if days_left < 0:
        return (
            f"Dispatch PAT EXPIRED {-days_left} day(s) ago ({expires_on}) -- "
            "the VM backup poller is now failing silently (every dispatch call will 401)."
        )
    if days_left <= warn_within_days:
        return (
            f"Dispatch PAT expires in {days_left} day(s) ({expires_on}) -- "
            "rotate it on kalshi-bot-vm (~/metaculus-trigger/.env) soon."
        )
    return None


def generate_dispatch_health_report(
    repo: str,
    github_token: str | None,
    pat_expires_on: str | None,
    days: int = 7,
    *,
    today: datetime | None = None,
) -> str:
    today = today or datetime.now(timezone.utc)
    lines = ["\n=== Backup poller (kalshi-bot-vm dispatch) health ===\n"]

    warning = pat_expiry_warning(pat_expires_on, today)
    if warning:
        lines.append(f"  WARNING: {warning}")

    if not github_token:
        lines.append("  No GITHUB_TOKEN available -- skipping dispatch-rate check.")
        return "\n".join(lines)

    since = today - timedelta(days=days)
    expected = expected_dispatch_count(days)
    for workflow_file in _MONITORED_WORKFLOWS:
        actual = count_dispatch_runs(repo, workflow_file, github_token, since)
        if actual is None:
            lines.append(f"  {workflow_file}: could not check (API error)")
            continue
        rate_pct = (actual / expected * 100) if expected else 0.0
        lines.append(f"  {workflow_file}: {actual}/{expected} dispatches landed in last {days}d ({rate_pct:.0f}%)")

    return "\n".join(lines)
