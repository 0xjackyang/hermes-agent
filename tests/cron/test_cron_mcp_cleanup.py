"""Tests for cron subprocess MCP cleanup.

These tests cover the bounded fix for duplicate/orphaned MCP stdio children
spawned by cron ProcessPoolExecutor workers.
"""

import concurrent.futures
import sys
from pathlib import Path

import pytest

# Ensure project root is importable
sys.path.insert(0, str(Path(__file__).parent.parent.parent))


def test_run_job_in_process_worker_cleans_up_mcp_on_success(monkeypatch):
    from cron import scheduler as sched

    cleaned = []
    expected = (True, "output", "final", None)

    monkeypatch.setattr(sched, "run_job", lambda job: expected)
    monkeypatch.setattr(
        sched,
        "_shutdown_worker_mcp_servers",
        lambda: cleaned.append("cleanup"),
    )

    result = sched._run_job_in_process_worker({"id": "job-1"})

    assert result == expected
    assert cleaned == ["cleanup"]


def test_run_job_in_process_worker_cleans_up_mcp_on_failure(monkeypatch):
    from cron import scheduler as sched

    cleaned = []

    def _boom(job):
        raise RuntimeError("worker failed")

    monkeypatch.setattr(sched, "run_job", _boom)
    monkeypatch.setattr(
        sched,
        "_shutdown_worker_mcp_servers",
        lambda: cleaned.append("cleanup"),
    )

    with pytest.raises(RuntimeError, match="worker failed"):
        sched._run_job_in_process_worker({"id": "job-2"})

    assert cleaned == ["cleanup"]


def test_job_runner_for_executor_uses_worker_wrapper_for_process_pools():
    from cron import scheduler as sched

    assert (
        sched._job_runner_for_executor(concurrent.futures.ProcessPoolExecutor)
        is sched._run_job_in_process_worker
    )
    assert (
        sched._job_runner_for_executor(concurrent.futures.ThreadPoolExecutor)
        is sched.run_job
    )


def test_tick_submits_job_runner_selected_by_helper(monkeypatch, tmp_path):
    from cron import scheduler as sched

    submitted = []
    finalized = []

    def sentinel_runner(job):
        return True, f"out-{job['id']}", f"final-{job['id']}", None

    class InlineExecutor:
        def __init__(self, max_workers):
            self.max_workers = max_workers

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def submit(self, fn, job):
            submitted.append(fn)
            future = concurrent.futures.Future()
            try:
                future.set_result(fn(job))
            except Exception as exc:  # pragma: no cover - defensive
                future.set_exception(exc)
            return future

    jobs = [{"id": "job-a"}, {"id": "job-b"}]

    monkeypatch.setattr(sched, "_LOCK_DIR", tmp_path)
    monkeypatch.setattr(sched, "_LOCK_FILE", tmp_path / ".tick.lock")
    monkeypatch.setattr(sched, "get_due_jobs", lambda: list(jobs))
    monkeypatch.setattr(sched, "advance_next_run", lambda job_id: None)
    monkeypatch.setattr(sched, "mark_job_run", lambda *a, **k: None)
    monkeypatch.setattr(
        sched,
        "_finalize_completed_job",
        lambda job, success, output, final_response, error, **kwargs: finalized.append(
            (job["id"], success, output, final_response, error)
        ),
    )
    monkeypatch.setattr(sched, "_JOB_EXECUTOR_CLASS", InlineExecutor)
    monkeypatch.setattr(sched, "_job_runner_for_executor", lambda cls: sentinel_runner)

    executed = sched.tick(verbose=False)

    assert executed == 2
    assert submitted == [sentinel_runner, sentinel_runner]
    assert sorted(finalized) == [
        ("job-a", True, "out-job-a", "final-job-a", None),
        ("job-b", True, "out-job-b", "final-job-b", None),
    ]
