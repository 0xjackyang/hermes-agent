"""Regression tests for crash-safe one-shot cron dispatch."""

from datetime import datetime, timezone
import os

import cron.jobs as jobs
import cron.scheduler as scheduler


def _fixed_now():
    return datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


def _configure_cron_paths(tmp_path, monkeypatch):
    cron_dir = tmp_path / "cron"
    monkeypatch.setattr(jobs, "CRON_DIR", cron_dir)
    monkeypatch.setattr(jobs, "JOBS_FILE", cron_dir / "jobs.json")
    monkeypatch.setattr(jobs, "OUTPUT_DIR", cron_dir / "output")
    monkeypatch.setattr(scheduler, "_LOCK_DIR", cron_dir)
    monkeypatch.setattr(scheduler, "_LOCK_FILE", cron_dir / ".tick.lock")


def test_mark_job_started_prevents_oneshot_rerun_and_recovers_after_restart(tmp_path, monkeypatch):
    _configure_cron_paths(tmp_path, monkeypatch)
    monkeypatch.setattr(jobs, "_hermes_now", _fixed_now)

    job = jobs.create_job(prompt="test one-shot", schedule=_fixed_now().isoformat())
    due = jobs.get_due_jobs()
    assert [item["id"] for item in due] == [job["id"]]

    claimed = jobs.mark_job_started(job["id"])
    assert claimed is not None
    assert claimed["state"] == "running"
    assert claimed["running_pid"] == os.getpid()
    assert claimed["next_run_at"] is None

    # The claiming process should not rediscover its own in-flight job.
    assert jobs.get_due_jobs() == []

    # A restarted process should treat the stale one-shot as abandoned rather
    # than runnable again.
    original_pid = os.getpid()
    monkeypatch.setattr(jobs.os, "getpid", lambda: original_pid + 1000)
    assert jobs.get_due_jobs() == []

    stored = jobs.get_job(job["id"])
    assert stored is not None
    assert stored["state"] == "abandoned"
    assert stored["enabled"] is False
    assert stored["last_status"] == "error"
    assert "completion state" in stored["last_error"]


def test_tick_claims_oneshot_before_run(tmp_path, monkeypatch):
    _configure_cron_paths(tmp_path, monkeypatch)
    monkeypatch.setattr(jobs, "_hermes_now", _fixed_now)
    monkeypatch.setattr(scheduler, "_hermes_now", _fixed_now)

    job = jobs.create_job(prompt="tick me once", schedule=_fixed_now().isoformat())

    def fake_run_job(claimed_job):
        stored = jobs.get_job(claimed_job["id"])
        assert stored is not None
        assert stored["state"] == "running"
        assert stored["next_run_at"] is None
        return True, "full output", "final response", None

    monkeypatch.setattr(scheduler, "run_job", fake_run_job)
    monkeypatch.setattr(scheduler, "_deliver_result", lambda *args, **kwargs: None)

    executed = scheduler.tick(verbose=False)
    assert executed == 1
    assert jobs.get_job(job["id"]) is None