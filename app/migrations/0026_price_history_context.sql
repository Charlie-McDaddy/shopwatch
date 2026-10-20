-- A price comparison is only like-for-like while the buying context stays the same.
-- Old observations lack enough context to make that claim, so mark them unknown.
ALTER TABLE price_history ADD COLUMN price_context TEXT;
ALTER TABLE price_history ADD COLUMN price_change_reason TEXT NOT NULL DEFAULT 'UNKNOWN';
