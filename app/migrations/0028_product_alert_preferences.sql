-- One target and optional comparable-price drop watch per product.
CREATE TABLE IF NOT EXISTS product_alert_preferences (
    product_id             INTEGER PRIMARY KEY REFERENCES products(id) ON DELETE CASCADE,
    target_price           REAL,
    drop_enabled           INTEGER NOT NULL DEFAULT 0,
    drop_min_change        REAL NOT NULL DEFAULT 20.0,
    drop_baseline_price    REAL,
    drop_baseline_context  TEXT,
    target_last_fired_price REAL,
    target_above_target     INTEGER NOT NULL DEFAULT 1,
    drop_last_fired_price   REAL,
    updated_at             TEXT NOT NULL DEFAULT (datetime('now')),
    CHECK (target_price IS NULL OR target_price >= 0),
    CHECK (drop_min_change >= 0),
    CHECK (drop_enabled IN (0, 1))
);
