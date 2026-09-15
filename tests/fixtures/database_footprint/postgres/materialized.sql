CREATE MATERIALIZED VIEW footprint_context_sample.revenue_snapshot AS
SELECT customer_id, region, net_revenue
FROM footprint_context_sample.customer_revenue;
