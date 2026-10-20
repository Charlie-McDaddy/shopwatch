ALTER TABLE listings ADD COLUMN conditional_requirements TEXT;
ALTER TABLE listings ADD COLUMN eligibility_confirmed INTEGER NOT NULL DEFAULT 0;
