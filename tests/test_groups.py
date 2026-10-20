"""Watch groups: several different products tracked as competing candidates.

Purchasing one candidate leaves every other candidate watched until it is explicitly
paused or removed.
"""

from __future__ import annotations

import pytest

from app import research, store


@pytest.fixture()
def two_candidates(conn):
    """Two standalone products, not yet grouped."""
    a = store.create_product(conn, {"name": "RTX 4070", "model": "RTX-4070-A"})
    b = store.create_product(conn, {"name": "RX 7800 XT", "model": "RX-7800-XT-A"})
    conn.commit()
    return a, b


def test_create_group_requires_a_name(conn):
    with pytest.raises(ValueError, match="name"):
        store.create_group(conn, {"notes": "no name given"})


def test_set_product_group_attaches_and_detaches(conn, two_candidates):
    a, _b = two_candidates
    group_id = store.create_group(conn, {"name": "GPU search"})
    conn.commit()

    store.set_product_group(conn, a, group_id)
    conn.commit()
    assert store.get_product(conn, a)["group_id"] == group_id

    store.set_product_group(conn, a, None)
    conn.commit()
    assert store.get_product(conn, a)["group_id"] is None


def test_group_view_assembles_members_and_merged_scale(conn, two_candidates):
    a, b = two_candidates
    group_id = store.create_group(conn, {"name": "GPU search"})
    conn.commit()
    store.set_product_group(conn, a, group_id)
    store.set_product_group(conn, b, group_id)
    conn.commit()

    retailer = store.ensure_retailer(conn, "JB Hi-Fi")
    store.create_listing(conn, {
        "product_id": a, "retailer_id": retailer["id"], "advertised_price": 899,
    })
    store.create_listing(conn, {
        "product_id": b, "retailer_id": retailer["id"], "advertised_price": 749,
    })
    conn.commit()

    group = store.group_view(conn, group_id)
    assert len(group["members"]) == 2
    assert group["scale"] is not None
    assert len(group["scale"]["points"]) == 2
    # Cheapest point should be positioned lower than the dearest.
    values = sorted(p["value"] for p in group["scale"]["points"])
    assert values == [749.0, 899.0]


def test_group_view_best_is_the_cheapest_non_ruled_out_point(conn, two_candidates):
    a, b = two_candidates
    group_id = store.create_group(conn, {"name": "GPU search"})
    conn.commit()
    store.set_product_group(conn, a, group_id)
    store.set_product_group(conn, b, group_id)
    conn.commit()

    retailer = store.ensure_retailer(conn, "JB Hi-Fi")
    la = store.create_listing(conn, {
        "product_id": a, "retailer_id": retailer["id"], "advertised_price": 500,
    })
    store.create_listing(conn, {
        "product_id": b, "retailer_id": retailer["id"], "advertised_price": 749,
    })
    conn.commit()

    group = store.group_view(conn, group_id)
    assert group["best"]["value"] == 500.0

    # Rule out the cheapest one - it must never win, however cheap.
    conn.execute("UPDATE listings SET ruled_out = 1 WHERE id = ?", (la,))
    conn.commit()
    group = store.group_view(conn, group_id)
    assert group["best"]["value"] == 749.0


def test_group_view_with_no_priced_listings_has_no_scale(conn, two_candidates):
    a, _b = two_candidates
    group_id = store.create_group(conn, {"name": "GPU search"})
    conn.commit()
    store.set_product_group(conn, a, group_id)
    conn.commit()

    group = store.group_view(conn, group_id)
    assert group["scale"] is None


def test_list_groups_excludes_archived_by_default(conn):
    group_id = store.create_group(conn, {"name": "GPU search"})
    conn.commit()
    store.update_group(conn, group_id, {"name": "GPU search"})  # touch, no-op
    conn.execute("UPDATE watch_groups SET archived = 1 WHERE id = ?", (group_id,))
    conn.commit()

    assert store.list_groups(conn) == []
    assert len(store.list_groups(conn, include_archived=True)) == 1


def test_list_products_exclude_grouped_hides_grouped_products(conn, two_candidates):
    a, b = two_candidates
    group_id = store.create_group(conn, {"name": "GPU search"})
    conn.commit()
    store.set_product_group(conn, a, group_id)
    conn.commit()

    all_products = store.list_products(conn)
    ungrouped_only = store.list_products(conn, exclude_grouped=True)
    assert {p["id"] for p in all_products} == {a, b}
    assert {p["id"] for p in ungrouped_only} == {b}


def test_status_counts_excludes_a_purchased_member_of_a_still_active_group(conn, two_candidates):
    """Buying a product before it is ever grouped skips the archive cascade
    entirely (there is no group yet to archive), so attaching it to a group
    afterwards is the one path that leaves a PURCHASED product sitting in a
    group that never resolved - exactly the state that made "Bought" count one
    with nothing behind it.
    """
    a, b = two_candidates
    store.record_purchase(conn, a, {"price_paid": 899})
    conn.commit()
    assert store.get_product(conn, a)["status"] == "PURCHASED"
    assert store.get_product(conn, a)["group_id"] is None

    # Control: before grouping, the purchase counts normally.
    assert store.status_counts(conn).get("PURCHASED") == 1

    group_id = store.create_group(conn, {"name": "GPU search"})
    conn.commit()
    store.set_product_group(conn, a, group_id)
    conn.commit()
    assert store.get_group(conn, group_id)["archived"] == 0  # group stays active

    counts = store.status_counts(conn)
    assert counts.get("PURCHASED", 0) == 0
    assert counts["ACTIVE"] == 1  # b, untouched
    assert counts["ALL"] == 1


def test_buying_one_candidate_keeps_the_other_candidates_watched(conn, two_candidates):
    a, b = two_candidates
    group_id = store.create_group(conn, {"name": "GPU search"})
    conn.commit()
    store.set_product_group(conn, a, group_id)
    store.set_product_group(conn, b, group_id)
    conn.commit()

    store.record_purchase(conn, a, {"price_paid": 899})
    conn.commit()

    bought = store.get_product(conn, a)
    other = store.get_product(conn, b)
    assert bought["status"] == "PURCHASED"
    assert not bought["archived"]  # the purchased one itself is not archived
    assert other["archived"] == 0
    assert other["status"] == "ACTIVE"
    assert store.get_group(conn, group_id)["archived"] == 0
    group = store.group_view(conn, group_id)
    assert {member["id"] for member in group["members"]} == {a, b}


def test_buying_a_standalone_product_does_not_touch_unrelated_products(conn, two_candidates):
    """No group membership means no cascade - archiving the wrong thing would be worse
    than not archiving anything."""
    a, b = two_candidates
    store.record_purchase(conn, a, {"price_paid": 899})
    conn.commit()

    assert not store.get_product(conn, b)["archived"]


def test_undo_purchase_leaves_group_siblings_unchanged(conn, two_candidates):
    a, b = two_candidates
    group_id = store.create_group(conn, {"name": "GPU search"})
    conn.commit()
    store.set_product_group(conn, a, group_id)
    store.set_product_group(conn, b, group_id)
    conn.commit()
    store.record_purchase(conn, a, {"price_paid": 899})
    conn.commit()

    store.undo_purchase(conn, a)
    conn.commit()

    assert store.get_product(conn, a)["status"] == "ACTIVE"
    assert store.get_product(conn, b)["archived"] == 0  # still watched throughout


def test_group_and_candidates_stay_visible_on_board_after_purchase(
    conn, two_candidates
):
    """A purchase changes one candidate's state without hiding the group or its peers."""
    a, b = two_candidates
    group_id = store.create_group(conn, {"name": "GPU search"})
    conn.commit()
    store.set_product_group(conn, a, group_id)
    store.set_product_group(conn, b, group_id)
    conn.commit()

    store.record_purchase(conn, a, {"price_paid": 899})
    conn.commit()

    # The group card remains visible, and names both the purchase and active alternative.
    groups = store.list_groups(conn)
    assert len(groups) == 1
    assert {member["id"] for member in groups[0]["members"]} == {a, b}
    assert store.get_product(conn, a)["status"] == "PURCHASED"
    assert store.get_product(conn, b)["status"] == "ACTIVE"
    assert store.status_counts(conn)["ALL"] == 1  # the purchased candidate is on the group card


def test_research_job_refuses_on_an_archived_product(conn, two_candidates):
    a, _b = two_candidates
    retailer = store.ensure_retailer(conn, "JB Hi-Fi")
    conn.execute("UPDATE products SET archived = 1 WHERE id = ?", (a,))
    conn.commit()

    with pytest.raises(ValueError, match="archived"):
        research.create_job(conn, a, [retailer["id"]])


def test_research_job_works_normally_on_an_unarchived_product(conn, two_candidates):
    a, _b = two_candidates
    retailer = store.ensure_retailer(conn, "JB Hi-Fi")
    conn.commit()
    job_id = research.create_job(conn, a, [retailer["id"]])  # must not raise
    assert job_id is not None


# ------------------------------------------------------- deleting a group (issue: no
# way to delete a group, and an empty one should remove itself)


def test_delete_group_detaches_members_rather_than_deleting_them(conn, two_candidates):
    a, b = two_candidates
    group_id = store.create_group(conn, {"name": "GPU search"})
    store.set_product_group(conn, a, group_id)
    store.set_product_group(conn, b, group_id)
    conn.commit()

    store.delete_group(conn, group_id)
    conn.commit()

    assert store.get_group(conn, group_id) is None
    assert store.get_product(conn, a)["group_id"] is None
    assert store.get_product(conn, b)["group_id"] is None
    # Neither product itself was touched, only detached.
    assert store.get_product(conn, a) is not None
    assert store.get_product(conn, b) is not None


def test_delete_group_if_empty_only_fires_with_no_members_left(conn, two_candidates):
    a, b = two_candidates
    group_id = store.create_group(conn, {"name": "GPU search"})
    store.set_product_group(conn, a, group_id)
    store.set_product_group(conn, b, group_id)
    conn.commit()

    assert store.delete_group_if_empty(conn, group_id) is False
    assert store.get_group(conn, group_id) is not None

    store.set_product_group(conn, a, None)
    conn.commit()
    assert store.delete_group_if_empty(conn, group_id) is False, "b is still a member"
    assert store.get_group(conn, group_id) is not None

    store.set_product_group(conn, b, None)
    conn.commit()
    assert store.delete_group_if_empty(conn, group_id) is True
    assert store.get_group(conn, group_id) is None


def test_an_archived_member_still_counts_as_belonging_to_its_group(conn, two_candidates):
    """Explicitly archiving alternatives keeps the group as historical context."""
    a, b = two_candidates
    group_id = store.create_group(conn, {"name": "GPU search"})
    store.set_product_group(conn, a, group_id)
    store.set_product_group(conn, b, group_id)
    conn.commit()
    store.archive_other_group_members(conn, a)
    conn.commit()

    assert store.get_product(conn, b)["archived"] == 1
    assert store.get_product(conn, b)["group_id"] == group_id
    assert store.delete_group_if_empty(conn, group_id) is False
    assert store.get_group(conn, group_id) is not None
