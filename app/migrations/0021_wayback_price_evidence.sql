-- 0021: keep sourced archive evidence and honest per-listing coverage results.
CREATE TABLE wayback_checks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id  INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    listing_id  INTEGER REFERENCES listings(id) ON DELETE SET NULL,
    original_url TEXT NOT NULL,
    checked_at  TEXT NOT NULL,
    status      TEXT NOT NULL,
    note        TEXT NOT NULL
);
CREATE INDEX idx_wayback_checks_product ON wayback_checks(product_id, checked_at);

CREATE TABLE wayback_price_evidence (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    check_id         INTEGER NOT NULL REFERENCES wayback_checks(id) ON DELETE CASCADE,
    product_id       INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    listing_id       INTEGER REFERENCES listings(id) ON DELETE SET NULL,
    original_url     TEXT NOT NULL,
    snapshot_url     TEXT NOT NULL,
    captured_at      TEXT NOT NULL,
    matched_model    TEXT NOT NULL,
    advertised_price REAL NOT NULL,
    currency         TEXT NOT NULL,
    price_context    TEXT NOT NULL
);
CREATE INDEX idx_wayback_evidence_product ON wayback_price_evidence(product_id, captured_at);
