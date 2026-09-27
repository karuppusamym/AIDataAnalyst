CREATE FUNCTION footprint_context_sample.read_revenue()
RETURNS TABLE (customer_id integer, region varchar, net_revenue numeric)
AS $$
    SELECT r.customer_id, r.region, r.net_revenue
    FROM footprint_context_sample.customer_revenue r;
$$ LANGUAGE sql;
