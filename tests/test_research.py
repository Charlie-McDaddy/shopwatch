"""Research-job state machine: concurrency guard, default-deny, and staleness recovery.

Pure unit tests against sqlite - no subprocess, no `claude` CLI, nothing that spends
real quota. That boundary is deliberate: the host-level runner is a separate process
this suite never touches (see app/research.py's module docstring).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from app import research, store


@pytest.fixture()
def product_id(conn):
    pid = store.create_product(conn, {"name": "Test TV", "model": "TEST-MODEL-1"})
    conn.commit()
    return pid


@pytest.fixture()
def retailer_ids(conn):
    ids = [
        store.ensure_retailer(conn, "JB Hi-Fi")["id"],
        store.ensure_retailer(conn, "Harvey Norman")["id"],
    ]
    conn.commit()
    return ids


def test_create_job_queues_a_result_row_per_known_retailer(conn, product_id, retailer_ids):
    job_id = research.create_job(conn, product_id, retailer_ids)
    conn.commit()

    job = research.get_job(conn, job_id)
    assert job["status"] == "QUEUED"
    assert {r["retailer_id"] for r in job["results"]} == set(retailer_ids)
    assert all(r["status"] == "PENDING" for r in job["results"])


def test_create_job_silently_drops_unknown_retailer_ids(conn, product_id, retailer_ids):
    """Default-deny: an id that isn't a real retailer never reaches the job at all."""
    bogus_id = 999999
    job_id = research.create_job(conn, product_id, [*retailer_ids, bogus_id])
    conn.commit()

    job = research.get_job(conn, job_id)
    assert {r["retailer_id"] for r in job["results"]} == set(retailer_ids)


def test_create_job_rejects_all_unknown_retailers(conn, product_id):
    with pytest.raises(ValueError, match="no known retailers"):
        research.create_job(conn, product_id, [999999])


def test_second_active_job_for_same_product_is_rejected(conn, product_id, retailer_ids):
    """The concurrency guard: a double-click must not spend the shared quota twice."""
    first_id = research.create_job(conn, product_id, retailer_ids)
    conn.commit()

    with pytest.raises(research.JobAlreadyRunning) as exc_info:
        research.create_job(conn, product_id, retailer_ids)
    assert exc_info.value.job_id == first_id


def test_a_completed_job_does_not_block_a_new_one(conn, product_id, retailer_ids):
    first_id = research.create_job(conn, product_id, retailer_ids)
    conn.commit()
    research.complete_job(conn, first_id, "DONE")
    conn.commit()

    second_id = research.create_job(conn, product_id, retailer_ids)  # must not raise
    assert second_id != first_id


def test_get_latest_job_for_product_returns_none_with_no_history(conn, product_id):
    assert research.get_latest_job_for_product(conn, product_id) is None


def test_get_latest_job_for_product_returns_the_most_recent_one(conn, product_id, retailer_ids):
    first_id = research.create_job(conn, product_id, retailer_ids)
    conn.commit()
    research.complete_job(conn, first_id, "DONE")
    conn.commit()
    second_id = research.create_job(conn, product_id, retailer_ids)
    conn.commit()

    latest = research.get_latest_job_for_product(conn, product_id)

    assert latest["id"] == second_id
    assert latest["id"] != first_id


def test_claim_next_queued_flips_status_and_sets_started_at(conn, product_id, retailer_ids):
    job_id = research.create_job(conn, product_id, retailer_ids)
    conn.commit()

    claimed = research.claim_next_queued(conn)
    conn.commit()

    assert claimed["id"] == job_id
    assert claimed["status"] == "RUNNING"
    assert claimed["started_at"] is not None


def test_claim_next_queued_returns_none_when_nothing_queued(conn):
    assert research.claim_next_queued(conn) is None


def test_report_result_then_complete_marks_the_job_done(conn, product_id, retailer_ids):
    job_id = research.create_job(conn, product_id, retailer_ids)
    conn.commit()
    research.claim_next_queued(conn)
    conn.commit()

    research.report_result(conn, job_id, retailer_ids[0], "FOUND", note="found it")
    research.report_result(conn, job_id, retailer_ids[1], "NEEDS_MANUAL_CHECK", note="blocked")
    research.complete_job(conn, job_id, "DONE")
    conn.commit()

    job = research.get_job(conn, job_id)
    assert job["status"] == "DONE"
    by_retailer = {r["retailer_id"]: r["status"] for r in job["results"]}
    assert by_retailer[retailer_ids[0]] == "FOUND"
    assert by_retailer[retailer_ids[1]] == "NEEDS_MANUAL_CHECK"


def test_report_result_rejects_an_unknown_status(conn, product_id, retailer_ids):
    job_id = research.create_job(conn, product_id, retailer_ids)
    conn.commit()
    with pytest.raises(ValueError):
        research.report_result(conn, job_id, retailer_ids[0], "MADE_UP_STATUS")


def test_a_job_stuck_running_past_the_ceiling_is_reconciled_to_failed(
    conn, product_id, retailer_ids
):
    """The control this test exists for: a wedged host script must not look alive forever."""
    job_id = research.create_job(conn, product_id, retailer_ids)
    conn.commit()
    long_ago = (datetime.now(UTC) - timedelta(seconds=research.JOB_CEILING_SECONDS + 60)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    conn.execute(
        "UPDATE research_jobs SET status = 'RUNNING', started_at = ? WHERE id = ?",
        (long_ago, job_id),
    )
    conn.commit()

    job = research.get_job(conn, job_id)  # get_job calls reconcile_stale itself

    assert job["status"] == "FAILED"
    assert "stale" in job["error"]


def test_a_job_running_within_the_ceiling_is_left_alone(conn, product_id, retailer_ids):
    """The same control must NOT fail a job that is still genuinely in progress."""
    job_id = research.create_job(conn, product_id, retailer_ids)
    conn.commit()
    research.claim_next_queued(conn)
    conn.commit()

    job = research.get_job(conn, job_id)

    assert job["status"] == "RUNNING"


def test_create_job_records_a_url_for_the_retailer_it_was_given(
    conn, product_id, retailer_ids
):
    """The "paste a listing URL" path (issue #94): a job scoped to one URL must carry
    it through to the result row, so the runner can read that exact page instead of
    searching generically."""
    url = "https://www.example.com.au/some-product"
    job_id = research.create_job(
        conn, product_id, [retailer_ids[0]], urls={retailer_ids[0]: url}
    )
    conn.commit()

    job = research.get_job(conn, job_id)
    by_retailer = {r["retailer_id"]: r["url"] for r in job["results"]}
    assert by_retailer[retailer_ids[0]] == url


def test_create_job_without_urls_leaves_url_null(conn, product_id, retailer_ids):
    """The ordinary wizard retailer-picker path (no urls argument at all) must not
    regress: every result row's url stays null, exactly as before this column existed."""
    job_id = research.create_job(conn, product_id, retailer_ids)
    conn.commit()

    job = research.get_job(conn, job_id)
    assert all(r["url"] is None for r in job["results"])


def test_create_job_only_sets_url_for_the_retailer_it_was_given_for(
    conn, product_id, retailer_ids
):
    """A urls dict naming only one of several retailers must not leak that URL onto
    the others - each result row's url is specific to its own retailer."""
    url = "https://www.example.com.au/some-product"
    job_id = research.create_job(
        conn, product_id, retailer_ids, urls={retailer_ids[0]: url}
    )
    conn.commit()

    job = research.get_job(conn, job_id)
    by_retailer = {r["retailer_id"]: r["url"] for r in job["results"]}
    assert by_retailer[retailer_ids[0]] == url
    assert by_retailer[retailer_ids[1]] is None


def test_reconciling_a_stale_running_job_frees_the_product_for_a_new_one(
    conn, product_id, retailer_ids
):
    job_id = research.create_job(conn, product_id, retailer_ids)
    conn.commit()
    long_ago = (datetime.now(UTC) - timedelta(seconds=research.JOB_CEILING_SECONDS + 60)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    conn.execute(
        "UPDATE research_jobs SET status = 'RUNNING', started_at = ? WHERE id = ?",
        (long_ago, job_id),
    )
    conn.commit()
    research.reconcile_stale(conn)
    conn.commit()

    new_job_id = research.create_job(conn, product_id, retailer_ids)  # must not raise
    assert new_job_id != job_id


# --------------------------------------------------------- historical-low kind (#93)


def test_create_job_defaults_to_price_kind(conn, product_id, retailer_ids):
    job_id = research.create_job(conn, product_id, retailer_ids)
    conn.commit()

    job = research.get_job(conn, job_id)
    assert job["kind"] == "price"


def test_create_job_rejects_an_unknown_kind(conn, product_id, retailer_ids):
    with pytest.raises(ValueError, match="kind must be one of"):
        research.create_job(conn, product_id, retailer_ids, kind="made_up_kind")


def test_historical_low_job_needs_no_retailer_selection(conn, product_id):
    """The control this test exists for: "has this ever been cheaper" is one question
    about the whole product, not one per retailer - unlike a price job, an empty
    retailer_ids must not be rejected."""
    job_id = research.create_job(conn, product_id, [], kind="historical_low")
    conn.commit()

    job = research.get_job(conn, job_id)
    assert job["kind"] == "historical_low"
    assert job["results"] == []


def test_historical_low_job_ignores_any_retailer_ids_passed(conn, product_id, retailer_ids):
    """retailer_ids is meaningless for this kind - passing some anyway must not create
    research_job_results rows, since nothing will ever report against them."""
    job_id = research.create_job(conn, product_id, retailer_ids, kind="historical_low")
    conn.commit()

    job = research.get_job(conn, job_id)
    assert job["results"] == []


def test_a_historical_low_job_and_a_price_job_share_the_one_active_guard(
    conn, product_id, retailer_ids
):
    """Both kinds spend the same shared, credentialed CLI quota, so a product must
    never have two active jobs at once regardless of which kind either one is."""
    first_id = research.create_job(conn, product_id, [], kind="historical_low")
    conn.commit()

    with pytest.raises(research.JobAlreadyRunning) as exc_info:
        research.create_job(conn, product_id, retailer_ids, kind="price")
    assert exc_info.value.job_id == first_id


def test_report_historical_low_sets_the_finding_on_the_job(conn, product_id):
    job_id = research.create_job(conn, product_id, [], kind="historical_low")
    conn.commit()

    research.report_historical_low(
        conn, job_id, price=899.0, date="2025-11-20", retailer="Appliances Online",
        notes="Found via a Black Friday price-history thread.", confidence="MEDIUM",
    )
    research.complete_job(conn, job_id, "DONE")
    conn.commit()

    job = research.get_job(conn, job_id)
    assert job["historical_low_price"] == 899.0
    assert job["historical_low_date"] == "2025-11-20"
    assert job["historical_low_retailer"] == "Appliances Online"
    assert job["historical_low_confidence"] == "MEDIUM"
    assert "Black Friday" in job["historical_low_notes"]


def test_report_historical_low_accepts_an_empty_finding(conn, product_id):
    """No confident historical low found is a legitimate, honest outcome - not an
    error - so price=None with just a note must be storable."""
    job_id = research.create_job(conn, product_id, [], kind="historical_low")
    conn.commit()

    research.report_historical_low(conn, job_id, notes="No trustworthy source found.")
    conn.commit()

    job = research.get_job(conn, job_id)
    assert job["historical_low_price"] is None
    assert job["historical_low_notes"] == "No trustworthy source found."


def test_report_historical_low_rejects_an_unknown_confidence(conn, product_id):
    job_id = research.create_job(conn, product_id, [], kind="historical_low")
    conn.commit()
    with pytest.raises(ValueError, match="confidence must be one of"):
        research.report_historical_low(conn, job_id, price=100, confidence="VERY_SURE")


def test_reporting_a_historical_low_never_touches_the_product(conn, product_id):
    """The control this test exists for: nothing about this feature is allowed to
    write products.lowest_known_price directly - only a human copying the estimate
    into the ordinary edit form (PATCH /api/products/{id}) can do that."""
    job_id = research.create_job(conn, product_id, [], kind="historical_low")
    conn.commit()
    research.report_historical_low(conn, job_id, price=42.0, confidence="HIGH")
    research.complete_job(conn, job_id, "DONE")
    conn.commit()

    product = store.get_product(conn, product_id)
    assert product["lowest_known_price"] is None


# ----------------------------------------- other retailers noticed along the way (#98)


def test_report_historical_low_stores_other_retailers_as_json(conn, product_id):
    job_id = research.create_job(conn, product_id, [], kind="historical_low")
    conn.commit()

    research.report_historical_low(
        conn, job_id, price=899.0,
        other_retailers=[
            {"name": "Centre Com", "url": "https://www.centrecom.com.au/x"},
            {"name": "Mwave", "url": None},
        ],
    )
    conn.commit()

    job = research.get_job(conn, job_id)
    assert json.loads(job["historical_low_other_retailers"]) == [
        {"name": "Centre Com", "url": "https://www.centrecom.com.au/x"},
        {"name": "Mwave", "url": None},
    ]


def test_report_historical_low_with_no_other_retailers_stores_null(conn, product_id):
    job_id = research.create_job(conn, product_id, [], kind="historical_low")
    conn.commit()

    research.report_historical_low(conn, job_id, price=899.0)
    conn.commit()

    job = research.get_job(conn, job_id)
    assert job["historical_low_other_retailers"] is None


def test_report_historical_low_drops_an_other_retailer_with_no_name(conn, product_id):
    job_id = research.create_job(conn, product_id, [], kind="historical_low")
    conn.commit()

    research.report_historical_low(
        conn, job_id, price=899.0,
        other_retailers=[{"name": "  ", "url": "https://x.com.au"}, "not a dict"],
    )
    conn.commit()

    job = research.get_job(conn, job_id)
    assert job["historical_low_other_retailers"] is None


def test_report_historical_low_neutralises_a_javascript_url_in_other_retailers(conn, product_id):
    """Same discipline as the runner's own _clean_other_retailers: candidate.url
    ultimately came out of an LLM's reply to a "search the web" prompt, and this
    server-side boundary must not trust the runner to have already filtered it -
    a non-http(s) scheme must never reach storage, since the product page renders
    this value straight into an anchor's href."""
    job_id = research.create_job(conn, product_id, [], kind="historical_low")
    conn.commit()

    research.report_historical_low(
        conn, job_id, price=899.0,
        other_retailers=[{"name": "Evil Co", "url": "javascript:alert(1)"}],
    )
    conn.commit()

    job = research.get_job(conn, job_id)
    assert json.loads(job["historical_low_other_retailers"]) == [
        {"name": "Evil Co", "url": None},
    ]


# ------------------------------------------------ excluded retailers dropped (#112)


def test_report_historical_low_drops_a_candidate_matching_an_excluded_retailer(
    conn, product_id
):
    """issue #112 item 6: the historical-low "other retailers found" list must never
    surface an excluded retailer as a tick-to-add option - filtered here, the actual
    boundary that writes to the database, not only trusted to the runner's prompt."""
    bing_lee = store.ensure_retailer(conn, "Bing Lee")
    store.set_retailer_excluded(conn, bing_lee["id"], True)
    conn.commit()

    job_id = research.create_job(conn, product_id, [], kind="historical_low")
    conn.commit()

    research.report_historical_low(
        conn, job_id, price=899.0,
        other_retailers=[
            {"name": "Bing Lee", "url": "https://www.binglee.com.au/x"},
            {"name": "Centre Com", "url": "https://www.centrecom.com.au/x"},
        ],
    )
    conn.commit()

    job = research.get_job(conn, job_id)
    assert json.loads(job["historical_low_other_retailers"]) == [
        {"name": "Centre Com", "url": "https://www.centrecom.com.au/x"},
    ]


def test_report_historical_low_matches_an_excluded_retailer_case_insensitively(
    conn, product_id
):
    bing_lee = store.ensure_retailer(conn, "Bing Lee")
    store.set_retailer_excluded(conn, bing_lee["id"], True)
    conn.commit()

    job_id = research.create_job(conn, product_id, [], kind="historical_low")
    conn.commit()

    research.report_historical_low(
        conn, job_id, price=899.0,
        other_retailers=[{"name": "bing lee", "url": "https://www.binglee.com.au/x"}],
    )
    conn.commit()

    job = research.get_job(conn, job_id)
    assert job["historical_low_other_retailers"] is None


def test_report_historical_low_keeps_a_candidate_once_it_is_un_excluded(conn, product_id):
    """Control for the drop test above: exclusion is live-checked against the
    retailers table, not a one-off snapshot."""
    bing_lee = store.ensure_retailer(conn, "Bing Lee")
    store.set_retailer_excluded(conn, bing_lee["id"], True)
    store.set_retailer_excluded(conn, bing_lee["id"], False)
    conn.commit()

    job_id = research.create_job(conn, product_id, [], kind="historical_low")
    conn.commit()

    research.report_historical_low(
        conn, job_id, price=899.0,
        other_retailers=[{"name": "Bing Lee", "url": "https://www.binglee.com.au/x"}],
    )
    conn.commit()

    job = research.get_job(conn, job_id)
    assert json.loads(job["historical_low_other_retailers"]) == [
        {"name": "Bing Lee", "url": "https://www.binglee.com.au/x"},
    ]


def test_paused_product_cannot_start_or_resume_queued_research(conn, product_id, retailer_ids):
    job_id = research.create_job(conn, product_id, retailer_ids)
    store.update_product(conn, product_id, {"status": "PARKED"})
    conn.commit()

    with pytest.raises(ValueError, match="paused or archived"):
        research.create_job(conn, product_id, retailer_ids)
    assert research.claim_next_queued(conn) is None

    store.update_product(conn, product_id, {"status": "ACTIVE"})
    conn.commit()
    assert research.claim_next_queued(conn)["id"] == job_id
