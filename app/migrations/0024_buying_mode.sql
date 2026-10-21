ALTER TABLE products ADD COLUMN buying_mode TEXT NOT NULL DEFAULT 'just_watching'
    CHECK (buying_mode IN ('just_watching', 'buy_asap'));
