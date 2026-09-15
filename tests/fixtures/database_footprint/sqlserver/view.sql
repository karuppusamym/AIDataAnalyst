CREATE VIEW footprint_context_sample.customer_revenue AS
SELECT c.customer_id, c.region, SUM(o.amount - o.discount) AS net_revenue
FROM footprint_context_sample.customers c
JOIN footprint_context_sample.orders o ON o.customer_id = c.customer_id
GROUP BY c.customer_id, c.region;
