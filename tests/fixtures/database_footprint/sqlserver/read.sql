CREATE PROCEDURE footprint_context_sample.read_revenue
AS
BEGIN
    SELECT r.customer_id, r.region, r.net_revenue
    FROM footprint_context_sample.customer_revenue r;
END;
