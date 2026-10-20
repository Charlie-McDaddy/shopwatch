-- Retain the postcode each manually confirmed local fulfilment value applies to.
ALTER TABLE listings ADD COLUMN freight_postcode TEXT;
ALTER TABLE listings ADD COLUMN pickup_postcode TEXT;
