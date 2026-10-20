-- Marketplace offers on one product page may share a URL while belonging to distinct sellers.
-- Replace the old URL-only uniqueness key without discarding any listing or child records.
PRAGMA foreign_keys = OFF;

CREATE TABLE listings_rebuilt (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id          INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    retailer_id         INTEGER NOT NULL REFERENCES retailers(id) ON DELETE CASCADE,
    url                 TEXT,
    model_on_page       TEXT,
    advertised_price    REAL,
    freight             REAL,
    cashback            REAL,
    rebate              REAL,
    coupon_discount     REAL,
    condition           TEXT NOT NULL DEFAULT 'NEW',
    stock_status        TEXT,
    pickup_status       TEXT,
    pickup_location     TEXT,
    warranty            TEXT,
    included_components TEXT,
    seller_notes        TEXT,
    price_guide         TEXT,
    active              INTEGER NOT NULL DEFAULT 1,
    last_checked_at     TEXT,
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at          TEXT NOT NULL DEFAULT (datetime('now')),
    ruled_out           INTEGER NOT NULL DEFAULT 0,
    ruled_out_reason    TEXT,
    ruled_out_at        TEXT,
    price_valid_until   TEXT,
    seller_name         TEXT,
    seller_id           TEXT,
    offer_id            TEXT,
    offer_key           TEXT NOT NULL DEFAULT '',
    check_error         TEXT,
    UNIQUE (product_id, retailer_id, url, offer_key)
);

INSERT INTO listings_rebuilt (
    id, product_id, retailer_id, url, model_on_page, advertised_price, freight, cashback,
    rebate, coupon_discount, condition, stock_status, pickup_status, pickup_location,
    warranty, included_components, seller_notes, price_guide, active, last_checked_at,
    created_at, updated_at, ruled_out, ruled_out_reason, ruled_out_at, price_valid_until
)
SELECT id, product_id, retailer_id, url, model_on_page, advertised_price, freight, cashback,
       rebate, coupon_discount, condition, stock_status, pickup_status, pickup_location,
       warranty, included_components, seller_notes, price_guide, active, last_checked_at,
       created_at, updated_at, ruled_out, ruled_out_reason, ruled_out_at, price_valid_until
FROM listings;

DROP TABLE listings;
ALTER TABLE listings_rebuilt RENAME TO listings;
CREATE INDEX idx_listings_product ON listings(product_id);
PRAGMA foreign_keys = ON;
