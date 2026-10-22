-- A model may have separately watched sizes/flavours. Keep their records distinct.
PRAGMA foreign_keys = OFF;
BEGIN;
CREATE TEMP TABLE products_sequence_backup (seq INTEGER NOT NULL);
INSERT INTO products_sequence_backup
SELECT COALESCE((SELECT seq FROM sqlite_sequence WHERE name = 'products'), 0);
CREATE TABLE products_new (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    model TEXT NOT NULL,
    brand TEXT,
    category TEXT NOT NULL DEFAULT 'general',
    generation TEXT,
    verdict TEXT NOT NULL DEFAULT 'MAYBE',
    notes TEXT,
    trigger_price REAL,
    excellent_price REAL,
    historical_low_price REAL,
    lowest_known_price REAL,
    lowest_known_date TEXT,
    lowest_known_retailer TEXT,
    lowest_known_notes TEXT,
    width_mm REAL,
    height_mm REAL,
    depth_mm REAL,
    weight_kg REAL,
    packaged_width_mm REAL,
    packaged_height_mm REAL,
    packaged_depth_mm REAL,
    packaged_weight_kg REAL,
    mounting_notes TEXT,
    vesa TEXT,
    fit_notes TEXT,
    specs_json TEXT NOT NULL DEFAULT '{}',
    components_json TEXT NOT NULL DEFAULT '{}',
    archived INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    status TEXT NOT NULL DEFAULT 'ACTIVE',
    group_id INTEGER REFERENCES watch_groups(id) ON DELETE SET NULL,
    trigger_price_auto INTEGER NOT NULL DEFAULT 0,
    excellent_price_auto INTEGER NOT NULL DEFAULT 0,
    historical_low_price_auto INTEGER NOT NULL DEFAULT 0,
    quantity REAL,
    quantity_unit TEXT,
    buying_mode TEXT NOT NULL DEFAULT 'just_watching'
        CHECK (buying_mode IN ('just_watching', 'buy_asap')),
    variant TEXT,
    description TEXT,
    image_url TEXT
);
INSERT INTO products_new SELECT * FROM products;
DROP TABLE products;
ALTER TABLE products_new RENAME TO products;
CREATE INDEX idx_products_group ON products(group_id);
CREATE UNIQUE INDEX idx_products_model_variant ON products(model, COALESCE(variant, ''));
CREATE TRIGGER product_check_schedule_insert AFTER INSERT ON products
BEGIN
    INSERT INTO product_check_schedules (product_id) VALUES (NEW.id);
END;
CREATE TRIGGER product_discovery_schedule_insert AFTER INSERT ON products
BEGIN
    INSERT INTO product_discovery_schedules (product_id) VALUES (NEW.id);
END;
UPDATE sqlite_sequence SET seq = MAX(seq, (SELECT seq FROM products_sequence_backup))
WHERE name = 'products';
INSERT INTO sqlite_sequence(name, seq)
SELECT 'products', seq FROM products_sequence_backup
WHERE NOT EXISTS (SELECT 1 FROM sqlite_sequence WHERE name = 'products');
DROP TABLE products_sequence_backup;
COMMIT;
PRAGMA foreign_keys = ON;
