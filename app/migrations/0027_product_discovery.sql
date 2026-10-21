ALTER TABLE retailer_search_jobs ADD COLUMN product_id INTEGER REFERENCES products(id) ON DELETE CASCADE;
ALTER TABLE retailer_search_jobs ADD COLUMN trigger TEXT NOT NULL DEFAULT 'manual';

CREATE TABLE product_discovery_schedules (
    product_id INTEGER PRIMARY KEY REFERENCES products(id) ON DELETE CASCADE,
    next_due_at TEXT NOT NULL DEFAULT (datetime('now')),
    last_attempt_at TEXT,
    last_error TEXT
);

INSERT INTO product_discovery_schedules (product_id)
SELECT id FROM products;

CREATE TRIGGER product_discovery_schedule_insert AFTER INSERT ON products
BEGIN
    INSERT INTO product_discovery_schedules (product_id) VALUES (NEW.id);
END;
