CREATE PROCEDURE footprint_context_sample.refresh_totals
AS
BEGIN
    SELECT o.customer_id, SUM(o.amount - o.discount) AS net_revenue
    INTO #footprint_totals
    FROM footprint_context_sample.orders o
    GROUP BY o.customer_id;

    INSERT INTO footprint_context_sample.customer_totals (customer_id, net_revenue)
    SELECT t.customer_id, t.net_revenue FROM #footprint_totals t;
END;
