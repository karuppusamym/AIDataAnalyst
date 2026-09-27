CREATE PROCEDURE footprint_context_sample.nested_revenue()
AS $$
BEGIN
    CALL footprint_context_sample.refresh_totals();
END;
$$ LANGUAGE plpgsql;
