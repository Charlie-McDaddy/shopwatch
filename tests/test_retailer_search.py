"""Retailer search job state machine: claim/complete race-safety and staleness
recovery. Same discipline as test_llm_jobs.py - pure unit tests against sqlite, no
network, no Firecrawl. deploy/research-runner.py (the thing that actually claims and
answers a job) is a separate process this suite never touches - see
app/retailer_search.py's own module docstring.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from app import retailer_search


def _finish_initial(conn, product_id):
    row = conn.execute(
        "SELECT id FROM retailer_search_jobs WHERE product_id = ?"
        " AND status IN ('QUEUED', 'RUNNING')", (product_id,)
    ).fetchone()
    if row:
        retailer_search.complete_job(conn, row["id"], "DONE", result='{"retailers": []}')


def test_create_job_requires_a_query(conn):
    with pytest.raises(ValueError, match="query"):
        retailer_search.create_job(conn, "  ")


def test_create_job_starts_queued(conn):
    job_id = retailer_search.create_job(conn, json.dumps({"product_name": "Dreame RoboMower"}))
    conn.commit()

    job = retailer_search.get_job(conn, job_id)
    assert job["status"] == "QUEUED"
    assert job["result"] is None


def test_claim_next_queued_flips_status_and_sets_started_at(conn):
    job_id = retailer_search.create_job(conn, json.dumps({"product_name": "Dreame RoboMower"}))
    conn.commit()

    claimed = retailer_search.claim_next_queued(conn)
    conn.commit()

    assert claimed["id"] == job_id
    assert claimed["status"] == "RUNNING"
    assert claimed["started_at"] is not None


def test_claim_next_queued_is_empty_when_nothing_is_queued(conn):
    assert retailer_search.claim_next_queued(conn) is None


def test_claiming_twice_never_hands_out_the_same_job(conn):
    """The control this test exists for: two overlapping runner polls must not both
    walk away thinking they own the same job."""
    job_id = retailer_search.create_job(conn, json.dumps({"product_name": "Dreame RoboMower"}))
    conn.commit()

    first = retailer_search.claim_next_queued(conn)
    conn.commit()
    second = retailer_search.claim_next_queued(conn)

    assert first["id"] == job_id
    assert second is None


def test_complete_job_stores_the_result(conn):
    job_id = retailer_search.create_job(conn, json.dumps({"product_name": "Dreame RoboMower"}))
    conn.commit()
    retailer_search.claim_next_queued(conn)
    conn.commit()

    payload = json.dumps({"retailers": [{"name": "Bunnings", "homepage": "https://bunnings.com.au"}]})
    retailer_search.complete_job(conn, job_id, "DONE", result=payload)
    conn.commit()

    job = retailer_search.get_job(conn, job_id)
    assert job["status"] == "DONE"
    assert json.loads(job["result"])["retailers"][0]["name"] == "Bunnings"


def test_complete_job_rejects_a_non_terminal_status(conn):
    job_id = retailer_search.create_job(conn, json.dumps({"product_name": "Dreame RoboMower"}))
    conn.commit()
    with pytest.raises(ValueError, match="DONE or FAILED"):
        retailer_search.complete_job(conn, job_id, "RUNNING")


def test_a_job_stuck_running_past_the_ceiling_is_reconciled_to_failed(conn):
    """The control this test exists for: a killed runner must not look alive forever."""
    job_id = retailer_search.create_job(conn, json.dumps({"product_name": "Dreame RoboMower"}))
    conn.commit()
    long_ago = (
        datetime.now(UTC) - timedelta(seconds=retailer_search.JOB_CEILING_SECONDS + 30)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn.execute(
        "UPDATE retailer_search_jobs SET status = 'RUNNING', started_at = ? WHERE id = ?",
        (long_ago, job_id),
    )
    conn.commit()

    job = retailer_search.get_job(conn, job_id)  # get_job calls reconcile_stale itself

    assert job["status"] == "FAILED"
    assert "stale" in job["error"]


def test_a_job_running_within_the_ceiling_is_left_alone(conn):
    """The same control must NOT fail a job that is still genuinely in progress."""
    job_id = retailer_search.create_job(conn, json.dumps({"product_name": "Dreame RoboMower"}))
    conn.commit()
    retailer_search.claim_next_queued(conn)
    conn.commit()

    job = retailer_search.get_job(conn, job_id)

    assert job["status"] == "RUNNING"


def test_product_discovery_uses_exact_variant_and_known_retailers(conn):
    from app import store

    product_id = store.create_product(conn, {
        "name": "Dreame RoboMower", "model": "A1-2026", "variant": "4 Ah battery",
    })
    retailer_id = store.ensure_retailer(conn, "Bunnings", homepage="https://bunnings.com.au")["id"]
    store.create_listing(conn, {"product_id": product_id, "retailer_id": retailer_id})

    _finish_initial(conn, product_id)
    job_id = retailer_search.request_product_discovery(
        conn, product_id, now="2026-10-21T00:00:00Z"
    )

    job = retailer_search.get_job(conn, job_id)
    query = json.loads(job["query"])
    assert query == {
        "product_name": "Dreame RoboMower", "model": "A1-2026",
        "variant": "4 Ah battery",
        "excluded_names": ["Bunnings"], "excluded_homepages": ["https://bunnings.com.au"],
    }
    assert job["product_id"] == product_id
    assert job["trigger"] == "manual"


def test_product_discovery_rejects_archived_or_parked_products(conn):
    from app import store

    product_id = store.create_product(conn, {"name": "Lamp", "model": "L-1"})
    store.update_product(conn, product_id, {"archived": 1})
    before = conn.execute("SELECT COUNT(*) FROM retailer_search_jobs").fetchone()[0]
    with pytest.raises(ValueError, match="inactive"):
        retailer_search.request_product_discovery(conn, product_id)
    assert conn.execute("SELECT COUNT(*) FROM retailer_search_jobs").fetchone()[0] == before


def test_due_discovery_is_weekly_and_independent_of_price_schedule(conn):
    from app import store

    product_id = store.create_product(conn, {"name": "Lamp", "model": "L-1"})
    _finish_initial(conn, product_id)
    conn.execute(
        "UPDATE product_discovery_schedules SET next_due_at = '2026-10-20T00:00:00Z'"
        " WHERE product_id = ?", (product_id,)
    )
    jobs = retailer_search.queue_due(conn, now="2026-10-21T00:00:00Z")
    assert len(jobs) == 1
    assert json.loads(retailer_search.get_job(conn, jobs[0])["query"])["model"] == "L-1"
    assert retailer_search.queue_due(conn, now="2026-10-22T00:00:00Z") == []
    due = conn.execute(
        "SELECT next_due_at FROM product_discovery_schedules WHERE product_id = ?", (product_id,)
    ).fetchone()[0]
    assert due == "2026-10-28T00:00:00Z"


def test_late_discovery_completion_discards_results_when_product_is_archived(conn):
    from app import store

    product_id = store.create_product(conn, {"name": "Lamp", "model": "L-1"})
    job_id = retailer_search.request_product_discovery(conn, product_id)
    store.update_product(conn, product_id, {"archived": 1})
    retailer_search.complete_job(conn, job_id, "DONE", result='{"retailers": []}')
    job = retailer_search.get_job(conn, job_id)
    assert job["status"] == "FAILED"
    assert "inactive" in job["error"]


def test_completion_keeps_exact_model_page_as_unpriced_listing_once(conn):
    from app import store

    product_id = store.create_product(conn, {"name": "Soundbar", "model": "HW-Q930H/XY"})
    job_id = retailer_search.request_product_discovery(conn, product_id)
    retailer_search.complete_job(conn, job_id, "DONE", result=json.dumps({"retailers": [{
        "name": "Example Store", "homepage": "https://example.com.au",
        "source_url": "https://example.com.au/soundbar", "source_title": "Samsung HW-Q930H/XY Soundbar",
    }]}))
    result = json.loads(retailer_search.get_job(conn, job_id)["result"])
    assert result["retailers"][0]["source_url"] == "https://example.com.au/soundbar"
    listing = conn.execute(
        "SELECT l.url, l.advertised_price FROM listings l JOIN retailers r"
        " ON r.id = l.retailer_id WHERE l.product_id = ? AND r.name = 'Example Store'", (product_id,)
    ).fetchone()
    assert tuple(listing) == ("https://example.com.au/soundbar", None)

    second_job = retailer_search.create_job(conn, "{}", product_id=product_id)
    retailer_search.complete_job(conn, second_job, "DONE", result=json.dumps({"retailers": [{
        "name": "Example Store", "homepage": "https://example.com.au",
        "source_url": "https://example.com.au/soundbar", "source_title": "Samsung HW-Q930H/XY Soundbar",
    }]}))
    assert conn.execute("SELECT COUNT(*) FROM listings WHERE product_id = ?", (product_id,)).fetchone()[0] == 1


def test_variant_discovery_preserves_sibling_variant_page_for_review_without_attaching(conn):
    from app import store

    vanilla_id = store.create_product(conn, {
        "name": "Protein powder", "model": "PRO-1", "variant": "Vanilla 1 kg",
    })
    store.create_product(conn, {
        "name": "Protein powder", "model": "PRO-1", "variant": "Chocolate 1 kg",
    })
    job_id = retailer_search.request_product_discovery(conn, vanilla_id)
    retailer_search.complete_job(conn, job_id, "DONE", result=json.dumps({"retailers": [{
        "name": "Example Store", "homepage": "https://example.com.au",
        "source_url": "https://example.com.au/pro-1-chocolate",
        "source_title": "PRO-1 Chocolate 1 kg",
    }]}))

    result = json.loads(retailer_search.get_job(conn, job_id)["result"])
    candidate = result["retailers"][0]
    assert candidate["source_url"] == "https://example.com.au/pro-1-chocolate"
    assert candidate["source_title"] == "PRO-1 Chocolate 1 kg"
    assert candidate["needs_review"] is True
    assert conn.execute(
        "SELECT COUNT(*) FROM listings WHERE product_id = ?", (vanilla_id,)
    ).fetchone()[0] == 0


def test_completion_does_not_create_listing_for_another_model_or_homepage(conn):
    from app import store

    product_id = store.create_product(conn, {"name": "Soundbar", "model": "HW-Q930H/XY"})
    job_id = retailer_search.request_product_discovery(conn, product_id)
    retailer_search.complete_job(conn, job_id, "DONE", result=json.dumps({"retailers": [{
        "name": "Example Store", "homepage": "https://example.com.au",
        "source_url": "https://example.com.au/hw-q930", "source_title": "Samsung HW-Q930H/XU",
    }, {"name": "Homepage Only", "homepage": "https://homepage-only.com.au"}]}))
    assert conn.execute("SELECT COUNT(*) FROM listings WHERE product_id = ?", (product_id,)).fetchone()[0] == 0
