CREATE FUNCTION footprint_context_sample.customer_net(@customer_id int)
RETURNS TABLE
AS RETURN (
    SELECT customer_id, net_revenue
    FROM footprint_context_sample.customer_revenue
    WHERE customer_id = @customer_id
);
