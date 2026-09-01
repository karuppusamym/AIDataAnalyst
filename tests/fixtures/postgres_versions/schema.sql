-- CN-3: executable PostgreSQL version fixture schema.
--
-- Deliberately exercises every envelope 1.1 discovery axis the PostgreSQL
-- connector claims (`DEFAULT_CAPABILITIES` in `src/aida/connectors/postgres.py`):
-- constraints (PRIMARY KEY, UNIQUE, FOREIGN KEY), indexes, partitions, views,
-- materialized views, routines (function + procedure), object comments
-- (schema/table/column) and grants -- in one schema, so a single `discover()`
-- call against a live server proves all of them at once rather than one
-- mocked-row test per axis (see tests/test_connectors.py for the mocked-row
-- coverage this complements, not replaces).
--
-- Applied fresh (DROP ... CASCADE first) by both
-- tests/test_postgres_version_fixtures.py, against whichever live Postgres a
-- version leg targets, and by this compose stack's init-script mount for a
-- plain `docker compose up` run.

DROP SCHEMA IF EXISTS cn3_pg_fixture CASCADE;
CREATE SCHEMA cn3_pg_fixture;
COMMENT ON SCHEMA cn3_pg_fixture IS 'CN-3 executable version fixture schema';

CREATE TABLE cn3_pg_fixture.customer (
    customer_id BIGINT PRIMARY KEY,
    customer_name TEXT NOT NULL,
    email TEXT UNIQUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
COMMENT ON TABLE cn3_pg_fixture.customer IS 'Fixture customer table';
COMMENT ON COLUMN cn3_pg_fixture.customer.email IS 'Fixture email column';

INSERT INTO cn3_pg_fixture.customer (customer_id, customer_name, email) VALUES
    (1, 'Example Customer One', 'one@example.invalid'),
    (2, 'Example Customer Two', 'two@example.invalid');

-- Range-partitioned fact table: exercises `_PARTITION_KEY_SQL`/`_PARTITION_SQL`
-- (pg_partitioned_table + pg_inherits), which need at least one real partition
-- with a real partition key to prove `append_partition_rows` groups correctly
-- rather than merely parsing an empty result.
CREATE TABLE cn3_pg_fixture.order_fact (
    order_id BIGINT NOT NULL,
    customer_id BIGINT NOT NULL REFERENCES cn3_pg_fixture.customer(customer_id),
    order_date DATE NOT NULL,
    amount NUMERIC(12, 2) NOT NULL,
    PRIMARY KEY (order_id, order_date)
) PARTITION BY RANGE (order_date);

CREATE TABLE cn3_pg_fixture.order_fact_2025 PARTITION OF cn3_pg_fixture.order_fact
    FOR VALUES FROM ('2025-01-01') TO ('2026-01-01');
CREATE TABLE cn3_pg_fixture.order_fact_2026 PARTITION OF cn3_pg_fixture.order_fact
    FOR VALUES FROM ('2026-01-01') TO ('2027-01-01');

INSERT INTO cn3_pg_fixture.order_fact (order_id, customer_id, order_date, amount) VALUES
    (9001, 1, '2025-06-01', 125.00),
    (9002, 2, '2026-02-01', 500.00);

-- Secondary (non-primary, non-unique) index: exercises `_INDEX_SQL`.
CREATE INDEX order_fact_customer_idx ON cn3_pg_fixture.order_fact (customer_id);

CREATE VIEW cn3_pg_fixture.customer_order_summary AS
    SELECT c.customer_id, c.customer_name, COUNT(o.order_id) AS order_count
    FROM cn3_pg_fixture.customer c
    LEFT JOIN cn3_pg_fixture.order_fact o ON o.customer_id = c.customer_id
    GROUP BY c.customer_id, c.customer_name;

-- Materialized view: exercises the CN-3 fix in `postgres.py`
-- (`_MATERIALIZED_VIEW_COLUMN_SQL`) -- `information_schema.tables`/`.columns`
-- never list relkind 'm', so before that fix this table's columns and
-- `view_definition` were silently absent from `discover()`'s output.
CREATE MATERIALIZED VIEW cn3_pg_fixture.customer_order_summary_mv AS
    SELECT * FROM cn3_pg_fixture.customer_order_summary;

CREATE FUNCTION cn3_pg_fixture.total_order_amount(p_customer_id BIGINT)
RETURNS NUMERIC
LANGUAGE sql
AS $$
    SELECT COALESCE(SUM(amount), 0)
    FROM cn3_pg_fixture.order_fact
    WHERE customer_id = p_customer_id
$$;

CREATE PROCEDURE cn3_pg_fixture.touch_customer(p_customer_id BIGINT)
LANGUAGE sql
AS $$
    UPDATE cn3_pg_fixture.customer
    SET created_at = created_at
    WHERE customer_id = p_customer_id
$$;

-- `PUBLIC` is a pseudo-role that always exists, so this exercises `_GRANT_SQL`
-- (information_schema.role_table_grants) without needing CREATE ROLE
-- privilege, which a Postgres service container's default user may not have.
GRANT SELECT ON cn3_pg_fixture.customer TO PUBLIC;
