CREATE PROCEDURE footprint_context_sample.dynamic_revenue()
AS $$
BEGIN
    EXECUTE 'INSERT INTO footprint_context_sample.customer_totals SELECT customer_id, net_revenue FROM footprint_context_sample.customer_revenue';
END;
$$ LANGUAGE plpgsql;
