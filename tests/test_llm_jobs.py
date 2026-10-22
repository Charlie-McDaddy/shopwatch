"""LLM helper job state machine: claim/complete race-safety and staleness recovery.

Pure unit tests against sqlite - no subprocess, no `claude`/`codex` CLI, nothing that
spends real quota. The host runner is a separate process this suite never touches -
see app/llm_jobs.py's own module docstring.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from app import llm_jobs


def test_create_job_requires_a_query(conn):
    with pytest.raises(ValueError, match="query"):
        llm_jobs.create_job(conn, "  ")


def test_create_job_starts_queued(conn):
    job_id = llm_jobs.create_job(conn, "Dreame RoboMower")
    conn.commit()

    job = llm_jobs.get_job(conn, job_id)
    assert job["status"] == "QUEUED"
    assert job["query"] == "Dreame RoboMower"
    assert job["result"] is None
    assert job["kind"] == "model_suggestion"  # the default, unchanged for old callers


def test_create_job_accepts_the_retailer_discovery_kind(conn):
    job_id = llm_jobs.create_job(conn, "Dreame RoboMower, checking: Bunnings", kind="retailer_discovery")
    conn.commit()

    assert llm_jobs.get_job(conn, job_id)["kind"] == "retailer_discovery"


def test_create_job_rejects_an_unknown_kind(conn):
    with pytest.raises(ValueError, match="kind"):
        llm_jobs.create_job(conn, "Dreame RoboMower", kind="not_a_real_kind")


def test_claim_next_queued_flips_status_and_sets_started_at(conn):
    job_id = llm_jobs.create_job(conn, "Dreame RoboMower")
    conn.commit()

    claimed = llm_jobs.claim_next_queued(conn)
    conn.commit()

    assert claimed["id"] == job_id
    assert claimed["status"] == "RUNNING"
    assert claimed["started_at"] is not None


def test_claim_next_queued_is_empty_when_nothing_is_queued(conn):
    assert llm_jobs.claim_next_queued(conn) is None


def test_claiming_twice_never_hands_out_the_same_job(conn):
    """The control this test exists for: two overlapping runner polls (or two runners)
    must not both walk away thinking they own the same job."""
    job_id = llm_jobs.create_job(conn, "Dreame RoboMower")
    conn.commit()

    first = llm_jobs.claim_next_queued(conn)
    conn.commit()
    second = llm_jobs.claim_next_queued(conn)

    assert first["id"] == job_id
    assert second is None


def test_complete_job_stores_the_result(conn):
    job_id = llm_jobs.create_job(conn, "Dreame RoboMower")
    conn.commit()
    llm_jobs.claim_next_queued(conn)
    conn.commit()

    payload = json.dumps({"candidates": [{"model": "DREAME-A2", "label": "Dreame A2"}]})
    llm_jobs.complete_job(conn, job_id, "DONE", result=payload)
    conn.commit()

    job = llm_jobs.get_job(conn, job_id)
    assert job["status"] == "DONE"
    assert json.loads(job["result"])["candidates"][0]["model"] == "DREAME-A2"


def test_complete_job_rejects_a_non_terminal_status(conn):
    job_id = llm_jobs.create_job(conn, "Dreame RoboMower")
    conn.commit()
    with pytest.raises(ValueError, match="DONE or FAILED"):
        llm_jobs.complete_job(conn, job_id, "RUNNING")


def test_a_job_stuck_running_past_the_ceiling_is_reconciled_to_failed(conn):
    """The control this test exists for: a killed runner must not look alive forever."""
    job_id = llm_jobs.create_job(conn, "Dreame RoboMower")
    conn.commit()
    long_ago = (
        datetime.now(UTC) - timedelta(seconds=llm_jobs.JOB_CEILING_SECONDS + 30)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn.execute(
        "UPDATE llm_jobs SET status = 'RUNNING', started_at = ? WHERE id = ?",
        (long_ago, job_id),
    )
    conn.commit()

    job = llm_jobs.get_job(conn, job_id)  # get_job calls reconcile_stale itself

    assert job["status"] == "FAILED"
    assert "stale" in job["error"]


def test_a_job_running_within_the_ceiling_is_left_alone(conn):
    """The same control must NOT fail a job that is still genuinely in progress."""
    job_id = llm_jobs.create_job(conn, "Dreame RoboMower")
    conn.commit()
    llm_jobs.claim_next_queued(conn)
    conn.commit()

    job = llm_jobs.get_job(conn, job_id)

    assert job["status"] == "RUNNING"


def test_completed_model_candidates_keep_valid_card_metadata_only(conn):
    job_id = llm_jobs.create_job(conn, "Protein powder")
    payload = json.dumps({"candidates": [{
        "model": "WHEY-9", "label": "Whey", "variant": "Vanilla 1 kg",
        "description": "Vanilla whey protein", "image_url": "https://images.example/whey.jpg",
    }, {
        "model": "WHEY-10", "variant": ["bad"], "image_url": "https://127.0.0.1/private.jpg",
    }]})
    llm_jobs.complete_job(conn, job_id, "DONE", result=payload)
    candidates = json.loads(llm_jobs.get_job(conn, job_id)["result"])["candidates"]
    assert candidates[0]["variant"] == "Vanilla 1 kg"
    assert candidates[0]["description"] == "Vanilla whey protein"
    assert candidates[0]["image_url"] == "https://images.example/whey.jpg"
    assert candidates[1] == {"model": "WHEY-10", "label": "WHEY-10"}
