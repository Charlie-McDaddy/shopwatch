DROP INDEX idx_products_model_variant;
CREATE UNIQUE INDEX idx_products_model_variant
ON products(model, COALESCE(variant, '')) WHERE model != '';
CREATE UNIQUE INDEX idx_products_name_variant_without_model
ON products(lower(trim(name)), COALESCE(variant, '')) WHERE model = '';
