ALTER TABLE household_settings ADD COLUMN timezone TEXT NOT NULL DEFAULT 'Australia/Brisbane';

CREATE TABLE product_check_schedules (
    product_id INTEGER PRIMARY KEY REFERENCES products(id) ON DELETE CASCADE,
    cadence TEXT NOT NULL DEFAULT 'weekly'
        CHECK (cadence IN ('daily', 'twice_weekly', 'weekly', 'monthly')),
    last_attempt_at TEXT,
    last_success_at TEXT,
    next_due_at TEXT NOT NULL DEFAULT (datetime('now')),
    failures INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    locked_until TEXT
);

INSERT INTO product_check_schedules (product_id)
SELECT id FROM products;

CREATE TRIGGER product_check_schedule_insert AFTER INSERT ON products
BEGIN
    INSERT INTO product_check_schedules (product_id) VALUES (NEW.id);
END;
