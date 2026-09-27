CREATE FUNCTION footprint_context_sample.customer_net(p_customer_id integer)
RETURNS TABLE (customer_id integer, net_revenue numeric)
AS $$
    SELECT customer_id, net_revenue
    FROM footprint_context_sample.customer_revenue
    WHERE customer_id = p_customer_id;
$$ LANGUAGE sql;

CREATE FUNCTION footprint_context_sample.customer_net(p_customer_id integer, p_minimum numeric)
RETURNS TABLE (customer_id integer, net_revenue numeric)
AS $$
    SELECT customer_id, net_revenue
    FROM footprint_context_sample.customer_revenue
    WHERE customer_id = p_customer_id AND net_revenue >= p_minimum;
$$ LANGUAGE sql;
