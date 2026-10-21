"""Alert evaluation and delivery.

Rules fire on delivered price, not headline price. An alert that cannot be delivered is
reported loudly and counted as a failure: a notifier that swallows its own errors is
indistinguishable from one that is switched off.
"""

from __future__ import annotations

import json
import logging
import math
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass, field
from hashlib import sha256
from typing import Any

from . import pricing, provenance, store
from .config import Config, load_config
from .db import utcnow

log = logging.getLogger("shopwatch.alerts")

try:  # pragma: no cover
    import requests
except ImportError:  # pragma: no cover
    requests = None  # type: ignore[assignment]


@dataclass
class Alert:
    product_name: str
    model: str
    retailer: str
    delivered: float
    classification: str
    condition: str
    rule_id: int
    rule_name: str
    url: str | None
    listing_id: int
    reason: str
    stock: str | None = None
    resolved: bool = True
    preference_kind: str | None = None

    def title(self) -> str:
        return f"{pricing.CLASS_LABELS[self.classification]}: {self.product_name}"

    def body(self) -> str:
        lines = [
            f"{self.retailer} - ${self.delivered:.0f} delivered ({self.condition})",
            f"Model: {self.model}",
            f"Rule: {self.rule_name} ({self.reason})",
        ]
        if self.stock:
            lines.append(f"Stock: {self.stock}")
        if not self.resolved:
            lines.append("WARNING: freight unresolved, delivered price is provisional")
        if self.url:
            lines.append(self.url)
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        return {
            "product": self.product_name, "model": self.model, "retailer": self.retailer,
            "delivered": self.delivered, "classification": self.classification,
            "condition": self.condition, "rule": self.rule_name, "reason": self.reason,
            "stock": self.stock, "url": self.url, "listing_id": self.listing_id,
            "freight_resolved": self.resolved, "at": utcnow(),
        }


@dataclass
class DeliveryReport:
    sent: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failures


def _conditions(rule: Mapping[str, Any]) -> set[str]:
    return {c.strip().upper() for c in (rule["conditions"] or "NEW").split(",") if c.strip()}


def evaluate_product(
    conn: sqlite3.Connection, product: Mapping[str, Any]
) -> list[Alert]:
    """Every alert this product would raise right now.

    A purchased card remains watched. Parked and archived cards raise no alerts; an
    open price-protection window can also raise its purchase-specific alert.
    """
    status = product.get("status", "ACTIVE")
    if product.get("archived") or status == "PARKED":
        return []

    preferences = conn.execute(
        "SELECT * FROM product_alert_preferences WHERE product_id = ?", (product["id"],)
    ).fetchone()
    # Once the household chooses the product-level watch, it replaces legacy defaults.
    # Otherwise a drop-only setting could still emit an old seeded target alert.
    rules = [] if preferences else conn.execute(
        "SELECT * FROM alert_rules WHERE product_id = ? AND enabled = 1", (product["id"],)
    ).fetchall()
    alerts: list[Alert] = []
    for rule in rules:
        allowed = _conditions(rule)
        candidates = []
        for listing in product["listings"]:
            # An excluded listing or flagged model never alerts. best_listing() applies
            # the same rule, so the board and phone cannot recommend different offers.
            if not _confirmed_listing(listing, require_new=False):
                continue
            if (listing["condition"] or "NEW").upper() not in allowed:
                continue
            if rule["require_complete"] and not (listing["included_components"] or "").strip():
                continue
            candidates.append(listing)
        # A rule describes the product's best buying option, not every retailer's
        # independent sale. An inferior seller changing price cannot trigger it.
        if not candidates:
            continue
        listing = min(candidates, key=lambda item: item["delivered"].value)
        delivered = listing["delivered"]
        if delivered.value > rule["max_delivered"]:
            continue
        last = rule["last_fired_price"]
        if last is not None and abs(last - delivered.value) < rule["min_change"]:
            continue  # not a meaningful change
        alerts.append(
            Alert(
                product_name=product["name"],
                model=product["model"],
                retailer=listing["retailer_name"],
                delivered=delivered.value,
                classification=listing["classification"],
                condition=listing["condition"] or "NEW",
                rule_id=rule["id"],
                rule_name=rule["name"],
                url=listing["url"],
                listing_id=listing["id"],
                stock=listing["stock_status"],
                resolved=delivered.resolved,
                reason=f"delivered ${delivered.value:.0f} <= ${rule['max_delivered']:.0f}",
            )
        )

    if preferences:
        candidates = [
            listing for listing in product["listings"]
            if _confirmed_listing(listing, require_new=True)
        ]
        best = min(candidates, key=lambda item: item["delivered"].value) if candidates else None
        if best:
            value = best["delivered"].value
            target_due = False
            if preferences["target_price"] is not None and value <= preferences["target_price"]:
                last = preferences["target_last_fired_price"]
                target_due = (last is None or preferences["target_above_target"]
                              or (value < last
                                  and last - value >= preferences["drop_min_change"]))
            drop_due = False
            if preferences["drop_enabled"]:
                baseline = preferences["drop_baseline_price"]
                drop_due = (baseline is not None
                            and value < baseline
                            and baseline - value >= preferences["drop_min_change"]
                            and preferences["drop_last_fired_price"] != value)
            if target_due or drop_due:
                kind = "target+drop" if target_due and drop_due else (
                    "target" if target_due else "drop"
                )
                name = "Target price" if target_due else "Price drop"
                alerts.append(_preference_alert(product, best, name, kind, value))
    if status == "PURCHASED" and product.get("protection_open"):
        protection = _protection_alerts(conn, product)
        # If both paths describe the same price movement, retain the useful guarantee
        # window explanation and drop the generic target alert.
        for protected in protection:
            alerts = [a for a in alerts if not (
                a.listing_id == protected.listing_id and a.delivered == protected.delivered
            )]
        alerts.extend(protection)
    # If legacy rules and preferences describe the same offer movement, deliver one
    # event. Keep price protection's purchase-specific explanation when it overlaps.
    unique: dict[tuple[int, float], Alert] = {}
    for alert in alerts:
        key = (alert.listing_id, alert.delivered)
        previous = unique.get(key)
        def priority(item: Alert) -> int:
            return 2 if item.rule_name == "Price protection" else (
                1 if item.preference_kind else 0
            )

        if previous is None or priority(alert) > priority(previous):
            unique[key] = alert
    alerts = list(unique.values())
    return alerts


def _preference_alert(
    product: Mapping[str, Any], listing: Mapping[str, Any],
    rule_name: str, reason: str, value: float,
) -> Alert:
    return Alert(
        product_name=product["name"], model=product["model"],
        retailer=listing["retailer_name"], delivered=value,
        classification=listing["classification"], condition=listing["condition"] or "NEW",
        rule_id=product["id"], rule_name=rule_name, url=listing["url"],
        listing_id=listing["id"], reason=f"best confirmed comparable delivered price: ${value:.0f}",
        stock=listing["stock_status"], resolved=True,
        preference_kind=reason,
    )


def _confirmed_listing(listing: Mapping[str, Any], *, require_new: bool) -> bool:
    delivered = listing.get("delivered")
    if not delivered or not delivered.known or not delivered.resolved:
        return False
    if not pricing.listing_is_comparable(listing):
        return False
    if not pricing.listing_eligibility(
        listing, require_new=require_new, require_delivery=True
    )[0]:
        return False
    freight_source = (listing.get("provenance") or {}).get("freight", {}).get("state")
    freight_verification = (
        (listing.get("verification") or {}).get("freight", {}).get("status")
    )
    if (freight_verification in {provenance.FLAGGED, provenance.STALE}
            and freight_source not in {provenance.LIVE, provenance.MANUAL}):
        return False
    # A populated imported freight field is calculable, but it is not a confirmed quote.
    return freight_source in {provenance.LIVE, provenance.MANUAL} or freight_verification in {
        "VERIFIED", "LIVE", "MANUAL",
    }


def _drop_context(
    conn: sqlite3.Connection,
    product: Mapping[str, Any],
    listing: Mapping[str, Any],
) -> str:
    settings = store.household_settings(conn)
    context = {
        "quantity": product.get("quantity"), "quantity_unit": product.get("quantity_unit"),
        "postcode": settings.get("postcode"),
        "condition": listing.get("condition"),
        "conditional_requirements": listing.get("conditional_requirements"),
        "eligibility_confirmed": listing.get("eligibility_confirmed"),
        "cashback": listing.get("cashback"),
    }
    return sha256(json.dumps(context, sort_keys=True).encode()).hexdigest()


def refresh_drop_baseline(conn: sqlite3.Connection, product: Mapping[str, Any]) -> None:
    """Seed/reset a drop baseline when a comparable buying context changes."""
    row = conn.execute(
        "SELECT drop_enabled, drop_baseline_context FROM product_alert_preferences"
        " WHERE product_id = ?", (product["id"],)
    ).fetchone()
    if not row:
        return
    candidates = [
        listing for listing in product["listings"]
        if _confirmed_listing(listing, require_new=True)
    ]
    best = min(candidates, key=lambda item: item["delivered"].value) if candidates else None
    context = _drop_context(conn, product, best) if best else None
    if row["drop_enabled"] and context != row["drop_baseline_context"]:
        conn.execute(
            "UPDATE product_alert_preferences SET drop_baseline_price = ?,"
            " drop_baseline_context = ?, updated_at = ? WHERE product_id = ?",
            (best["delivered"].value if best else None, context, utcnow(), product["id"]),
        )
    if best:
        conn.execute(
            "UPDATE product_alert_preferences SET target_above_target = 1 WHERE product_id = ?"
            " AND target_price IS NOT NULL AND ? > target_price",
            (product["id"], best["delivered"].value),
        )


def get_preferences(conn: sqlite3.Connection, product_id: int) -> dict[str, Any]:
    row = conn.execute(
        "SELECT * FROM product_alert_preferences WHERE product_id = ?", (product_id,)
    ).fetchone()
    if row is None:
        return {"product_id": product_id, "target_price": None, "drop_enabled": False,
                "drop_min_change": 20.0}
    return {**dict(row), "drop_enabled": bool(row["drop_enabled"])}


def update_preferences(
    conn: sqlite3.Connection, product_id: int, payload: Mapping[str, Any]
) -> dict[str, Any]:
    target = payload.get("target_price")
    threshold = payload.get("drop_min_change", 20.0)
    enabled = payload.get("drop_enabled", False)
    if target is not None and (isinstance(target, bool) or not isinstance(target, (int, float))
                               or not math.isfinite(target) or target < 0):
        raise ValueError("target_price must be a non-negative finite number or null")
    if isinstance(threshold, bool) or not isinstance(threshold, (int, float)) \
            or not math.isfinite(threshold) or threshold < 0:
        raise ValueError("drop_min_change must be a non-negative finite number")
    if not isinstance(enabled, bool):
        raise ValueError("drop_enabled must be a boolean")
    existing = conn.execute(
        "SELECT * FROM product_alert_preferences WHERE product_id = ?", (product_id,)
    ).fetchone()
    if existing is None:
        conn.execute(
            "INSERT INTO product_alert_preferences (product_id, target_price, drop_enabled,"
            " drop_min_change) VALUES (?, ?, ?, ?)",
            (product_id, target, int(enabled), threshold),
        )
    else:
        target_changed = existing["target_price"] != target
        drop_changed = (bool(existing["drop_enabled"]) != enabled
                        or existing["drop_min_change"] != threshold)
        conn.execute(
            "UPDATE product_alert_preferences SET target_price = ?, drop_enabled = ?,"
            " drop_min_change = ?,"
            " target_last_fired_price = CASE WHEN ? THEN NULL ELSE target_last_fired_price END,"
            " target_above_target = CASE WHEN ? THEN 1 ELSE target_above_target END,"
            " drop_baseline_price = CASE WHEN ? THEN NULL ELSE drop_baseline_price END,"
            " drop_baseline_context = CASE WHEN ? THEN NULL ELSE drop_baseline_context END,"
            " drop_last_fired_price = CASE WHEN ? THEN NULL ELSE drop_last_fired_price END,"
            " updated_at = ? WHERE product_id = ?",
            (target, int(enabled), threshold, int(target_changed), int(target_changed),
             int(drop_changed), int(drop_changed), int(drop_changed), utcnow(), product_id),
        )
    return get_preferences(conn, product_id)


def _protection_alerts(conn: sqlite3.Connection, product: Mapping[str, Any]) -> list[Alert]:
    """Fire when a purchased product is now available for less than was paid.

    Only confirmed delivered prices count: telling someone to chase a price guarantee on
    an unconfirmed figure sends them to a checkout to find out it was never cheaper.
    """
    purchase = product.get("purchase")
    if not purchase:
        return []
    paid = purchase["price_paid"]
    best = None
    for listing in product["listings"]:
        delivered = listing["delivered"]
        # Same rule as the buy alerts: a listing you have rejected cannot be the
        # evidence for chasing a price guarantee against it.
        if not _confirmed_listing(listing, require_new=False):
            continue
        if delivered.value >= paid:
            continue
        if best is None or delivered.value < best["delivered"].value:
            best = listing
    if best is None:
        return []

    drop = round(paid - best["delivered"].value, 2)
    last = purchase.get("last_alert_price")
    if last is not None and abs(last - best["delivered"].value) < 20.0:
        return []

    return [
        Alert(
            product_name=product["name"],
            model=product["model"],
            retailer=best["retailer_name"],
            delivered=best["delivered"].value,
            classification=best["classification"],
            condition=best["condition"] or "NEW",
            rule_id=-purchase["id"],  # negative id marks a purchase-derived alert
            rule_name="Price protection",
            url=best["url"],
            listing_id=best["id"],
            stock=best["stock_status"],
            resolved=True,
            reason=(
                f"${drop:.0f} below the ${paid:.0f} you paid on "
                f"{str(purchase['purchased_at'])[:10]}; protection window closes "
                f"{purchase['price_protection_until']}"
            ),
        )
    ]


def evaluate_all(conn: sqlite3.Connection) -> list[Alert]:
    alerts: list[Alert] = []
    for product in store.list_products(conn):
        alerts.extend(evaluate_product(conn, product))
    return alerts


def record_fired(conn: sqlite3.Connection, alert: Alert) -> None:
    """Remember what fired, so the same price does not alert every run.

    A negative rule_id is a purchase-derived price-protection alert, whose suppression
    state lives on the purchase row rather than on an alert rule.
    """
    if alert.preference_kind:
        conn.execute(
            "UPDATE product_alert_preferences SET"
            " target_last_fired_price = CASE WHEN ? IN ('target', 'target+drop')"
            " THEN ? ELSE target_last_fired_price END,"
            " target_above_target = CASE WHEN ? IN ('target', 'target+drop')"
            " THEN 0 ELSE target_above_target END,"
            " drop_last_fired_price = CASE WHEN ? IN ('drop', 'target+drop')"
            " THEN ? ELSE drop_last_fired_price END,"
            " drop_baseline_price = CASE WHEN ? IN ('drop', 'target+drop')"
            " THEN ? ELSE drop_baseline_price END, updated_at = ? WHERE product_id = ?",
            (alert.preference_kind, alert.delivered, alert.preference_kind,
             alert.preference_kind, alert.delivered, alert.preference_kind,
             alert.delivered, utcnow(), alert.rule_id),
        )
        return
    if alert.rule_id < 0:
        conn.execute(
            "UPDATE product_alert_preferences SET"
            " target_last_fired_price = CASE WHEN target_price IS NOT NULL"
            " AND ? <= target_price THEN ? ELSE target_last_fired_price END,"
            " target_above_target = CASE WHEN target_price IS NOT NULL"
            " AND ? <= target_price THEN 0 ELSE target_above_target END,"
            " drop_last_fired_price = CASE WHEN drop_enabled = 1"
            " AND drop_baseline_price IS NOT NULL"
            " AND ? <= drop_baseline_price - drop_min_change THEN ? ELSE drop_last_fired_price END,"
            " drop_baseline_price = CASE WHEN drop_enabled = 1"
            " AND drop_baseline_price IS NOT NULL"
            " AND ? <= drop_baseline_price - drop_min_change THEN ? ELSE drop_baseline_price END"
            " WHERE product_id = (SELECT product_id FROM purchases WHERE id = ?)",
            (alert.delivered, alert.delivered, alert.delivered, alert.delivered, alert.delivered,
             alert.delivered, alert.delivered, -alert.rule_id),
        )
        conn.execute(
            "UPDATE purchases SET last_alert_price = ? WHERE id = ?",
            (alert.delivered, -alert.rule_id),
        )
        conn.execute(
            "UPDATE alert_rules SET last_fired_at = ?, last_fired_price = ?"
            " WHERE product_id = (SELECT product_id FROM purchases WHERE id = ?)"
            " AND enabled = 1",
            (utcnow(), alert.delivered, -alert.rule_id),
        )
        return
    conn.execute(
        "UPDATE purchases SET last_alert_price = ? WHERE product_id = ("
        " SELECT product_id FROM alert_rules WHERE id = ?)"
        " AND price_protection_until IS NOT NULL",
        (alert.delivered, alert.rule_id),
    )
    conn.execute(
        "UPDATE alert_rules SET last_fired_at = ?, last_fired_price = ? WHERE id = ?",
        (utcnow(), alert.delivered, alert.rule_id),
    )


def send(alert: Alert, config: Config | None = None) -> DeliveryReport:
    """Deliver one alert to every configured channel. Console is always used."""
    config = config or load_config()
    report = DeliveryReport()

    print(f"[ALERT] {alert.title()}\n{alert.body()}", flush=True)
    report.sent.append("console")

    if not config.alerts_enabled:
        return report

    if config.ntfy_url:
        try:
            _send_ntfy(alert, config)
            report.sent.append("ntfy")
        except Exception as exc:
            message = f"ntfy delivery failed: {type(exc).__name__}: {_safe_error(exc, config)}"
            log.error(message)
            print(f"[ALERT-FAILURE] {message}", flush=True)
            report.failures.append(message)

    if config.webhook_url:
        try:
            _send_webhook(alert, config)
            report.sent.append("webhook")
        except Exception as exc:
            message = f"webhook delivery failed: {type(exc).__name__}: {_safe_error(exc, config)}"
            log.error(message)
            print(f"[ALERT-FAILURE] {message}", flush=True)
            report.failures.append(message)

    return report


def _safe_error(exc: Exception, config: Config) -> str:
    message = str(exc)
    for secret in (config.ntfy_url, config.ntfy_token, config.webhook_url):
        if secret:
            message = message.replace(secret, "[configured destination]")
    return message


def _send_ntfy(alert: Alert, config: Config) -> None:
    if requests is None:
        raise RuntimeError("requests is not installed")
    headers = {
        "Title": alert.title(),
        "Priority": config.ntfy_priority,
        "Tags": "shopping_cart",
    }
    if alert.url:
        headers["Click"] = alert.url
    if config.ntfy_token:
        headers["Authorization"] = f"Bearer {config.ntfy_token}"
    response = requests.post(
        config.ntfy_url, data=alert.body().encode("utf-8"), headers=headers, timeout=15
    )
    # A 2xx means ntfy accepted the publish, not that a device displayed it.
    if response.status_code >= 300:
        raise RuntimeError(f"HTTP {response.status_code}: {response.text[:200]}")


def _send_webhook(alert: Alert, config: Config) -> None:
    if requests is None:
        raise RuntimeError("requests is not installed")
    response = requests.post(
        config.webhook_url,
        data=json.dumps(alert.to_dict()),
        headers={"Content-Type": "application/json"},
        timeout=15,
    )
    if response.status_code >= 300:
        raise RuntimeError(f"HTTP {response.status_code}: {response.text[:200]}")


def dispatch(
    conn: sqlite3.Connection, alerts: list[Alert], config: Config | None = None
) -> dict[str, Any]:
    """Send alerts and record what fired. Returns a report including delivery failures."""
    sent, failures = 0, []
    for alert in alerts:
        report = send(alert, config)
        if report.ok:
            sent += 1
        else:
            failures.extend(report.failures)
        # The rule is marked fired either way: a delivery failure is a notifier problem,
        # and re-firing the same alert every run would bury the failure in noise.
        record_fired(conn, alert)
        product_id = _alert_product_id(conn, alert)
        if product_id is not None:
            status = "failed" if report.failures else (
                "accepted" if {"ntfy", "webhook"} & set(report.sent) else "not_sent"
            )
            conn.execute(
                "INSERT INTO alert_history (product_id, listing_id, rule_name, delivered_price,"
                " retailer, classification, reason, delivery_status, delivery_error, payload,"
                " created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (product_id, alert.listing_id, alert.rule_name, alert.delivered, alert.retailer,
                 alert.classification, alert.reason, status,
                 "\n".join(report.failures) or None,
                 json.dumps(alert.to_dict()), utcnow()),
            )
    return {
        "alerts": len(alerts),
        "sent": sent,
        "delivery_failures": failures,
        "payload": [a.to_dict() for a in alerts],
    }


def _alert_product_id(conn: sqlite3.Connection, alert: Alert) -> int | None:
    if alert.preference_kind:
        return alert.rule_id
    if alert.rule_id < 0:
        row = conn.execute(
            "SELECT product_id FROM purchases WHERE id = ?", (-alert.rule_id,)
        ).fetchone()
        return row["product_id"] if row else None
    row = conn.execute(
        "SELECT product_id FROM alert_rules WHERE id = ?", (alert.rule_id,)
    ).fetchone()
    return row["product_id"] if row else None


def get_alert_history(
    conn: sqlite3.Connection, product_id: int | None = None, limit: int = 100
) -> list[dict[str, Any]]:
    if not 1 <= limit <= 500:
        raise ValueError("limit must be between 1 and 500")
    if product_id is None:
        rows = conn.execute(
            "SELECT * FROM alert_history ORDER BY created_at DESC, id DESC LIMIT ?", (limit,)
        )
    else:
        rows = conn.execute(
            "SELECT * FROM alert_history WHERE product_id = ?"
            " ORDER BY created_at DESC, id DESC LIMIT ?", (product_id, limit)
        )
    result = []
    for row in rows:
        item = dict(row)
        item["payload"] = json.loads(item["payload"])
        result.append(item)
    return result
