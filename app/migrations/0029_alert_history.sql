CREATE TABLE IF NOT EXISTS alert_history (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id      INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    listing_id      INTEGER REFERENCES listings(id) ON DELETE SET NULL,
    rule_name       TEXT NOT NULL,
    delivered_price REAL NOT NULL,
    retailer        TEXT NOT NULL,
    classification  TEXT NOT NULL,
    reason          TEXT NOT NULL,
    delivery_status TEXT NOT NULL, -- accepted means service accepted; never means device displayed
    delivery_error  TEXT,
    payload         TEXT NOT NULL,
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_alert_history_product
    ON alert_history(product_id, created_at DESC, id DESC);
