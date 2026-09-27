CREATE PROCEDURE footprint_context_sample.dynamic_revenue
AS
BEGIN
    EXEC(N'SELECT customer_id, net_revenue FROM footprint_context_sample.customer_revenue');
END;
