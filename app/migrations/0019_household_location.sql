-- 0019: household local area for delivery and pickup context.
CREATE TABLE IF NOT EXISTS household_settings (
    id              INTEGER PRIMARY KEY CHECK (id = 1),
    postcode        TEXT,
    pickup_radius_km INTEGER NOT NULL DEFAULT 25 CHECK (pickup_radius_km BETWEEN 1 AND 500)
);
INSERT OR IGNORE INTO household_settings (id, postcode, pickup_radius_km) VALUES (1, NULL, 25);
