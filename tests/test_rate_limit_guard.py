"""
Regression test for a real incident (2026-10-19): main.py's rate-limit
guard used to exempt only GITHUB_EVENT_NAME == "schedule" runs from the
local/manual-run OpenRouter quota check. The GCP VM backup poller (added
because GitHub's own schedule: trigger was silently skipping ~96% of
ticks -- see docs/STAGE_B_PLAN.md "Backup poller") fires via
workflow_dispatch, not schedule, so once free-tier quota dipped below the
15-request reserve partway through the day, every backup-poller-triggered
run (the *actual* production runs at that point) got refused, while the
few remaining native schedule: ticks kept succeeding -- both workflows
failed for roughly an hour before this was caught.

Fix: IS_CI_RUN now checks GITHUB_ACTIONS == "true" (set for every GitHub
Actions run regardless of trigger event) instead of
GITHUB_EVENT_NAME == "schedule" -- the real distinction the guard needs
is "running in CI" vs "someone's laptop," not which trigger fired it.
"""
from __future__ import annotations

import importlib
import os
from types import SimpleNamespace

import main as main_module


def _cfg(*, enforce: bool = True, reserve: int = 15) -> SimpleNamespace:
    return SimpleNamespace(enforce_rate_limit_guard=enforce, min_quota_reserve_for_scheduled_run=reserve)


def test_is_ci_run_true_when_github_actions_env_set(monkeypatch):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    reloaded = importlib.reload(main_module)
    assert reloaded.IS_CI_RUN is True


def test_is_ci_run_false_when_unset_locally(monkeypatch):
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    reloaded = importlib.reload(main_module)
    assert reloaded.IS_CI_RUN is False
    importlib.reload(main_module)  # restore a clean module state for later tests


def test_workflow_dispatch_triggered_ci_run_is_exempt_from_quota_guard(monkeypatch):
    """The actual bug: a workflow_dispatch-triggered CI run (the backup
    poller) must be treated the same as a schedule-triggered one, not
    blocked once quota is low."""
    monkeypatch.setattr(main_module, "IS_CI_RUN", True)
    monkeypatch.setenv("GITHUB_EVENT_NAME", "workflow_dispatch")

    def fake_quota():
        raise AssertionError("quota should never be checked when IS_CI_RUN is True")

    monkeypatch.setattr(main_module, "get_free_model_daily_quota", fake_quota)

    assert main_module._check_rate_limit_guard(_cfg()) is True


def test_local_run_with_low_quota_is_refused(monkeypatch):
    monkeypatch.setattr(main_module, "IS_CI_RUN", False)
    monkeypatch.setattr(
        main_module, "get_free_model_daily_quota", lambda: SimpleNamespace(used=38, limit=50, remaining=12)
    )
    assert main_module._check_rate_limit_guard(_cfg(reserve=15)) is False


def test_local_run_with_low_quota_but_guard_disabled_proceeds(monkeypatch):
    monkeypatch.setattr(main_module, "IS_CI_RUN", False)
    monkeypatch.setattr(
        main_module, "get_free_model_daily_quota", lambda: SimpleNamespace(used=38, limit=50, remaining=12)
    )
    assert main_module._check_rate_limit_guard(_cfg(enforce=False, reserve=15)) is True


def test_local_run_with_sufficient_quota_proceeds(monkeypatch):
    monkeypatch.setattr(main_module, "IS_CI_RUN", False)
    monkeypatch.setattr(
        main_module, "get_free_model_daily_quota", lambda: SimpleNamespace(used=10, limit=50, remaining=40)
    )
    assert main_module._check_rate_limit_guard(_cfg(reserve=15)) is True


def test_quota_check_failure_proceeds_anyway(monkeypatch):
    monkeypatch.setattr(main_module, "IS_CI_RUN", False)
    monkeypatch.setattr(main_module, "get_free_model_daily_quota", lambda: None)
    assert main_module._check_rate_limit_guard(_cfg()) is True


def teardown_module(_module) -> None:
    # Leave the module in a clean, env-independent state for any test file
    # collected after this one in the same pytest session.
    os.environ.pop("GITHUB_ACTIONS", None)
    importlib.reload(main_module)
