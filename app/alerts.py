"""Alert evaluation and delivery.

Rules fire on delivered price, not headline price. An alert that cannot be delivered is
reported loudly and counted as a failure: a notifier that swallows its own errors is
indistinguishable from one that is switched off.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from . import pricing, store
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

    rules = conn.execute(
        "SELECT * FROM alert_rules WHERE product_id = ? AND enabled = 1", (product["id"],)
    ).fetchall()
    alerts: list[Alert] = []
    for rule in rules:
        allowed = _conditions(rule)
        for listing in product["listings"]:
            delivered = listing["delivered"]
            if not delivered.known:
                continue
            # An excluded listing or flagged model never alerts. best_listing() applies
            # the same rule, so the board and phone cannot recommend different offers.
            if not pricing.listing_is_comparable(listing):
                continue
            if not pricing.listing_eligibility(
                listing, require_new=False, require_delivery=bool(rule["require_resolved"])
            )[0]:
                continue
            if (listing["condition"] or "NEW").upper() not in allowed:
                continue
            if rule["require_resolved"] and not delivered.resolved:
                continue
            if rule["require_complete"] and not (listing["included_components"] or "").strip():
                continue
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
    if status == "PURCHASED" and product.get("protection_open"):
        protection = _protection_alerts(conn, product)
        # If both paths describe the same price movement, retain the useful guarantee
        # window explanation and drop the generic target alert.
        for protected in protection:
            alerts = [a for a in alerts if not (
                a.listing_id == protected.listing_id and a.delivered == protected.delivered
            )]
        alerts.extend(protection)
    return alerts


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
        if not delivered.known or not delivered.resolved:
            continue
        # Same rule as the buy alerts: a listing you have rejected cannot be the
        # evidence for chasing a price guarantee against it.
        if not pricing.listing_is_comparable(listing):
            continue
        if not pricing.listing_eligibility(listing, require_new=False)[0]:
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
    if alert.rule_id < 0:
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
            message = f"ntfy delivery failed: {type(exc).__name__}: {exc}"
            log.error(message)
            print(f"[ALERT-FAILURE] {message}", flush=True)
            report.failures.append(message)

    if config.webhook_url:
        try:
            _send_webhook(alert, config)
            report.sent.append("webhook")
        except Exception as exc:
            message = f"webhook delivery failed: {type(exc).__name__}: {exc}"
            log.error(message)
            print(f"[ALERT-FAILURE] {message}", flush=True)
            report.failures.append(message)

    return report


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
    return {
        "alerts": len(alerts),
        "sent": sent,
        "delivery_failures": failures,
        "payload": [a.to_dict() for a in alerts],
    }
