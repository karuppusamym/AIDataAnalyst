CREATE PROCEDURE footprint_context_sample.nested_revenue
AS
BEGIN
    EXEC footprint_context_sample.read_revenue;
END;
